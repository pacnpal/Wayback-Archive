"""Core downloader module for Wayback-Archive."""

import hashlib
import logging
import os
import posixpath
import queue
import re
import sys
import threading
import time
import traceback
import mimetypes
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse, unquote
from pathlib import Path
from typing import Optional, Set, Dict, List, Tuple
import requests
from bs4 import BeautifulSoup, Comment
from wayback_archive.config import Config

log = logging.getLogger("wayback_archive.downloader")


class WaybackDownloader:
    """Main downloader class for Wayback Machine archives."""

    # Common tracker/analytics patterns
    TRACKER_PATTERNS = [
        r"google-analytics\.com",
        r"googletagmanager\.com",
        r"facebook\.net",
        r"doubleclick\.net",
        r"googleads\.g\.doubleclick\.net",
        r"googlesyndication\.com",
        r"facebook\.com/tr",
        r"analytics\.",
        r"stats\.",
        r"tracking\.",
        r"tagmanager\.google\.com",
        r"gtag\.js",
        r"ga\.js",
        r"analytics\.js",
    ]

    # Common ad patterns
    AD_PATTERNS = [
        r"ads\.",
        r"advertising\.com",
        r"doubleclick\.net",
        r"googlesyndication\.com",
        r"googleads\.",
        r"adserver\.",
        r"banner",
        r"popup",
        r"sponsor",
    ]

    # Contact link patterns
    CONTACT_PATTERNS = [
        r"^mailto:",
        r"^tel:",
        r"^sms:",
        r"^whatsapp:",
        r"^callto:",
    ]

    def __init__(self, config: Config):
        """Initialize downloader with configuration."""
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
        )
        # Track corrupted font files (HTML error pages instead of actual fonts)
        self.corrupted_fonts: Set[str] = set()
        # Track font URLs that have already been pre-checked for corruption
        # (whether corrupted or not) so _check_and_remove_corrupted_fonts_in_css
        # never re-downloads the same URL when multiple CSS files reference it.
        self._font_prefetch_checked: Set[str] = set()
        # Concurrency primitives. `_lock` guards the crawl's shared mutable
        # state (config.visited_urls, config.downloaded_files,
        # corrupted_fonts, _font_prefetch_checked, the file counter and the
        # run stats). `_tlocal` holds per-worker page context — see
        # `_current_page_url` usage — so concurrent HTML pages don't clobber
        # each other's relative-path base. Both are harmless no-ops in the
        # single-worker case.
        self._lock = threading.Lock()
        self._tlocal = threading.local()
        self._file_counter = 0
        # Per-run metrics counters (read by snapshot_metrics()).
        self._cache_hits = 0
        self._net_calls = 0
        self._net_ms_sum = 0.0
        self._net_ms_max = 0.0
        # `output_root` is the resolved sandbox root used by
        # `_get_local_path` when `sandbox_local_paths` is on. Computed
        # lazily in case `config.output_dir` is created post-construction.
        self._output_root_cache: Optional[Path] = None
        self._install_retry_adapter()
        if self.config.archive_only:
            self._install_archive_only_gate()
        self._parse_wayback_url()
        if self.config.purge_partial_on_start:
            self._purge_partial_last_file()
        if self.config.playwright_redirect_stub:
            self._install_redirect_stub_wrapper()
        if os.environ.get("USE_PLAYWRIGHT", "").strip().lower() in ("1", "true", "yes", "on"):
            self._install_playwright_render()

    @property
    def _current_page_url(self):
        """Per-worker base URL for relative-path computation.

        Stored on a thread-local so the concurrent crawl can process many
        HTML pages at once without one worker's base URL leaking into
        another's link rewriting. Reads return None until a worker enters
        `_process_html`.
        """
        return getattr(self._tlocal, "current_page_url", None)

    @_current_page_url.setter
    def _current_page_url(self, value):
        self._tlocal.current_page_url = value

    def _install_retry_adapter(self) -> None:
        """Mount an HTTPAdapter with retry/backoff and a connection pool
        sized for the configured worker count.

        Wayback throttles aggressively; without retries a single transient
        429/502/503/504 kicks an asset into the expensive multi-timestamp
        fallback path. The pool is sized to the worker count so the
        concurrent crawl doesn't serialize on urllib3's default
        10-connection pool.
        """
        try:
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
        except ImportError:
            return
        try:
            retry = Retry(
                total=4,
                backoff_factor=0.8,
                status_forcelist=(429, 502, 503, 504),
                allowed_methods=frozenset(["GET", "HEAD"]),
                respect_retry_after_header=True,
                raise_on_status=False,
            )
        except TypeError:
            # urllib3 < 1.26 used method_whitelist instead of allowed_methods.
            retry = Retry(
                total=4,
                backoff_factor=0.8,
                status_forcelist=(429, 502, 503, 504),
                raise_on_status=False,
            )
        pool = max(16, int(getattr(self.config, "workers", 1) or 1) * 2)
        adapter = HTTPAdapter(
            pool_connections=pool, pool_maxsize=pool, max_retries=retry,
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _timed_get(self, *args, **kwargs):
        """Wrapper around `self.session.get` that records the network call
        time into the per-instance metrics counters. Use this everywhere
        in `download_file` (primary fetch, if_ HTML fetch, timestamp
        retries via `_fetch_at_timestamp`, CDX recovery, live-origin
        fallback) so `snapshot_metrics()` reflects real activity.
        """
        t0 = time.monotonic()
        try:
            return self.session.get(*args, **kwargs)
        finally:
            self._record_network_call((time.monotonic() - t0) * 1000.0)

    def _install_archive_only_gate(self) -> None:
        """In archive_only mode, stop the session from following any
        captured redirect whose target leaves web.archive.org.

        ``id_`` raw playback replays a snapshot's response verbatim, so an
        old ``302 -> http://<dead-origin>/`` capture would otherwise have
        ``requests`` chase the long-dead origin host. Returning None from
        ``get_redirect_target`` tells ``requests`` "this response is not a
        redirect" and ends the chain at the archive boundary. This — plus
        the ``archive_only`` guards on the explicit live-origin fallback in
        ``download_file`` — is the whole of archive-only mode: the
        downloader never touches a non-web.archive.org host.
        """
        orig = getattr(self.session, "get_redirect_target", None)
        if orig is None:
            return

        def _gated_redirect_target(resp):
            target = orig(resp)
            if target is None:
                return None
            try:
                host = (urlparse(urljoin(resp.url or "", target)).hostname or "").lower()
            except Exception:
                return None
            return target if host == "web.archive.org" else None

        self.session.get_redirect_target = _gated_redirect_target  # type: ignore[assignment]

    # --- Snapshot accessors ---------------------------------------------

    def snapshot_metrics(self) -> tuple[int, int, float, float]:
        """Return ``(cache_hits, net_calls, net_ms_sum, net_ms_max)`` —
        the running counters for any consumer that wants to summarize a
        run. Read under the lock so concurrent worker updates are atomic.
        """
        with self._lock:
            return (
                self._cache_hits, self._net_calls,
                self._net_ms_sum, self._net_ms_max,
            )

    def _record_cache_hit(self) -> None:
        with self._lock:
            self._cache_hits += 1
        obs = self.config.metrics_observer
        if obs is not None:
            try:
                obs("cache_hit")
            except Exception:
                pass

    def _record_network_call(self, dt_ms: float) -> None:
        with self._lock:
            self._net_calls += 1
            self._net_ms_sum += dt_ms
            if dt_ms > self._net_ms_max:
                self._net_ms_max = dt_ms
        obs = self.config.metrics_observer
        if obs is not None:
            try:
                obs("net_call", duration_ms=dt_ms)
            except Exception:
                pass

    # --- Sandbox + masquerade helpers -----------------------------------

    def _resolved_output_root(self) -> Path:
        """Lazily-computed resolved output_dir, used as the sandbox root
        for `_get_local_path`'s clamp. Cached on the instance so the
        resolve() syscall doesn't run per-asset."""
        if self._output_root_cache is None:
            self._output_root_cache = Path(self.config.output_dir).resolve()
        return self._output_root_cache

    # --- Init-time wrappers (set up only when their Config flag is on) --

    def _purge_partial_last_file(self) -> None:
        """If the output-dir's `.log` shows a "Downloading ..." line with no
        matching outcome, remove the in-flight target file so a retried run
        starts clean. Best-effort: any error gets logged and ignored.

        Lifted from the dashboard resume shim's `_purge_partial_last_file`.
        """
        try:
            log_path = Path(self.config.output_dir) / ".log"
            if not log_path.is_file():
                return
            step_re = re.compile(r"\[\d+[^\]]*\]\s+Downloading\s+\S+:\s+(.+?)\s*$")
            outcome_re = re.compile(r"✓ Downloaded|Failed to download|\[resumed from disk\]")
            try:
                with log_path.open("rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - 32768))
                    tail = f.read().decode("utf-8", errors="replace")
            except OSError:
                return
            lines = tail.splitlines()
            last_idx = -1
            last_url = None
            for i, line in enumerate(lines):
                m = step_re.search(line)
                if m:
                    last_idx = i
                    last_url = m.group(1).strip()
            if last_idx < 0 or not last_url:
                return
            if outcome_re.search("\n".join(lines[last_idx + 1:])):
                return
            parsed = urlparse(last_url)
            netloc = (parsed.netloc or "").lower()
            if netloc.startswith("www."):
                netloc = netloc[4:]
            if not netloc:
                return
            # Preserve the query string: when `query_string_suffix` is on,
            # `_get_local_path` splices a query-hash into the filename, so
            # stripping the query here would look up the wrong path and
            # leave the partial file in place.
            normalized = parsed._replace(netloc=netloc, fragment="").geturl()
            local_path = self._get_local_path(normalized)
            if local_path.is_file():
                local_path.unlink()
        except Exception:
            # Best-effort; never abort init.
            pass

    _WAYBACK_URL_RE_CLASS = re.compile(
        r"^https?://web\.archive\.org/web/\d+[a-z_]*/(https?://.+)$",
        re.IGNORECASE,
    )

    @classmethod
    def _origin_from_wayback(cls, wb_url: str) -> Optional[str]:
        m = cls._WAYBACK_URL_RE_CLASS.match(wb_url or "")
        return m.group(1) if m else None

    def _install_redirect_stub_wrapper(self) -> None:
        """Capture redirect chains on the requests session. For any
        (pre, post) pair whose origin-site paths differ, write a tiny
        meta-refresh stub at the pre-redirect local path so cross-path
        internal links resolve locally even when Wayback bounced through
        an old origin URL.

        Lifted from the dashboard resume shim's `_patch_redirect_stubs`.
        """
        sess = self.session
        if getattr(sess, "_wa_redir_wrapped", False):
            return
        _orig_get = sess.get
        dl = self

        def wrapped_get(url, *a, **kw):
            resp = _orig_get(url, *a, **kw)
            try:
                history = getattr(resp, "history", None) or []
                if not history:
                    return resp
                final_origin = dl._origin_from_wayback(resp.url)
                if not final_origin:
                    return resp
                for hop in history:
                    pre_origin = dl._origin_from_wayback(hop.url)
                    if not pre_origin or pre_origin == final_origin:
                        continue
                    try:
                        pre_path = dl._get_local_path(pre_origin)
                        post_path = dl._get_local_path(final_origin)
                    except Exception:
                        continue
                    if pre_path == post_path or pre_path.exists():
                        continue
                    try:
                        # Compute as a filesystem path with os.path.relpath
                        # (POSIX) then normalize to URL separators. Using
                        # str(Path) directly would produce backslashes on
                        # Windows and break the meta-refresh URL.
                        import os.path as _ospath
                        fs_rel = _ospath.relpath(
                            str(post_path),
                            str(pre_path.parent) or ".",
                        )
                        rel = fs_rel.replace(os.sep, "/")
                    except Exception:
                        continue
                    stub = (
                        f'<!DOCTYPE html><meta http-equiv="refresh" '
                        f'content="0; url={rel}"><title>Redirect</title>\n'
                    )
                    try:
                        from wayback_archive.io_atomic import atomic_write_bytes
                        atomic_write_bytes(pre_path, stub.encode("utf-8"))
                    except Exception:
                        pass
            except Exception:
                pass
            return resp

        sess.get = wrapped_get  # type: ignore[assignment]
        sess._wa_redir_wrapped = True

    def _install_playwright_render(self) -> None:
        """When `USE_PLAYWRIGHT=1` and the `playwright` package is
        importable, wrap this instance's `download_file` so HTML pages
        are rendered through a single process-wide render thread before
        extraction.

        The render thread + Chromium browser are a *process-level
        singleton* (module-state `_PLAYWRIGHT_RENDER_Q` / `_THREAD`):
        every WaybackDownloader instance constructed in this process
        shares them. This matters because `repair()` spins up one
        WaybackDownloader per worker thread; without the singleton,
        a 4-worker repair job would spawn 4 browsers and 4 render
        threads. The first instance that opts in starts the singleton;
        subsequent ones reuse it.
        """
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:
            return

        from concurrent.futures import Future
        from wayback_archive.playback import url_ext as _url_ext

        _render_q = _ensure_playwright_singleton()
        if _render_q is None:  # singleton failed to start
            return

        _current_download = self.download_file

        def rendered_download(url: str):
            ext = _url_ext(url)
            is_html = not ext or ext in (".html", ".htm")
            if not is_html:
                return _current_download(url)
            content = _current_download(url)
            if not content:
                return content
            fut: "Future" = Future()
            _render_q.put((content.decode("utf-8", errors="replace"), fut))
            try:
                rendered = fut.result(timeout=30)
            except Exception:
                return content
            if rendered is None:
                return content
            return rendered.encode("utf-8")

        # Bind on the instance so we wrap exactly the one configured
        # downloader rather than monkey-patching the class.
        self.download_file = rendered_download  # type: ignore[assignment]

    def _parse_wayback_url(self):
        """Parse the Wayback Machine URL to extract the original URL."""
        # Extract timestamp and URL from Wayback URL
        # Format: https://web.archive.org/web/TIMESTAMP/URL
        match = re.match(
            r"https?://web\.archive\.org/web/(\d+[a-z]*)/(.+)", self.config.wayback_url
        )
        if match:
            timestamp, original_url = match.groups()
            # Ensure original_url starts with http/https
            if not original_url.startswith(("http://", "https://")):
                original_url = "http://" + original_url
            self.config.base_url = original_url
            self.config.domain = urlparse(original_url).netloc
            # Store original timestamp for timeframe fallback
            self.original_timestamp = timestamp
            # Parse timestamp to datetime for timeframe calculations
            try:
                numeric_part = re.match(r'(\d+)', timestamp).group(1)
                if len(numeric_part) >= 14:
                    self.original_datetime = datetime.strptime(numeric_part[:14], '%Y%m%d%H%M%S')
                else:
                    # Pad with zeros if needed
                    padded = numeric_part + '0' * (14 - len(numeric_part))
                    self.original_datetime = datetime.strptime(padded, '%Y%m%d%H%M%S')
            except (ValueError, AttributeError):
                # Fallback to current time if parsing fails
                self.original_datetime = datetime.now()
        else:
            raise ValueError(f"Invalid Wayback URL format: {self.config.wayback_url}")

    def _is_internal_url(self, url: str) -> bool:
        """Check if URL is internal to the site.
        
        Returns False for special schemes (tel:, mailto:, javascript:, etc.)
        """
        # Skip special URL schemes that shouldn't be downloaded
        non_downloadable_schemes = (
            'tel:', 'mailto:', 'javascript:', 'data:', 
            'ftp:', 'file:', 'sms:', 'whatsapp:', '#'
        )
        url_lower = url.lower().strip()
        if url_lower.startswith(non_downloadable_schemes) or url_lower == '#':
            return False
        
        parsed = urlparse(url)
        
        # Also check the parsed scheme
        if parsed.scheme and parsed.scheme.lower() not in ('http', 'https', ''):
            return False
        
        url_domain = parsed.netloc.lower().lstrip("www.")
        base_domain = self.config.domain.lower().lstrip("www.")

        # Treat Squarespace CDN as internal so we rewrite and download those assets.
        if self._is_squarespace_cdn(url):
            return True

        return url_domain == base_domain or url_domain == ""

    def _is_squarespace_cdn(self, url: str) -> bool:
        """Check if URL is from Squarespace CDN (should be downloaded)."""
        squarespace_domains = [
            'static1.squarespace.com',
            'static.squarespace.com',
            'images.squarespace-cdn.com',
            'definitions.sqspcdn.com',
            'sqspcdn.com'
        ]
        parsed = urlparse(url)
        url_domain = parsed.netloc.lower().lstrip("www.")
        return any(domain in url_domain for domain in squarespace_domains)

    def _is_tracker(self, url: str) -> bool:
        """Check if URL is a tracker/analytics script."""
        for pattern in self.TRACKER_PATTERNS:
            if re.search(pattern, url, re.IGNORECASE):
                return True
        return False

    def _is_ad(self, url: str) -> bool:
        """Check if URL is an ad."""
        for pattern in self.AD_PATTERNS:
            if re.search(pattern, url, re.IGNORECASE):
                return True
        return False

    def _is_contact_link(self, url: str) -> bool:
        """Check if URL is a contact link."""
        for pattern in self.CONTACT_PATTERNS:
            if re.search(pattern, url, re.IGNORECASE):
                return True
        return False

    def _convert_to_wayback_url(self, url: str) -> str:
        """Convert a regular URL to a Wayback Machine URL.
        
        This method is kept for backward compatibility.
        For timeframe fallback, use _convert_to_wayback_url_with_timestamp().
        """
        return self._convert_to_wayback_url_with_timestamp(url)
    
    def _convert_to_wayback_url_with_timestamp(self, url: str, timestamp: str = None, use_iframe: bool = False) -> str:
        """Convert a regular URL to a Wayback Machine URL with optional timestamp.
        
        Args:
            url: The original URL
            timestamp: Optional timestamp (YYYYMMDDHHMMSS). If None, uses original timestamp.
            use_iframe: If True, use 'if_' prefix to get unwrapped HTML content (no Wayback interface)
        """
        if url.startswith("http://web.archive.org") or url.startswith("https://web.archive.org"):
            return url
        
        if timestamp is None:
            timestamp = self.original_timestamp
        
        # For HTML pages, use 'if_' prefix to get unwrapped content (no Wayback interface)
        if use_iframe:
            return f"https://web.archive.org/web/{timestamp}if_/{url}"
        
        # Determine asset type prefix (im_, cs_, js_)
        parsed = urlparse(url)
        path = parsed.path.lower()
        asset_prefix = ""
        if any(ext in path for ext in [".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp"]):
            asset_prefix = "im_"
        elif any(ext in path for ext in [".woff", ".woff2", ".ttf", ".eot", ".otf"]):
            # Font files also use im_ prefix in Wayback Machine
            asset_prefix = "im_"
        elif any(ext in path for ext in [".css"]):
            asset_prefix = "cs_"
        elif any(ext in path for ext in [".js"]):
            asset_prefix = "js_"
        
        if asset_prefix:
            return f"https://web.archive.org/web/{timestamp}{asset_prefix}/{url}"
        return f"https://web.archive.org/web/{timestamp}/{url}"

    def _make_relative_path(self, url: str) -> str:
        """Convert absolute URL to relative path."""
        parsed = urlparse(url)
        path = parsed.path or "/"
        suffix = ""
        if parsed.query:
            suffix += "?" + parsed.query
        if parsed.fragment:
            suffix += "#" + parsed.fragment
        return self._to_relative_path(path) + suffix

    def _extract_original_url_from_path(self, path: str) -> Optional[str]:
        """Extract original URL from Wayback Machine path in HTML."""
        if not path or not isinstance(path, str):
            return None
        
        try:
            # Handle protocol-relative URLs: //web.archive.org/web/...
            if path.startswith("//"):
                path = "https:" + path
            
            # Pattern: /web/TIMESTAMP/https://original.com/path and replay variants
            # such as im_, cs_, js_, jm_, if_, and fw_.
            wayback_url_pattern = r"(?:https?://web\.archive\.org)?/web/\d+(?:[a-z]+_)?/(https?://[^\"\s'<>\)]+)"
            match = re.search(wayback_url_pattern, path)
            if match:
                extracted = match.group(1)
                extracted = extracted.rstrip('.,;:)\'"')
                return extracted
            
            # Pattern for mailto:/tel:/whatsapp: in wayback URLs
            # Handle both /web/... and https://web.archive.org/web/...
            wayback_protocol_pattern = r"(?:https?://web\.archive\.org)?/web/\d+[a-z]*/(mailto:|tel:|whatsapp:|sms:|callto:)(.+)"
            match = re.search(wayback_protocol_pattern, path)
            if match:
                protocol = match.group(1)
                rest = match.group(2).split("?")[0].split("&")[0]  # Remove query params
                return protocol + rest
        except Exception as e:
            # Silently fail - return None if extraction fails
            pass
        
        return None

    def _normalize_url(self, url: str, base_url: str) -> str:
        """Normalize URL and handle www/non-www conversion."""
        # Extract original URL from wayback paths first (handles both absolute and relative)
        original = self._extract_original_url_from_path(url)
        if original:
            url = original
        # Handle relative URLs (but not wayback paths - those should have been extracted above)
        elif not url.startswith(("http://", "https://", "//")):
            # Check if it's a relative wayback path
            if url.startswith("/web/"):
                # Try to construct full URL first
                full_url = urljoin(base_url, url)
                original = self._extract_original_url_from_path(full_url)
                if original:
                    url = original
                else:
                    url = full_url
            else:
                url = urljoin(base_url, url)

        # Handle protocol-relative URLs
        # Use the scheme from base_url to preserve http/https consistency
        if url.startswith("//"):
            parsed_base = urlparse(base_url)
            scheme = parsed_base.scheme if parsed_base.scheme else "http"
            url = f"{scheme}:{url}"

        parsed = urlparse(url)
        parsed_base = urlparse(base_url)
        
        # For internal URLs, preserve the scheme from base_url to ensure consistency
        # This prevents http:// URLs from being converted to https://
        url_domain = parsed.netloc.lower().lstrip("www.")
        base_domain = parsed_base.netloc.lower().lstrip("www.")
        if url_domain == base_domain or url_domain == "":
            # Internal URL - use base_url scheme
            if parsed_base.scheme and parsed.scheme != parsed_base.scheme:
                parsed = parsed._replace(scheme=parsed_base.scheme)

        # Handle www/non-www conversion
        if self.config.make_non_www and parsed.netloc.startswith("www."):
            parsed = parsed._replace(netloc=parsed.netloc[4:])
        elif self.config.make_www and not parsed.netloc.startswith("www.") and parsed.netloc:
            parsed = parsed._replace(netloc="www." + parsed.netloc)

        # Remove fragment and query string for file identification
        # This ensures URLs with different query params or fragments point to the same file
        # Preserve query string for asset URLs (e.g., format params on images)
        # but always drop fragments.
        url_normalized = parsed._replace(fragment="").geturl()

        return url_normalized

    def _get_local_path(self, url: str) -> Path:
        """
        Get local file path for a URL.
        This ensures consistent file naming that works with static file servers.
        Files are saved without query strings or fragments for clean URLs.

        When `config.sandbox_local_paths` is on, URLs without a netloc
        are rejected and the resolved path must stay inside `output_dir`.
        When `config.query_string_suffix` is on, a query string is
        disambiguated by splicing `.q-<sha1[:8]>` into the filename stem
        so same-path different-query URLs don't collide on disk.
        """
        parsed = urlparse(url)
        if self.config.sandbox_local_paths and not parsed.netloc:
            raise ValueError(f"no netloc: {url!r}")

        # Special handling for Google Fonts - preserve domain structure
        if "fonts.googleapis.com" in parsed.netloc or "fonts.gstatic.com" in parsed.netloc:
            # For Google Fonts, preserve the full domain and path structure
            # e.g., fonts.googleapis.com/css-abc123.css or fonts.gstatic.com/s/montserrat/v29/file.woff2
            domain_path = f"{parsed.netloc}{parsed.path}"
            # Remove leading slashes
            while domain_path.startswith("/"):
                domain_path = domain_path[1:]
            return self._apply_path_flags(
                Path(self.config.output_dir) / domain_path, parsed,
            )

        # Special handling for Squarespace CDN - preserve domain structure
        # This prevents CDN root URLs from overwriting index.html
        if self._is_squarespace_cdn(url):
            domain_path = f"{parsed.netloc}{parsed.path}"
            # Remove leading slashes
            while domain_path.startswith("/"):
                domain_path = domain_path[1:]
            # If no path, add index.html under the domain folder
            if not parsed.path or parsed.path == "/":
                domain_path = f"{parsed.netloc}/index.html"
            return self._apply_path_flags(
                Path(self.config.output_dir) / domain_path, parsed,
            )
        
        path = unquote(parsed.path)
        
        # Remove leading slashes (handle both single and double slashes)
        while path.startswith("/"):
            path = path[1:]
        
        # Clean up any double slashes in the middle of the path
        while "//" in path:
            path = path.replace("//", "/")

        # Default to index.html for directories
        if not path or path.endswith("/"):
            path = "index.html"

        # Determine if this is likely a page (HTML) or an asset
        # Check if it has a known asset extension
        known_asset_extensions = {
            ".css", ".js", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", 
            ".ico", ".woff", ".woff2", ".ttf", ".eot", ".otf", ".pdf", ".zip",
            ".mp4", ".mp3", ".avi", ".mov", ".wmv", ".flv", ".pdf", ".doc", ".docx"
        }
        
        has_extension = "." in os.path.basename(path)
        is_asset = False
        if has_extension:
            ext = os.path.splitext(path)[1].lower()
            is_asset = ext in known_asset_extensions

        # Add .html extension if no extension and it's not an asset (treat as page)
        if not has_extension and not is_asset:
            # If the path doesn't have a file extension, treat it as a page
            dir_part = os.path.dirname(path) if os.path.dirname(path) else ""
            base_part = os.path.basename(path) if os.path.basename(path) else "index"
            if dir_part:
                path = os.path.join(dir_part, base_part + ".html")
            else:
                path = base_part + ".html"

        return self._apply_path_flags(
            Path(self.config.output_dir) / path, parsed,
        )

    def _apply_path_flags(self, local: Path, parsed) -> Path:
        """Common post-processing for `_get_local_path`'s three return
        branches (Google Fonts, Squarespace, generic): splice the
        query-hash suffix into the stem if `query_string_suffix` is on,
        then sandbox-clamp inside `output_dir` if `sandbox_local_paths`
        is on. Centralizing this keeps the special-case branches from
        skipping the sandbox check — that was a path-traversal hole
        when CDN URLs contained `..` segments."""
        if self.config.query_string_suffix and parsed.query:
            from wayback_archive.query_hash import suffix_for_query
            suffix = suffix_for_query(parsed.query)
            local = local.with_name(local.stem + suffix + local.suffix)
        if self.config.sandbox_local_paths:
            try:
                resolved = local.resolve()
                resolved.relative_to(self._resolved_output_root())
            except Exception:
                raise ValueError(f"path escapes OUTPUT_DIR: {local}")
        return local

    def _get_relative_link_path(self, url: str, is_page: bool = True) -> str:
        """
        Get truly relative link path that matches where the file will be saved.
        Paths are relative to the current page so files work when opened
        directly from the filesystem (no web server required).

        Args:
            url: The normalized URL to convert
            is_page: If True, adds .html extension to paths without extensions.
                     If False, preserves the original extension (for assets).
        """
        parsed = urlparse(url)

        # Special handling for Google Fonts URLs - preserve domain structure
        if "fonts.googleapis.com" in parsed.netloc or "fonts.gstatic.com" in parsed.netloc:
            domain_path = f"{parsed.netloc}{parsed.path}"
            while domain_path.startswith("/"):
                domain_path = domain_path[1:]
            path = f"/{domain_path}"

        # Special handling for Squarespace CDN URLs - preserve domain structure
        elif self._is_squarespace_cdn(url):
            domain_path = f"{parsed.netloc}{parsed.path}"
            while domain_path.startswith("/"):
                domain_path = domain_path[1:]
            path = f"/{domain_path}"

        else:
            path = unquote(parsed.path)

            # Directory or root → index.html (matches _get_local_path behavior)
            if not path or path.endswith("/"):
                path = (path.rstrip("/") or "") + "/index.html"

            # Determine if this has an asset extension
            known_asset_extensions = {
                ".css", ".js", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp",
                ".ico", ".woff", ".woff2", ".ttf", ".eot", ".otf", ".pdf", ".zip",
                ".mp4", ".mp3", ".avi", ".mov", ".wmv", ".flv", ".pdf", ".doc", ".docx"
            }

            has_extension = "." in os.path.basename(path)
            is_asset = False
            if has_extension:
                ext = os.path.splitext(path)[1].lower()
                is_asset = ext in known_asset_extensions

            if is_page and not has_extension and not is_asset:
                path = path + ".html"

            # Ensure leading slash for relpath computation
            if not path.startswith("/"):
                path = "/" + path

        # Collect query/fragment suffix.
        # When `query_string_suffix` is on, the file on disk is renamed
        # with `.q-<hash>` spliced into its stem and NO `?query` part.
        # The href must match that filename, so we drop the `?query`
        # from the suffix and splice the same hash into `path` here —
        # otherwise the saved file and the rewritten link diverge and
        # the browser hits a 404 for every query-bearing internal asset.
        suffix = ""
        if parsed.query:
            if self.config.query_string_suffix:
                from wayback_archive.query_hash import suffix_for_query
                qhash = suffix_for_query(parsed.query)
                stem, dot, ext = path.rpartition(".")
                if dot and "/" not in ext:
                    path = f"{stem}{qhash}.{ext}"
                else:
                    path = path + qhash
            else:
                suffix += "?" + parsed.query
        if parsed.fragment:
            suffix += "#" + parsed.fragment

        # Compute truly relative path from the current page's directory
        current_url = getattr(self, '_current_page_url', None)
        if current_url:
            from_parsed = urlparse(current_url)
            from_path = unquote(from_parsed.path)
            if not from_path or from_path.endswith("/"):
                from_dir = from_path.rstrip("/") or "/"
            else:
                from_dir = posixpath.dirname(from_path)
            if not from_dir:
                from_dir = "/"
            path = posixpath.relpath(path, from_dir)

        return path + suffix

    def _to_relative_path(self, abs_path: str) -> str:
        """Convert a root-absolute path to one relative to the current page."""
        current_url = getattr(self, '_current_page_url', None)
        if not current_url or not abs_path.startswith("/"):
            return abs_path
        from_parsed = urlparse(current_url)
        from_path = unquote(from_parsed.path)
        if not from_path or from_path.endswith("/"):
            from_dir = from_path.rstrip("/") or "/"
        else:
            from_dir = posixpath.dirname(from_path)
        return posixpath.relpath(abs_path, from_dir or "/")

    def _search_snapshots_via_cdx(self, url: str) -> List[str]:
        """Query the Wayback CDX index for actual successful captures of url.

        The brute-force +/- timeframe scan in ``download_file`` guesses
        timestamps that may not be valid captures at all. The CDX API tells
        us exactly which timestamps DO have a 200 capture, so when an asset
        was only archived at a wildly different date than the page that
        references it, we can still pull it in.

        Returns a list of timestamp strings (YYYYMMDDHHMMSS), sorted by
        proximity to the run's original timestamp so the closest captures
        are tried first. Capped at ``config.auto_search_snapshots_limit``.
        Cached per URL only on a successful CDX response, so a transient
        network failure (timeout, 429, 503) doesn't permanently mark the
        URL as having no snapshots for the rest of the crawl.

        Implementation note: CDX returns captures chronologically and a
        single ``limit=N`` request would yield the EARLIEST N matches —
        for a URL with thousands of captures, every result could sit far
        from ``original_datetime``. We instead issue two windowed queries
        (the N captures just before the original timestamp, and the N
        captures just after) and merge them. That bounds the response
        size while guaranteeing the candidates we sort are actually near
        the run's anchor point.
        """
        try:
            cache = self._cdx_cache
        except AttributeError:
            with self._lock:
                if not hasattr(self, '_cdx_cache'):
                    self._cdx_cache = {}
            cache = self._cdx_cache
        if url in cache:
            return cache[url]

        from urllib.parse import quote
        per_side = max(1, self.config.auto_search_snapshots_limit)
        original_ts = getattr(self, 'original_timestamp', None)
        anchor = (original_ts or '')[:14] or self.original_datetime.strftime('%Y%m%d%H%M%S')

        def _query(extra_params: str) -> List[str]:
            cdx_url = (
                "https://web.archive.org/cdx/search/cdx"
                f"?url={quote(url, safe='')}"
                "&output=json"
                "&fl=timestamp,statuscode,mimetype"
                "&filter=statuscode:200"
                "&filter=!mimetype:warc/revisit"
                f"{extra_params}"
            )
            # Route through the dashboard's CDX gate (if injected) so the
            # shared rate-limit budget covers both repair-mode lookups and
            # the normal crawl's CDX recovery. Falls back to session.get
            # when no hook is configured.
            cdx_urlopen = self.config.cdx_urlopen
            if cdx_urlopen is not None:
                import urllib.request
                import json as _json
                req = urllib.request.Request(
                    cdx_url, headers={"User-Agent": "Wayback-Archive/1.0"},
                )
                with cdx_urlopen(req, timeout=15) as r:
                    raw = r.read()
                rows = _json.loads(raw) if raw else []
            else:
                response = self.session.get(cdx_url, timeout=15)
                response.raise_for_status()
                rows = response.json()
            # First row is the header.
            return [row[0] for row in rows[1:]] if len(rows) > 1 else []

        try:
            # The N captures immediately BEFORE the anchor (``limit=-N``
            # returns the last N matches in chronological order).
            before = _query(f"&to={anchor}&limit=-{per_side}")
            # The N captures immediately AFTER the anchor.
            after = _query(f"&from={anchor}&limit={per_side}")
        except Exception:
            # Transient failure — leave the cache untouched so the next
            # miss on this URL gets another chance.
            return []

        timestamps = list({*before, *after})

        def _proximity(ts: str) -> float:
            try:
                return abs(
                    (datetime.strptime(ts[:14], '%Y%m%d%H%M%S')
                     - self.original_datetime).total_seconds()
                )
            except Exception:
                return float('inf')

        timestamps.sort(key=_proximity)
        # Drop the original timestamp itself — download_file already
        # tried it before falling through to CDX search.
        if original_ts:
            timestamps = [t for t in timestamps if t != original_ts]
        result = timestamps[: self.config.auto_search_snapshots_limit]
        with self._lock:
            self._cdx_cache[url] = result
        return result

    def _fetch_at_timestamp(self, url: str, timestamp: Optional[str],
                            is_html_page: bool, timeout: int = 10
                            ) -> Optional[bytes]:
        """Try to download ``url`` at a specific Wayback ``timestamp``.

        Returns the response bytes on a 200 OK, or None on any failure
        (including a font response that was actually an HTML error page —
        those get recorded in ``corrupted_fonts``). Shared by both the
        brute-force timeframe scan and the CDX-driven snapshot search so
        they apply the same prefix selection and corruption check.
        """
        try:
            wayback_url = self._convert_to_wayback_url_with_timestamp(
                url, timestamp, use_iframe=is_html_page,
            )
            response = self._timed_get(wayback_url, timeout=timeout,
                                        allow_redirects=True)
            if response.status_code != 200:
                return None
            content = response.content
            if self._is_corrupted_font(content, url):
                normalized_url = self._normalize_url(url, self.config.base_url)
                self._mark_corrupted_font(normalized_url)
                return None
            if self.config.reject_html_masquerade:
                from wayback_archive.playback import looks_like_html_error, url_ext
                if looks_like_html_error(content, url_ext(url)):
                    return None
            return content
        except Exception:
            return None

    def _generate_timestamp_variants(self, hours_range: int = 24, step_hours: int = 1) -> List[str]:
        """Generate timestamp variants for timeframe search.
        
        Args:
            hours_range: How many hours before/after to search
            step_hours: Step size in hours between attempts
            
        Returns:
            List of timestamp strings (YYYYMMDDHHMMSS format)
        """
        timestamps = []
        base_time = self.original_datetime
        
        # Try timestamps before and after the original
        for hours_offset in range(-hours_range, hours_range + 1, step_hours):
            if hours_offset == 0:
                continue  # Skip the original timestamp (already tried)
            variant_time = base_time + timedelta(hours=hours_offset)
            timestamp_str = variant_time.strftime('%Y%m%d%H%M%S')
            timestamps.append(timestamp_str)
        
        # Sort by proximity to original (closest first)
        timestamps.sort(key=lambda ts: abs((datetime.strptime(ts, '%Y%m%d%H%M%S') - base_time).total_seconds()))
        
        return timestamps

    def _is_corrupted_font(self, content: bytes, url: str) -> bool:
        """Check if a downloaded font file is actually an HTML error page.
        
        Wayback Machine sometimes returns HTML error pages instead of font files.
        This detects those cases.
        """
        # Check if it's a font file extension
        font_extensions = ('.woff', '.woff2', '.ttf', '.eot', '.otf', '.svg')
        if not any(url.lower().endswith(ext) for ext in font_extensions):
            return False
        
        # Check if content starts with HTML (error page)
        # HTML typically starts with <!doctype, <html, or <HTML
        content_start = content[:200].strip()
        if content_start.startswith((b'<!doctype', b'<!DOCTYPE', b'<html', b'<HTML')):
            return True
        
        return False
    
    def download_file(self, url: str) -> Optional[bytes]:
        """Download a file from the given URL with timeframe fallback.

        If the file returns 404 at the original timestamp, searches nearby
        timestamps to find when the file was available.
        If all Wayback attempts fail, tries downloading from the original live URL.

        When `config.resume_from_disk` is on, an existing on-disk file at
        the computed local path is returned without a network call —
        unless its first 512 bytes sniff as a Wayback HTML-error
        masquerade, in which case it's unlinked and re-fetched.

        When `config.reject_html_masquerade` is on (default), every
        successful HTTP response is sniffed against the URL's extension;
        an HTML body in a binary slot returns None instead of being
        treated as content.
        """
        # Job-level resume: serve from disk if the file is already there
        # and not a masquerade. Guarded by Config flag (off by default to
        # preserve the standalone-CLI's historical "always re-fetch"
        # behavior).
        if self.config.resume_from_disk:
            from wayback_archive.playback import looks_like_html_error, url_ext
            try:
                # Normalize the URL the way the crawl loop later will, so the
                # cache lookup keys the same path the crawl writes to.
                parsed_norm = urlparse(url)
                netloc = (parsed_norm.netloc or "").lower()
                if netloc.startswith("www."):
                    netloc = netloc[4:]
                if netloc:
                    normalized = parsed_norm._replace(
                        netloc=netloc, fragment="",
                    ).geturl()
                    local_path = self._get_local_path(normalized)
                    if local_path.is_file() and local_path.stat().st_size > 0:
                        body = local_path.read_bytes()
                        if looks_like_html_error(body, url_ext(url)):
                            try:
                                local_path.unlink()
                            except OSError:
                                pass
                        else:
                            self._record_cache_hit()
                            print(f"         [resumed from disk] {local_path}", flush=True)
                            return body
            except ValueError:
                # Sandbox rejection — fall through to normal fetch path
                # which will also fail the same way, but with the
                # downloader's standard error handling.
                pass
            except Exception:
                pass

        # Determine if this is an HTML page (we should NOT fallback to live for HTML)
        parsed = urlparse(url)
        path_lower = parsed.path.lower()
        is_html_page = (
            not path_lower or 
            path_lower.endswith('.html') or 
            path_lower.endswith('.htm') or
            (not os.path.splitext(path_lower)[1] and 
             not any(path_lower.endswith(ext) for ext in ['.css', '.js', '.jpg', '.jpeg', '.png', '.gif', '.svg', '.woff', '.woff2', '.ttf', '.eot', '.otf', '.ico', '.json', '.xml', '.pdf']))
        )
        
        # For HTML pages, try the 'if_' version first to get unwrapped content
        # This avoids the Wayback Machine interface wrapper
        if is_html_page:
            wayback_url_if = self._convert_to_wayback_url_with_timestamp(url, use_iframe=True)
            try:
                response = self._timed_get(
                    wayback_url_if, timeout=15, allow_redirects=True
                )
                response.raise_for_status()
                content = response.content
                
                # Verify it's actually HTML content, not an error page
                content_start = content[:200].strip()
                if content_start.startswith((b'<!doctype', b'<!DOCTYPE', b'<html', b'<HTML')):
                    # The if_ version still has Wayback scripts but also contains the actual page
                    # Check if it has actual page content (not just the wrapper interface)
                    try:
                        html_str = content.decode("utf-8", errors="ignore")[:5000]
                        # Check if it's ONLY the wrapper (has Wayback Machine title AND no actual page content)
                        # The if_ version will have both Wayback scripts AND the actual page content
                        is_only_wrapper = (
                            "<title>Wayback Machine</title>" in html_str and
                            "<!-- This is Squarespace. -->" not in html_str and
                            "<body" not in html_str.lower() or
                            (html_str.count("<body") == 0 and "<!-- End Wayback Rewrite JS Include -->" not in html_str)
                        )
                        if not is_only_wrapper:
                            # Got content (even if it has Wayback scripts, it has the actual page)
                            return content
                    except:
                        # If we can't decode, assume it's good
                        return content
            except:
                # If if_ version fails, fall through to regular download
                pass
        
        # Try original timestamp first (or fallback from if_)
        wayback_url = self._convert_to_wayback_url_with_timestamp(url)
        try:
            response = self._timed_get(
                wayback_url, timeout=15, allow_redirects=True
            )
            response.raise_for_status()
            content = response.content

            # Check if font file is corrupted (HTML error page)
            if self._is_corrupted_font(content, url):
                # Mark as corrupted and don't return it
                normalized_url = self._normalize_url(url, self.config.base_url)
                self._mark_corrupted_font(normalized_url)
                print(f"         ⚠️  Font file is corrupted (HTML error page) - will be removed from CSS", flush=True)
                return None

            # General Wayback HTML-error-masquerade rejection: an asset
            # served as HTML (Wayback CGI error page) returned under a
            # binary content-type. Strictly more general than the
            # font-only check above and defaults ON.
            if self.config.reject_html_masquerade:
                from wayback_archive.playback import looks_like_html_error, url_ext
                if looks_like_html_error(content, url_ext(url)):
                    return None

            return content
        except requests.exceptions.HTTPError as e:
            if hasattr(e, 'response') and e.response is not None and e.response.status_code == 404:
                # File not found at original timestamp, try nearby timestamps
                # Use progressively wider search ranges with limited attempts
                for search_range, step, max_attempts in [(12, 2, 5), (48, 6, 5), (168, 24, 3)]:
                    timestamps = self._generate_timestamp_variants(
                        hours_range=search_range, step_hours=step
                    )

                    for timestamp in timestamps[:max_attempts]:
                        # Reuse the canonical fetch path so brute-force and
                        # CDX flows share one if_/asset-prefix selection,
                        # status check, and corrupted-font handling.
                        content = self._fetch_at_timestamp(
                            url, timestamp, is_html_page, timeout=10,
                        )
                        if content is not None:
                            return content

                # Brute-force timeframe scan exhausted. Ask the Wayback CDX
                # index for the snapshots that actually exist for this URL
                # and try the ones closest to the run's original timestamp.
                # This is what lets a partially-archived site come back
                # together: an asset captured only on a totally different
                # date than the page that references it still gets pulled.
                if self.config.auto_search_snapshots:
                    cdx_timestamps = self._search_snapshots_via_cdx(url)
                    if cdx_timestamps:
                        print(
                            f"         🔎 CDX found {len(cdx_timestamps)} snapshot(s) "
                            f"for {url[:80]}... — trying closest first",
                            flush=True,
                        )
                        for cdx_ts in cdx_timestamps:
                            content = self._fetch_at_timestamp(
                                url, cdx_ts, is_html_page,
                            )
                            if content is not None:
                                print(
                                    f"         ✓ Recovered via CDX snapshot {cdx_ts}",
                                    flush=True,
                                )
                                return content

                # All Wayback attempts failed - try the original live URL as a
                # fallback. Only for assets (never HTML pages), and never in
                # archive_only mode — there the run stays on web.archive.org.
                if not is_html_page and not self.config.archive_only:
                    try:
                        print(f"         🔄 Wayback failed, trying original URL: {url[:80]}...", flush=True)
                        live_response = self._timed_get(
                            url, timeout=10, allow_redirects=True
                        )
                        live_response.raise_for_status()
                        content = live_response.content
                        
                        # Check if font file is corrupted
                        if self._is_corrupted_font(content, url):
                            normalized_url = self._normalize_url(url, self.config.base_url)
                            self._mark_corrupted_font(normalized_url)
                            print(f"         ⚠️  Font file is corrupted (HTML error page) - will be removed from CSS", flush=True)
                            return None
                        if self.config.reject_html_masquerade:
                            from wayback_archive.playback import looks_like_html_error, url_ext
                            if looks_like_html_error(content, url_ext(url)):
                                return None
                        print(f"         ✓ Downloaded from original URL (fallback)", flush=True)
                        return content
                    except requests.exceptions.HTTPError:
                        pass
                    except requests.exceptions.Timeout:
                        pass
                    except Exception:
                        pass
            # Other HTTP errors - skip silently
        except requests.exceptions.Timeout:
            # Timeout on Wayback - try the original live URL as a fallback.
            # Only for assets, and never in archive_only mode.
            if not is_html_page and not self.config.archive_only:
                try:
                    print(f"         🔄 Wayback timeout, trying original URL: {url[:80]}...", flush=True)
                    live_response = self._timed_get(
                        url, timeout=10, allow_redirects=True
                    )
                    live_response.raise_for_status()
                    content = live_response.content
                    
                    # Check if font file is corrupted
                    if self._is_corrupted_font(content, url):
                        normalized_url = self._normalize_url(url, self.config.base_url)
                        self._mark_corrupted_font(normalized_url)
                        print(f"         ⚠️  Font file is corrupted (HTML error page) - will be removed from CSS", flush=True)
                        return None
                    if self.config.reject_html_masquerade:
                        from wayback_archive.playback import looks_like_html_error, url_ext
                        if looks_like_html_error(content, url_ext(url)):
                            return None
                    print(f"         ✓ Downloaded from original URL (fallback)", flush=True)
                    return content
                except Exception:
                    pass
        except Exception:
            pass

        return None
    
    def _get_file_type_from_url(self, url: str) -> str:
        """Get a human-readable file type from URL."""
        parsed = urlparse(url)
        path = parsed.path.lower()
        
        # Check for Google Fonts CSS files (they don't have .css extension)
        if "fonts.googleapis.com" in url and "/css" in url:
            return "CSS"
        
        if path.endswith('.html') or path.endswith('.htm') or not os.path.splitext(path)[1]:
            return "HTML"
        elif path.endswith('.css'):
            return "CSS"
        elif path.endswith('.js') or path.endswith('.mjs'):
            return "JavaScript"
        elif any(path.endswith(ext) for ext in ['.woff', '.woff2', '.ttf', '.eot', '.otf', '.svg']):
            return "Font"
        elif any(path.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.ico']):
            return "Image"
        elif path.endswith('.json'):
            return "JSON"
        elif path.endswith('.xml'):
            return "XML"
        else:
            return "Asset"

    def _optimize_html(self, html: str) -> str:
        """Optimize HTML code."""
        if not self.config.optimize_html:
            return html

        try:
            import minify_html
            # minify-html is a Python 3.14+ compatible alternative to htmlmin
            # minify_html.minify() expects a string, not bytes
            return minify_html.minify(html, minify_js=False, minify_css=False)
        except Exception as e:
            print(f"Error optimizing HTML: {e}")
            return html

    def _minify_js(self, content: str) -> str:
        """Minify JavaScript."""
        if not self.config.minify_js:
            return content

        try:
            import rjsmin

            return rjsmin.jsmin(content)
        except Exception as e:
            print(f"Error minifying JS: {e}")
            return content

    def _check_and_remove_corrupted_fonts_in_css(self, css: str, base_url: str) -> str:
        """Proactively check font URLs in CSS and detect corrupted ones.
        
        This checks font files referenced in CSS to see if they're HTML error pages,
        even before they're queued for download.

        Each unique font URL is fetched at most once across all CSS files: after the
        first pre-check (corrupted or not) the normalized URL is recorded in
        ``self._font_prefetch_checked`` so subsequent CSS files that reference the
        same font skip the redundant download.  Without this guard the same large
        font/SVG file could be re-downloaded once per CSS file that mentions it —
        producing the "same file status=ok 5-6×" pattern.
        """
        # Find all font URLs in CSS
        font_url_pattern = r'url\s*\(\s*["\']?([^"\']*\.(?:woff|woff2|ttf|eot|otf|svg))["\']?\s*\)'
        font_urls = re.findall(font_url_pattern, css, re.IGNORECASE)
        
        for font_url in font_urls:
            # Convert relative URLs to absolute
            if not font_url.startswith(('http://', 'https://')):
                # Try to construct absolute URL
                if font_url.startswith('/'):
                    # Absolute path from domain
                    font_url = f"{self.config.base_url.rstrip('/')}{font_url}"
                else:
                    # Relative path
                    font_url = urljoin(base_url, font_url)
            
            # Normalize URL
            normalized_font_url = self._normalize_url(font_url, base_url)
            
            # Skip if already corrupted or already reserved for checking (atomic).
            # The URL is added to _font_prefetch_checked inside the SAME lock
            # acquisition as the membership test, so no concurrent worker can
            # slip past the check before the reservation is recorded.
            with self._lock:
                if (
                    normalized_font_url in self.corrupted_fonts
                    or normalized_font_url in self._font_prefetch_checked
                ):
                    continue
                # Reserve this URL; other workers will see it and skip.
                self._font_prefetch_checked.add(normalized_font_url)

            # Try to download and check if corrupted (with quick timeout).
            try:
                wayback_url = self._convert_to_wayback_url_with_timestamp(font_url)
                response = self.session.get(wayback_url, timeout=5, allow_redirects=True)
                if response.status_code == 200:
                    if self._is_corrupted_font(response.content, font_url):
                        self._mark_corrupted_font(normalized_font_url)
                        print(f"         ⚠️  Detected corrupted font in CSS: {os.path.basename(font_url)}", flush=True)
            except Exception:
                # On network failure, un-reserve so a later CSS file can retry.
                # Don't print errors here to avoid spam.
                with self._lock:
                    self._font_prefetch_checked.discard(normalized_font_url)
        
        return css
    
    def _remove_corrupted_fonts_from_css(self, css: str) -> str:
        """Remove references to corrupted font files from CSS.
        
        This prevents browsers from trying to load HTML error pages as fonts,
        which can break typography.
        """
        # Snapshot the set under the lock: concurrent workers add to
        # corrupted_fonts via _mark_corrupted_font, and iterating it live
        # would risk "set changed size during iteration".
        with self._lock:
            corrupted_list = list(self.corrupted_fonts)
        if not corrupted_list:
            return css

        # For each corrupted font, remove its references from CSS.
        for corrupted_font_url in corrupted_list:
            # Extract just the filename from the URL
            parsed = urlparse(corrupted_font_url)
            font_filename = os.path.basename(parsed.path)
            # Also get the path relative to domain (for matching in CSS)
            font_path = parsed.path.lstrip('/')
            
            if not font_filename:
                continue
            
            # Remove url() references to this font file
            # CSS might have relative paths like /templates/.../fontname.ext
            # We need to match the path as it appears in CSS (usually relative to root)
            # The font_path is like "templates/shaper_fixter/fonts/fa-brands-400.eot"
            # But CSS might have "/templates/shaper_fixter/fonts/fa-brands-400.eot"
            
            # Try matching with leading slash
            css_path_with_slash = '/' + font_path
            # Try matching without leading slash (already handled by font_path)
            
            patterns = [
                # Match full path with leading slash: url(/templates/.../fontname.ext)
                rf'url\s*\(\s*["\']?[^"\']*{re.escape(css_path_with_slash)}["\']?\s*\)',
                # Match full path without leading slash
                rf'url\s*\(\s*["\']?[^"\']*{re.escape(font_path)}["\']?\s*\)',
                # Match just filename: url(...fontname.ext)
                rf'url\s*\(\s*["\']?[^"\']*{re.escape(font_filename)}["\']?\s*\)',
                # Match with format: url(...fontname.ext) format("...")
                rf'url\s*\(\s*["\']?[^"\']*{re.escape(font_filename)}["\']?\s*\)\s+format\s*\([^)]+\)',
                # Match with format and full path (with slash)
                rf'url\s*\(\s*["\']?[^"\']*{re.escape(css_path_with_slash)}["\']?\s*\)\s+format\s*\([^)]+\)',
                # Match with format and full path (without slash)
                rf'url\s*\(\s*["\']?[^"\']*{re.escape(font_path)}["\']?\s*\)\s+format\s*\([^)]+\)',
            ]
            
            for pattern in patterns:
                css = re.sub(pattern, '', css, flags=re.IGNORECASE)
            
            # Also remove standalone src:url(...fontname.ext); lines
            css = re.sub(rf'src:\s*url\s*\(\s*["\']?[^"\']*{re.escape(font_filename)}["\']?\s*\)\s*;', '', css, flags=re.IGNORECASE)
            css = re.sub(rf'src:\s*url\s*\(\s*["\']?[^"\']*{re.escape(css_path_with_slash)}["\']?\s*\)\s*;', '', css, flags=re.IGNORECASE)
            css = re.sub(rf'src:\s*url\s*\(\s*["\']?[^"\']*{re.escape(font_path)}["\']?\s*\)\s*;', '', css, flags=re.IGNORECASE)
        
        # Clean up any double commas or trailing commas
        css = re.sub(r',\s*,+', ',', css)  # Multiple commas
        css = re.sub(r',\s*}', '}', css)  # Trailing comma before }
        css = re.sub(r'src:\s*,', 'src:', css)  # src: with leading comma
        css = re.sub(r'src:\s*;', '', css)  # Empty src:;
        
        return css
    
    def _remove_legacy_font_formats_from_css(self, css: str) -> str:
        """Remove .eot and .svg font format references from CSS.
        
        These legacy formats are often corrupted (HTML error pages) in Wayback Machine,
        and modern browsers don't need them - they'll use .woff2, .woff, and .ttf.
        """
        # Remove .eot references (with or without format)
        css = re.sub(r',\s*url\s*\(\s*["\']?[^"\']*\.eot["\']?\s*\)\s*(?:format\s*\([^)]+\))?', '', css, flags=re.IGNORECASE)
        css = re.sub(r'url\s*\(\s*["\']?[^"\']*\.eot["\']?\s*\)\s*(?:format\s*\([^)]+\))?', '', css, flags=re.IGNORECASE)
        css = re.sub(r'src:\s*url\s*\(\s*["\']?[^"\']*\.eot["\']?\s*\)\s*;', '', css, flags=re.IGNORECASE)
        
        # Remove .svg font format references (but keep .svg images)
        # Only remove if it's in a font context (has format("svg") or in @font-face)
        css = re.sub(r',\s*url\s*\(\s*["\']?[^"\']*\.svg["\']?\s*\)\s+format\s*\(["\']?svg["\']?\)', '', css, flags=re.IGNORECASE)
        css = re.sub(r'url\s*\(\s*["\']?[^"\']*\.svg["\']?\s*\)\s+format\s*\(["\']?svg["\']?\)', '', css, flags=re.IGNORECASE)
        
        # Clean up any double commas or trailing commas
        css = re.sub(r',\s*,+', ',', css)  # Multiple commas
        css = re.sub(r',\s*}', '}', css)  # Trailing comma before }
        css = re.sub(r'src:\s*,', 'src:', css)  # src: with leading comma
        css = re.sub(r'src:\s*;', '', css)  # Empty src:;
        
        return css
    
    def _minify_css(self, content: str) -> str:
        """Minify CSS."""
        if not self.config.minify_css:
            return content

        try:
            import cssmin

            return cssmin.cssmin(content)
        except Exception as e:
            print(f"Error minifying CSS: {e}")
            return content

    def _extract_css_urls(self, css: str, base_url: str) -> List[str]:
        """Extract URLs from CSS content."""
        urls = []
        
        # Extract @import URLs
        import_pattern = r'@import\s+(?:url\()?["\']?([^"\'()]+)["\']?\)?'
        for match in re.finditer(import_pattern, css, re.IGNORECASE):
            import_url = match.group(1).strip()
            # Extract from wayback URLs
            original = self._extract_original_url_from_path(import_url)
            if original:
                import_url = original
            normalized = self._normalize_url(import_url, base_url)
            if normalized not in urls:
                urls.append(normalized)
        
        # Extract url() references (images, fonts, etc.)
        url_pattern = r'url\s*\(\s*["\']?([^"\'()]+)["\']?\s*\)'
        for match in re.finditer(url_pattern, css, re.IGNORECASE):
            css_url = match.group(1).strip()
            # Skip data URIs and special protocols
            if not css_url.startswith(("data:", "javascript:", "vbscript:", "#")):
                # Extract from wayback URLs
                original = self._extract_original_url_from_path(css_url)
                if original:
                    css_url = original
                # Convert relative paths to absolute URLs using base_url
                # This is critical for font files referenced with relative paths in CSS
                if css_url.startswith("/") and not css_url.startswith("//"):
                    # Absolute path from domain root - construct full URL
                    from urllib.parse import urljoin
                    parsed_base = urlparse(base_url)
                    css_url = f"{parsed_base.scheme}://{parsed_base.netloc}{css_url}"
                normalized = self._normalize_url(css_url, base_url)
                if normalized not in urls:
                    urls.append(normalized)
        
        return urls

    def _rewrite_css_urls(self, css: str, base_url: str) -> str:
        """Rewrite URLs in CSS to relative paths."""
        def replace_css_url(match):
            full_match = match.group(0)
            url_part = match.group(1)
            
            # Extract original URL from wayback path
            original = self._extract_original_url_from_path(url_part)
            if original:
                url_part = original
            
            # Handle absolute paths starting with / in Google Fonts CSS files
            # These are relative to fonts.gstatic.com, not the site's domain
            if url_part.startswith("/") and not url_part.startswith("//"):
                # Check if this is a Google Fonts CSS file (base_url contains fonts.googleapis.com)
                if "fonts.googleapis.com" in base_url:
                    # Convert to full Google Fonts URL
                    url_part = f"https://fonts.gstatic.com{url_part}"
                else:
                    # Regular absolute path - convert using base_url
                    parsed_base = urlparse(base_url)
                    url_part = f"{parsed_base.scheme}://{parsed_base.netloc}{url_part}"
            
            normalized = self._normalize_url(url_part, base_url)
            
            # Handle fonts.gstatic.com URLs - these need to be converted to local paths
            # to avoid CORS issues when loading from localhost
            is_google_font = "fonts.gstatic.com" in normalized or "fonts.googleapis.com" in normalized
            is_squarespace_cdn = self._is_squarespace_cdn(normalized)
            if self._is_internal_url(normalized) or is_google_font or is_squarespace_cdn:
                if self.config.make_internal_links_relative:
                    # For Google Fonts, construct relative path from the normalized URL
                    if is_google_font:
                        # Construct path directly from URL to avoid path duplication
                        parsed_font = urlparse(normalized)
                        if "fonts.gstatic.com" in parsed_font.netloc:
                            # Path will be like fonts.gstatic.com/s/montserrat/v29/...
                            # Check if path already contains the domain (avoid duplication)
                            font_path = parsed_font.path.lstrip("/")
                            if font_path.startswith("fonts.gstatic.com"):
                                relative_path = font_path
                            else:
                                relative_path = f"{parsed_font.netloc}/{font_path}"
                        elif "fonts.googleapis.com" in parsed_font.netloc:
                            # For Google Fonts CSS files
                            relative_path = parsed_font.path.lstrip("/")
                        else:
                            relative_path = parsed_font.path.lstrip("/")
                        # Ensure it starts with / for absolute paths
                        if not relative_path.startswith("/"):
                            relative_path = "/" + relative_path
                        new_path = relative_path
                    else:
                        new_path = self._make_relative_path(normalized)
                    return f"url({new_path})"
                return f"url({normalized})"
            
            # If it's a Squarespace CDN URL, still rewrite it to local path
            if is_squarespace_cdn:
                parsed_resource = urlparse(normalized)
                resource_path = f"{parsed_resource.netloc}{parsed_resource.path}"
                # Remove leading slashes
                while resource_path.startswith("/"):
                    resource_path = resource_path[1:]
                if self.config.make_internal_links_relative:
                    return f"url(/{resource_path})"
                return f"url({normalized})"
            
            return full_match
        
        # Pattern to match url() with wayback URLs and absolute paths
        url_patterns = [
            r'url\s*\(\s*["\']?(https?://web\.archive\.org/web/\d+[a-z]*(?:im_|cs_|js_|jm_)/https?://[^"\'()]+)["\']?\s*\)',  # Absolute wayback (check first)
            r'url\s*\(\s*["\']?(/web/\d+[a-z]*(?:im_|cs_|js_|jm_)/https?://[^"\'()]+)["\']?\s*\)',  # Relative wayback
            r'url\s*\(\s*["\']?(https?://[^"\'()]+)["\']?\s*\)',  # Regular URLs
            r'url\s*\(\s*["\']?(/[^"\'()]+)["\']?\s*\)',  # Absolute paths (for Google Fonts CSS)
        ]
        
        for pattern in url_patterns:
            css = re.sub(pattern, replace_css_url, css, flags=re.IGNORECASE)
        
        return css

    def _extract_js_urls(self, js: str, base_url: str) -> List[str]:
        """Extract URLs from JavaScript content."""
        urls = []
        
        # More specific patterns to avoid false positives (like code snippets)
        patterns = [
            r'(?:fetch|XMLHttpRequest|axios\.get|axios\.post|\.load|\.ajax)\s*\(\s*["\']([^"\']+)["\']',  # Fetch/ajax calls
            r'\.src\s*=\s*["\']([^"\']+)["\']',  # src assignments
            r'\.href\s*=\s*["\']([^"\']+)["\']',  # href assignments
            r'url\s*[:=]\s*["\'](https?://[^"\']+)["\']',  # URL properties
            r'["\'](https?://[^"\']+\.(?:jpg|jpeg|png|gif|svg|webp|css|js|woff|woff2|ttf|eot|otf)[^"\']*)["\']',  # Asset URLs
        ]
        
        for pattern in patterns:
            for match in re.finditer(pattern, js):
                js_url = match.group(1).strip()
                # Skip if it looks like code, not a URL
                if any(skip in js_url for skip in ["function", "return", "if", "else", "var ", "let ", "const "]):
                    continue
                if not js_url.startswith(("data:", "javascript:", "vbscript:", "#", "mailto:", "tel:", "//", "http", "https")):
                    continue
                if not js_url.startswith(("http://", "https://", "/")):
                    continue
                    
                original = self._extract_original_url_from_path(js_url)
                if original:
                    js_url = original
                normalized = self._normalize_url(js_url, base_url)
                if normalized not in urls and self._is_internal_url(normalized):
                    urls.append(normalized)
        
        return urls

    def _optimize_image(self, content: bytes, format: str = "JPEG") -> bytes:
        """Optimize image."""
        if not self.config.optimize_images:
            return content

        try:
            from PIL import Image
            from io import BytesIO

            img = Image.open(BytesIO(content))
            
            # Convert RGBA to RGB for JPEG
            if format.upper() == "JPEG" and img.mode == "RGBA":
                background = Image.new("RGB", img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[3])
                img = background
            elif img.mode not in ("RGB", "L"):
                img = img.convert("RGB")

            output = BytesIO()
            img.save(output, format=format, optimize=True, quality=85)
            return output.getvalue()
        except Exception as e:
            print(f"Error optimizing image: {e}")
            return content

    def _process_html(self, html: str, base_url: str) -> tuple[str, List[str]]:
        """Process HTML content and extract links."""
        self._current_page_url = base_url
        soup = BeautifulSoup(html, "lxml")
        links_to_follow: List[str] = []

        # Remove Wayback Machine banner, scripts, and styles
        elements_to_remove = []
        for element in soup.find_all(["iframe", "div", "script", "link"], id=True):
            if element is None:
                continue
            try:
                element_id = element.get("id")
                if element_id and any(banner_id in str(element_id).lower() for banner_id in ["wm-ipp", "wm-bipp", "wm-toolbar", "wm-ipp-base"]):
                    elements_to_remove.append(element)
            except (AttributeError, TypeError):
                continue
        for element in elements_to_remove:
            element.decompose()
        
        # Remove wayback machine script tags by src
        # But preserve cookie consent scripts (cookieyes, etc.) even if they come from external CDNs
        for script in soup.find_all("script", src=True):
            src = script.get("src", "")
            # Preserve cookie consent scripts
            if "cookieyes" in src.lower() or "cookie-consent" in src.lower():
                continue
            if "web.archive.org" in src or "web-static.archive.org" in src or "bundle-playback.js" in src or "wombat.js" in src or "ruffle.js" in src:
                script.decompose()
        
        # Remove wayback machine link tags by href (but keep internal links that need processing)
        for link in soup.find_all("link", href=True):
            if link is None:
                continue
            href = link.get("href", "")
            if not href:
                continue
            # Only remove wayback machine banner/styles, not internal assets that need processing
            if "banner-styles.css" in href or "iconochive.css" in href or "web-static.archive.org" in href:
                link.decompose()
            # For /web/ paths, we'll process them below, don't remove here
        
        # Remove wayback-specific meta tags and scripts
        for meta in soup.find_all("meta"):
            if meta is None:
                continue
            meta_property = meta.get("property")
            meta_content = meta.get("content", "")
            if meta_property == "og:url" and meta_content and "web.archive.org" in str(meta_content):
                meta.decompose()
        
        # Remove inline wayback scripts (__wm, __wm.wombat, RufflePlayer)
        for script in soup.find_all("script"):
            if script.string:
                script_content = script.string
                if any(pattern in script_content for pattern in ["__wm", "wombat", "RufflePlayer", "web.archive.org"]):
                    script.decompose()
        
        # Add Static object stub if needed (for Squarespace sites)
        # Check if any script references Static but it's not defined
        needs_static_stub = False
        for script in soup.find_all("script"):
            if script.string and ("Static." in script.string or "window.Static" in script.string):
                needs_static_stub = True
                break
        
        if needs_static_stub:
            # Find the first script tag and add stub before it, or add after SQUARESPACE_ROLLUPS if present
            first_script = soup.find("script")
            if first_script:
                static_stub = soup.new_string("\n")
                static_script = soup.new_tag("script")
                static_script.string = "window.Static = window.Static || {}; window.Static.SQUARESPACE_CONTEXT = window.Static.SQUARESPACE_CONTEXT || { showAnnouncementBar: false };"
                # Try to insert after SQUARESPACE_ROLLUPS script if it exists
                rollups_script = None
                for script in soup.find_all("script"):
                    if script.string and "SQUARESPACE_ROLLUPS" in script.string:
                        rollups_script = script
                        break
                if rollups_script:
                    rollups_script.insert_after(static_script)
                else:
                    first_script.insert_before(static_script)
        
        # Remove comments
        for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()

        # Remove trackers and analytics
        if self.config.remove_trackers:
            for script in soup.find_all("script", src=True):
                if script is None:
                    continue
                script_src = script.get("src")
                if script_src and self._is_tracker(script_src):
                    script.decompose()

            # Remove inline tracking scripts (Google Analytics, gtag, dataLayer)
            # Note: Cookie consent scripts (like cookieyes) are preserved as they're part of site functionality
            for script in soup.find_all("script"):
                if script.string:
                    script_text = script.string.lower()
                    # Only remove tracking scripts, not cookie consent functionality
                    if any(pattern in script_text for pattern in self.TRACKER_PATTERNS + [
                        "gtag", "datalayer", "google-analytics"
                    ]):
                        # Skip cookieyes and cookie consent scripts - preserve them
                        if "cookieyes" not in script_text and "cookie consent" not in script_text:
                            script.decompose()
            
            # Note: Cookie popups and consent UI are preserved - they're part of site functionality

        # Remove ads
        if self.config.remove_ads:
            for element in soup.find_all(["script", "iframe", "img"], src=True):
                if self._is_ad(element["src"]):
                    element.decompose()

        # Remove external iframes
        if self.config.remove_external_iframes:
            for iframe in soup.find_all("iframe", src=True):
                if not self._is_internal_url(iframe["src"]):
                    iframe.decompose()

        # Process frames (<frame src="...">) and internal iframes
        # Frame-based pages (using <frameset>/<frame>) won't render without their frame content
        for frame in soup.find_all(["frame", "iframe"], src=True):
            src = frame.get("src", "")
            if not src:
                continue
            # Extract wayback URL first
            original = self._extract_original_url_from_path(src)
            if original:
                src = original
            # Keep original URL with query strings for downloading
            original_url = src
            # Normalize for checking and final output
            normalized_url = self._normalize_url(src, base_url)

            if self._is_internal_url(normalized_url):
                if self.config.make_internal_links_relative:
                    # Frame content is HTML pages
                    frame["src"] = self._get_relative_link_path(normalized_url, is_page=True)
                else:
                    frame["src"] = normalized_url

                if normalized_url not in self.config.visited_urls:
                    links_to_follow.append(original_url)

        # Process links
        for link in soup.find_all("a", href=True):
            if link is None:
                continue
            href = link.get("href", "")
            if not href:
                continue
            
            # Check if this link is inside a floating buttons container BEFORE processing
            is_floating_button = False
            parent_classes = []
            parent = link.find_parent()
            while parent and parent.name:  # parent.name checks if it's a valid tag
                parent_class = parent.get("class")
                if parent_class:
                    if isinstance(parent_class, list):
                        parent_classes.extend(parent_class)
                    else:
                        parent_classes.append(str(parent_class))
                parent_id = parent.get("id", "")
                if parent_id and "sp-footeredu" in str(parent_id):
                    is_floating_button = True
                parent = parent.find_parent()
            
            is_floating_button = is_floating_button or any("botonesflotantes" in str(cls).lower() for cls in parent_classes)
            
            # For floating button links, preserve them as-is (don't process wayback URLs)
            if is_floating_button:
                # Extract wayback URL from href if present, but preserve tel:/mailto: protocols
                if href.startswith("https://web.archive.org/web/") or href.startswith("http://web.archive.org/web/") or href.startswith("/web/"):
                    # Extract protocol-relative URL from wayback path (e.g., /web/TIMESTAMP/tel:xxx)
                    wayback_protocol_pattern = r"/web/\d+[a-z]*/(tel:|mailto:|whatsapp:)(.+)"
                    match = re.search(wayback_protocol_pattern, href)
                    if match:
                        protocol = match.group(1)
                        path = match.group(2)
                        # Remove query params if present in the path
                        if "?" in path:
                            path = path.split("?")[0]
                        href = protocol + path
                        link["href"] = href
                    else:
                        # Check if it's a direct mailto: link in wayback URL
                        # Handle both relative (/web/TIMESTAMP/mailto:...) and absolute (https://web.archive.org/web/TIMESTAMP/mailto:...)
                        mailto_direct_patterns = [
                            r"/web/\d+[a-z]*/(mailto:[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",
                            r"https?://web\.archive\.org/web/\d+[a-z]*/(mailto:[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})"
                        ]
                        mailto_extracted = False
                        for pattern in mailto_direct_patterns:
                            mailto_direct_match = re.search(pattern, href)
                            if mailto_direct_match:
                                href = mailto_direct_match.group(1)
                                link["href"] = href
                                mailto_extracted = True
                                break
                        
                        if not mailto_extracted:
                            # Check if it's an email address hidden in an https:// URL
                            # Pattern: /web/TIMESTAMP/https://domain.com/email@domain.com
                            mailto_pattern = r"/web/\d+[a-z]*/https?://[^/]+/([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})"
                            mailto_match = re.search(mailto_pattern, href)
                            if mailto_match:
                                email = mailto_match.group(1)
                                href = f"mailto:{email}"
                                link["href"] = href
                            else:
                                # Try regular extraction
                                original = self._extract_original_url_from_path(href)
                                if original:
                                    # Check if extracted URL looks like an email address (domain.com/email@domain.com -> mailto:)
                                    if "@" in original and "/" in original and not original.startswith("mailto:"):
                                        email_part = original.split("/")[-1]
                                        if "@" in email_part:
                                            href = f"mailto:{email_part}"
                                            link["href"] = href
                                    else:
                                        href = original
                                        link["href"] = href
                # Skip further processing for floating buttons - preserve them
                continue
            
            # Extract wayback URL first (for non-floating-button links)
            original = self._extract_original_url_from_path(href)
            if original:
                href = original
            # Keep original URL with query strings for downloading - normalize later for file paths
            original_url = href
            # Normalize only for checking if internal/external
            parsed_original = urlparse(original_url)
            normalized_for_check = parsed_original._replace(fragment="", query="").geturl()
            # Check if internal using normalized version
            is_internal = self._is_internal_url(normalized_for_check)

            # Handle contact links (but preserve floating buttons and icon groups - already handled above)
            # Check if link is in an icon group before removing contact links
            parent = link.parent
            is_in_icon_group = False
            while parent is not None:
                parent_classes = parent.get("class", [])
                if parent_classes:
                    if isinstance(parent_classes, list):
                        parent_classes_str = " ".join(parent_classes)
                    else:
                        parent_classes_str = str(parent_classes)
                    if "sppb-icons-group-list" in parent_classes_str or "icons-group" in parent_classes_str.lower():
                        is_in_icon_group = True
                        break
                parent = parent.parent if hasattr(parent, 'parent') else None
            
            if self.config.remove_clickable_contacts and self._is_contact_link(original_url) and not is_floating_button and not is_in_icon_group:
                if self.config.remove_external_links_remove_anchors:
                    link.decompose()
                else:
                    link["href"] = "#"
                continue

            # Handle external links (but preserve floating button contact links and contact links when not removing them)
            if not is_internal:
                # Preserve contact links (tel:, mailto:) when remove_clickable_contacts is False
                if self._is_contact_link(original_url) and not self.config.remove_clickable_contacts:
                    # Update href to the extracted URL (removes wayback prefix)
                    link["href"] = original_url
                    continue
                
                # Don't remove/modify contact links in floating buttons
                if is_floating_button and self._is_contact_link(original_url):
                    # Keep the original href for floating button contact links
                    continue
                
                # Preserve button links (sppb-btn classes) - these are styled buttons that should remain functional
                link_classes = link.get("class", [])
                if link_classes and isinstance(link_classes, list):
                    link_classes_str = " ".join(link_classes)
                elif link_classes:
                    link_classes_str = str(link_classes)
                else:
                    link_classes_str = ""
                
                is_button_link = "sppb-btn" in link_classes_str or "btn" in link_classes_str
                if is_button_link:
                    # Preserve button links - just clean up the href (remove wayback prefix)
                    link["href"] = original_url
                    continue
                
                # Preserve links in icon groups (sppb-icons-group-list) - these are social media icons
                # Check if the link is inside an icon group list
                parent = link.parent
                is_in_icon_group = False
                while parent is not None:
                    parent_classes = parent.get("class", [])
                    if parent_classes:
                        if isinstance(parent_classes, list):
                            parent_classes_str = " ".join(parent_classes)
                        else:
                            parent_classes_str = str(parent_classes)
                        if "sppb-icons-group-list" in parent_classes_str or "icons-group" in parent_classes_str.lower():
                            is_in_icon_group = True
                            break
                    parent = parent.parent if hasattr(parent, 'parent') else None
                
                if is_in_icon_group:
                    # Preserve icon group links - just clean up the href (remove wayback prefix)
                    link["href"] = original_url
                    continue
                
                if self.config.remove_external_links_remove_anchors:
                    link.decompose()
                elif self.config.remove_external_links_keep_anchors:
                    link["href"] = "#"
                    # Keep the text but remove link
                    text = link.get_text()
                    link.replace_with(text)
                continue

            # Process internal links - normalize for final HTML output
            normalized_url = self._normalize_url(original_url, base_url)
            if self.config.make_internal_links_relative:
                # Use _get_relative_link_path to ensure links match saved file paths
                relative_path = self._get_relative_link_path(normalized_url, is_page=True)
                link["href"] = relative_path
            else:
                if self.config.make_non_www or self.config.make_www:
                    link["href"] = normalized_url

            # Add to links to follow - use original URL with query strings for downloading
            # Track by normalized URL to avoid downloading same file multiple times
            if normalized_url not in self.config.visited_urls:
                links_to_follow.append(original_url)

        # Process images
        for img in soup.find_all("img", src=True):
            src = img["src"]
            # Keep original src before processing (for Squarespace CDN detection)
            original_src = src
            # Extract wayback URL first
            original = self._extract_original_url_from_path(src)
            if original:
                src = original
            # Keep original URL with query strings for downloading
            original_url = src
            # Normalize for checking and final output
            normalized_url = self._normalize_url(src, base_url)

            # Check if it's Squarespace CDN (should be downloaded even though external)
            is_squarespace_cdn = self._is_squarespace_cdn(normalized_url) or self._is_squarespace_cdn(original_src)

            if self._is_internal_url(normalized_url) or is_squarespace_cdn:
                if self.config.make_internal_links_relative:
                    # Images are assets, don't add .html extension
                    if is_squarespace_cdn:
                        # For Squarespace CDN, preserve domain structure
                        parsed_img = urlparse(normalized_url)
                        img_path = f"{parsed_img.netloc}{parsed_img.path}"
                        # Remove leading slashes
                        while img_path.startswith("/"):
                            img_path = img_path[1:]
                        img["src"] = self._to_relative_path(f"/{img_path}")
                    else:
                        img["src"] = self._get_relative_link_path(normalized_url, is_page=False)
                else:
                    img["src"] = normalized_url

                if normalized_url not in self.config.visited_urls:
                    links_to_follow.append(original_url)

        # Process HTML background attributes (legacy <body>, <table>, <td>, <tr>, <th>)
        for elem in soup.find_all(["body", "table", "td", "tr", "th"], attrs={"background": True}):
            bg = elem.get("background", "")
            if not bg:
                continue
            original = self._extract_original_url_from_path(bg)
            if original:
                bg = original
            original_url = bg
            normalized_url = self._normalize_url(bg, base_url)
            if self._is_internal_url(normalized_url):
                if self.config.make_internal_links_relative:
                    elem["background"] = self._get_relative_link_path(normalized_url, is_page=False)
                else:
                    elem["background"] = normalized_url
                if normalized_url not in self.config.visited_urls:
                    links_to_follow.append(original_url)

        # Process picture/source tags for responsive images
        for picture in soup.find_all("picture"):
            for source in picture.find_all("source", srcset=True):
                srcset = source.get("srcset", "")
                if not srcset:
                    continue
                # Rewrite srcset URLs - handle wayback URLs and convert to local paths
                # Parse srcset manually (format: "url1 100w, url2 200w" or "url1 1x, url2 2x")
                srcset_parts = []
                for item in srcset.split(','):
                    item = item.strip()
                    if not item:
                        continue
                    # Split URL and descriptor (e.g., "url 500w" or "url?format=100w 100w")
                    # Descriptor is at the end: space followed by number and 'w' or 'x'
                    parts = re.split(r'\s+(\d+(?:\.\d+)?[xw])$', item, maxsplit=1)
                    if len(parts) == 3:
                        url_part, descriptor, _ = parts
                        descriptor = f" {descriptor}"
                    else:
                        url_part = item
                        descriptor = ""
                    
                    original_srcset = url_part
                    # Extract wayback URL if present
                    original = self._extract_original_url_from_path(url_part)
                    if not original and "web.archive.org" in url_part:
                        # Try to extract from absolute wayback URL - match the full URL including query strings
                        wayback_match = re.search(r'/web/\d+[a-z]*(?:im_|cs_|js_|jm_)/(https?://[^\s"\'<>\)]+)', url_part)
                        if wayback_match:
                            original = wayback_match.group(1)
                    
                    if original:
                        url_part = original
                    
                    normalized_srcset = self._normalize_url(url_part, base_url)
                    is_squarespace_cdn = self._is_squarespace_cdn(normalized_srcset) or self._is_squarespace_cdn(original_srcset)
                    
                    # Queue for download if internal or Squarespace CDN
                    if (self._is_internal_url(normalized_srcset) or is_squarespace_cdn) and normalized_srcset not in self.config.visited_urls:
                        links_to_follow.append(url_part)
                    
                    # Rewrite to local path
                    if self._is_internal_url(normalized_srcset) or is_squarespace_cdn:
                        if is_squarespace_cdn:
                            parsed_resource = urlparse(normalized_srcset)
                            resource_path = f"{parsed_resource.netloc}{parsed_resource.path}"
                            # Preserve query string if present
                            if parsed_resource.query:
                                resource_path += "?" + parsed_resource.query
                            while resource_path.startswith("/"):
                                resource_path = resource_path[1:]
                            if self.config.make_internal_links_relative:
                                srcset_parts.append(f"{self._to_relative_path(f'/{resource_path}')}{descriptor}")
                            else:
                                srcset_parts.append(f"{normalized_srcset}{descriptor}")
                        else:
                            relative_path = self._get_relative_link_path(normalized_srcset, is_page=False)
                            srcset_parts.append(f"{relative_path}{descriptor}")
                    else:
                        # Keep external URLs as-is
                        srcset_parts.append(item)
                
                if srcset_parts:
                    source["srcset"] = ", ".join(srcset_parts)
                
            # Also process img inside picture
            for img in picture.find_all("img", src=True):
                src = img.get("src", "")
                original_src = src
                original = self._extract_original_url_from_path(src)
                if original:
                    src = original
                normalized_url = self._normalize_url(src, base_url)
                is_squarespace_cdn = self._is_squarespace_cdn(normalized_url) or self._is_squarespace_cdn(original_src)
                if (self._is_internal_url(normalized_url) or is_squarespace_cdn) and normalized_url not in self.config.visited_urls:
                    links_to_follow.append(src)
                # Rewrite img src in picture tags
                if self._is_internal_url(normalized_url) or is_squarespace_cdn:
                    if self.config.make_internal_links_relative:
                        if is_squarespace_cdn:
                            parsed_img = urlparse(normalized_url)
                            img_path = f"{parsed_img.netloc}{parsed_img.path}"
                            while img_path.startswith("/"):
                                img_path = img_path[1:]
                            img["src"] = self._to_relative_path(f"/{img_path}")
                        else:
                            img["src"] = self._get_relative_link_path(normalized_url, is_page=False)
                    else:
                        img["src"] = normalized_url

        # Process CSS links
        for link in soup.find_all("link", rel="stylesheet", href=True):
            href = link.get("href", "")
            if not href:
                continue
            # Keep original href before processing (for Google Fonts detection)
            original_href = href
            # Extract wayback URL first
            original = self._extract_original_url_from_path(href)
            if original:
                href = original
            # Keep original URL with query strings for downloading
            original_url = href
            # Normalize for checking and final output
            normalized_url = self._normalize_url(href, base_url)

            # Handle external links (e.g., Google Fonts, Squarespace CDN)
            # For Google Fonts and Squarespace CDN files available on Wayback Machine, download them
            # to ensure fonts and styles load correctly locally
            if not self._is_internal_url(normalized_url):
                # Check if this is a Google Fonts CSS file available on Wayback Machine
                # The original_href might be a wayback path like //web.archive.org/web/...cs_/http://fonts.googleapis.com/...
                is_google_font = "fonts.googleapis.com" in normalized_url or "fonts.googleapis.com" in original_href
                is_squarespace_cdn = self._is_squarespace_cdn(normalized_url) or self._is_squarespace_cdn(original_href)
                
                if is_google_font or is_squarespace_cdn:
                    # Extract original URL from wayback path if present (use original_href which has the wayback path)
                    original_resource_url = self._extract_original_url_from_path(original_href)
                    if not original_resource_url:
                        # If extraction failed, try using the already-extracted href
                        original_resource_url = href if (is_google_font and "fonts.googleapis.com" in href) or (is_squarespace_cdn and self._is_squarespace_cdn(href)) else None
                    if original_resource_url:
                        # Normalize for tracking (remove query strings for visited check)
                        parsed_resource = urlparse(original_resource_url)
                        normalized_resource = parsed_resource._replace(fragment="", query="").geturl()
                        # Add to queue to download from Wayback Machine
                        if normalized_resource not in self.config.visited_urls:
                            links_to_follow.append(original_resource_url)
                            resource_type = "Google Fonts CSS" if is_google_font else "Squarespace CDN"
                            print(f"         📥 Queued {resource_type} for download: {original_resource_url[:80]}...", flush=True)
                        # Convert to local path immediately so HTML references local file
                        # Use _get_local_path to determine where the file will be saved
                        if is_google_font:
                            # For Google Fonts, create a path like /fonts.googleapis.com/css.css
                            query_hash = hashlib.md5(parsed_resource.query.encode()).hexdigest()[:8]
                            resource_path = f"fonts.googleapis.com/css-{query_hash}.css"
                        else:
                            # For Squarespace CDN, preserve domain structure
                            resource_path = f"{parsed_resource.netloc}{parsed_resource.path}"
                            # Remove leading slashes
                            while resource_path.startswith("/"):
                                resource_path = resource_path[1:]
                        local_resource_path = self._get_local_path(f"http://{resource_path}")
                        # Get relative path for HTML
                        if self.config.make_internal_links_relative:
                            relative_path = self._get_relative_link_path(f"http://{resource_path}", is_page=False)
                            link["href"] = relative_path
                        else:
                            link["href"] = self._to_relative_path(f"/{resource_path}")
                        continue
                
                # Remove external links if configured
                if self.config.remove_external_links_remove_anchors:
                    link.decompose()
                elif self.config.remove_external_links_keep_anchors:
                    # Keep but remove wayback URLs - convert to direct external URL
                    link["href"] = normalized_url if normalized_url.startswith(("http://", "https://")) else href
                continue

            if self.config.make_internal_links_relative:
                # CSS files are assets, preserve extension
                link["href"] = self._get_relative_link_path(normalized_url, is_page=False)
            else:
                link["href"] = normalized_url

            if normalized_url not in self.config.visited_urls:
                links_to_follow.append(original_url)

        # Process script tags
        for script in soup.find_all("script", src=True):
            if script is None:
                continue
            src = script.get("src", "")
            if not src:
                continue
            # Extract wayback URL first
            original = self._extract_original_url_from_path(src)
            if original:
                src = original
            # Keep original URL with query strings for downloading
            original_url = src
            # Normalize for checking and final output
            normalized_url = self._normalize_url(src, base_url)

            if self._is_internal_url(normalized_url):
                if self.config.make_internal_links_relative:
                    # JavaScript files are assets, preserve extension
                    script["src"] = self._get_relative_link_path(normalized_url, is_page=False)
                else:
                    script["src"] = normalized_url

                if normalized_url not in self.config.visited_urls:
                    links_to_follow.append(original_url)

        # Process SVG use elements with xlink:href attributes
        for use_elem in soup.find_all("use"):
            xlink_href = use_elem.get("xlink:href") or use_elem.get("href")
            original_xlink = str(xlink_href) if xlink_href else ""
            if xlink_href:
                # Extract wayback URL if present
                original = self._extract_original_url_from_path(str(xlink_href))
                if original:
                    xlink_href = original
                # Remove wayback paths from xlink:href - just keep the fragment/anchor
                # Format: /web/20250818034506im_/https://qqnailspa.com/#email-icon -> #email-icon
                if "/web/" in original_xlink:
                    # Extract just the fragment part
                    if "#" in str(xlink_href):
                        fragment = "#" + str(xlink_href).split("#", 1)[1]
                        # Remove query params from fragment if present
                        if "?" in fragment:
                            fragment = fragment.split("?")[0]
                        use_elem["xlink:href"] = fragment
                        if use_elem.get("href"):
                            use_elem["href"] = fragment
                    elif str(xlink_href).startswith("#"):
                        # Already a fragment, just clean it
                        fragment = str(xlink_href).split("?")[0] if "?" in str(xlink_href) else str(xlink_href)
                        use_elem["xlink:href"] = fragment
                        if use_elem.get("href"):
                            use_elem["href"] = fragment

        # Process other link tags (favicon, etc.) - but skip stylesheets as they're handled above
        for link in soup.find_all("link", href=True):
            if link is None:
                continue
            link_rel = link.get("rel")
            # Skip stylesheets as they're already processed above
            if link_rel and (link_rel == ["stylesheet"] or (isinstance(link_rel, list) and "stylesheet" in link_rel)):
                continue
            href = link.get("href", "")
            if not href:
                continue
            # Extract wayback URL first
            original = self._extract_original_url_from_path(href)
            if original:
                href = original
            normalized_url = self._normalize_url(href, base_url)

            is_squarespace_cdn = self._is_squarespace_cdn(normalized_url)
            if self._is_internal_url(normalized_url) or is_squarespace_cdn:
                if self.config.make_internal_links_relative:
                    if is_squarespace_cdn:
                        parsed_asset = urlparse(normalized_url)
                        asset_path = f"{parsed_asset.netloc}{parsed_asset.path}"
                        if parsed_asset.query:
                            asset_path += "?" + parsed_asset.query
                        while asset_path.startswith("/"):
                            asset_path = asset_path[1:]
                        link["href"] = self._to_relative_path(f"/{asset_path}")
                    else:
                        link["href"] = self._make_relative_path(normalized_url)
                else:
                    link["href"] = normalized_url

                if normalized_url not in self.config.visited_urls:
                    links_to_follow.append(normalized_url)

        # Process inline styles (background-image, etc.)
        for element in soup.find_all(style=True):
            style = element["style"]
            # Extract URLs from inline styles
            style_urls = self._extract_css_urls(style, base_url)
            for style_url in style_urls:
                is_squarespace_cdn = self._is_squarespace_cdn(style_url)
                if style_url not in self.config.visited_urls and (self._is_internal_url(style_url) or is_squarespace_cdn):
                    links_to_follow.append(style_url)
            
            # Rewrite URLs in inline styles - handle url() functions
            if "web.archive.org" in style or "/web/" in style or "url(" in style:
                def replace_url_in_style(match):
                    full_match = match.group(0)
                    url_part = match.group(1) if len(match.groups()) > 0 else full_match
                    
                    # Extract original URL from wayback path
                    original = self._extract_original_url_from_path(url_part)
                    if original:
                        url_part = original
                    elif "web.archive.org" in url_part:
                        # Try extracting from absolute wayback URL
                        match_obj = re.search(r"/web/\d+[a-z]*(?:im_|cs_|js_|jm_)/(https?://[^\"\s'()]+)", url_part)
                        if match_obj:
                            url_part = match_obj.group(1)
                    
                    normalized = self._normalize_url(url_part, base_url)
                    is_squarespace_cdn = self._is_squarespace_cdn(normalized)
                    
                    if self._is_internal_url(normalized) or is_squarespace_cdn:
                        if self.config.make_internal_links_relative:
                            if is_squarespace_cdn:
                                parsed_resource = urlparse(normalized)
                                resource_path = f"{parsed_resource.netloc}{parsed_resource.path}"
                                while resource_path.startswith("/"):
                                    resource_path = resource_path[1:]
                                new_path = self._to_relative_path(f"/{resource_path}")
                            else:
                                new_path = self._make_relative_path(normalized)
                            return f"url({new_path})"
                        return f"url({normalized})"
                    
                    return full_match
                
                # Pattern for url() with wayback URLs in inline styles
                url_patterns = [
                    r'url\s*\(\s*["\']?(/web/\d+[a-z]*(?:im_|cs_|js_|jm_)/https?://[^"\'()]+)["\']?\s*\)',  # Relative wayback
                    r'url\s*\(\s*["\']?(https?://web\.archive\.org/web/\d+[a-z]*(?:im_|cs_|js_|jm_)/https?://[^"\'()]+)["\']?\s*\)',  # Absolute wayback
                    r'url\s*\(\s*["\']?(https?://web\.archive\.org/[^"\'()]+)["\']?\s*\)',  # Simple web.archive.org URL
                ]
                new_style = style
                for pattern in url_patterns:
                    new_style = re.sub(pattern, replace_url_in_style, new_style, flags=re.IGNORECASE)
                # Remove references to corrupted fonts from inline styles
                new_style = self._remove_corrupted_fonts_from_css(new_style)
                element["style"] = new_style

        # Process <style> tags in HTML (not just inline styles)
        for style_tag in soup.find_all("style"):
            if style_tag.string:
                css_content = style_tag.string
                # Extract URLs from style tag content
                style_urls = self._extract_css_urls(css_content, base_url)
                for style_url in style_urls:
                    is_squarespace_cdn = self._is_squarespace_cdn(style_url)
                    if style_url not in self.config.visited_urls and (self._is_internal_url(style_url) or is_squarespace_cdn):
                        links_to_follow.append(style_url)
                
                # Rewrite URLs in style tag CSS
                css_content = self._rewrite_css_urls(css_content, base_url)
                # Remove references to corrupted fonts
                css_content = self._remove_corrupted_fonts_from_css(css_content)
                style_tag.string = css_content

        # Process data-* attributes that contain URLs (e.g., data-video_src, data-src, data-href, etc.)
        # Convert domain URLs to relative paths to match Wayback Machine behavior
        for element in soup.find_all(True):  # All elements
            if not hasattr(element, 'attrs') or not element.attrs:
                continue
            for attr_name, attr_value in element.attrs.items():
                if attr_name.startswith('data-') and isinstance(attr_value, str):
                    # Check if attribute contains a domain URL
                    if (self.config.domain and self.config.domain in attr_value) or self._is_squarespace_cdn(attr_value):
                        # Extract original URL if it's a wayback path
                        original = self._extract_original_url_from_path(attr_value)
                        if original:
                            attr_value = original
                        
                        # Normalize and convert to relative path if internal
                        normalized = self._normalize_url(attr_value, base_url)
                        is_squarespace_cdn = self._is_squarespace_cdn(normalized)
                        if (self._is_internal_url(normalized) or is_squarespace_cdn) and self.config.make_internal_links_relative:
                            # Convert to relative path
                            if is_squarespace_cdn:
                                parsed_asset = urlparse(normalized)
                                asset_path = f"{parsed_asset.netloc}{parsed_asset.path}"
                                if parsed_asset.query:
                                    asset_path += "?" + parsed_asset.query
                                while asset_path.startswith("/"):
                                    asset_path = asset_path[1:]
                                element[attr_name] = self._to_relative_path(f"/{asset_path}")
                            else:
                                relative_path = self._get_relative_link_path(normalized, is_page=False)
                                element[attr_name] = relative_path
                        elif self._is_internal_url(normalized) or is_squarespace_cdn:
                            # Keep normalized URL but ensure it uses the correct scheme
                            element[attr_name] = normalized

        # Convert any remaining domain references in text content and attributes to relative paths
        # This handles cases where domain URLs appear in href, src, or other attributes
        parsed_base = urlparse(base_url)
        base_domain = parsed_base.netloc.lower().lstrip("www.")
        
        for element in soup.find_all(True):  # All elements
            for attr_name, attr_value in list(element.attrs.items()):
                if isinstance(attr_value, str) and (base_domain in attr_value.lower() or self._is_squarespace_cdn(attr_value) or "web.archive.org" in attr_value or attr_value.startswith("/web/")):
                    # Check if it's a full URL with the domain or a Squarespace CDN URL
                    is_squarespace_cdn = self._is_squarespace_cdn(attr_value)
                    if attr_value.startswith(("http://", "https://", "/web/")) or is_squarespace_cdn or "web.archive.org" in attr_value:
                        # Extract original URL if it's a wayback path
                        original = self._extract_original_url_from_path(attr_value)
                        if original:
                            attr_value = original
                        
                        # Normalize and convert to relative path if internal or Squarespace CDN
                        normalized = self._normalize_url(attr_value, base_url)
                        is_sqcdn_norm = self._is_squarespace_cdn(normalized)
                        if (self._is_internal_url(normalized) or is_sqcdn_norm) and self.config.make_internal_links_relative:
                            if is_sqcdn_norm:
                                parsed_asset = urlparse(normalized)
                                asset_path = f"{parsed_asset.netloc}{parsed_asset.path}"
                                if parsed_asset.query:
                                    asset_path += "?" + parsed_asset.query
                                while asset_path.startswith("/"):
                                    asset_path = asset_path[1:]
                                element[attr_name] = self._to_relative_path(f"/{asset_path}")
                            else:
                                relative_path = self._get_relative_link_path(normalized, is_page=False)
                                element[attr_name] = relative_path
                        elif self._is_internal_url(normalized) or is_sqcdn_norm:
                            element[attr_name] = normalized

        # Get processed HTML
        processed_html = str(soup)
        processed_html = self._optimize_html(processed_html)

        return processed_html, links_to_follow

    def _norm_track(self, url: str) -> str:
        """Normalization key for the visited-set: lowercased netloc with a
        leading ``www.`` stripped, no fragment, and no query by default.
        Matches the historical sequential loop's dedup so resume/visited
        semantics are unchanged by the move to a concurrent crawl.

        When `Config.query_string_suffix` is on, the query string is
        kept in the tracking key so `foo.png?v=1` and `foo.png?v=2`
        stay distinct (they otherwise collapse into a single fetch
        before the suffix-splicing in `_get_local_path` ever sees the
        second variant, neutralizing the on-disk disambiguation).
        """
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        if self.config.query_string_suffix:
            return parsed._replace(netloc=netloc, fragment="").geturl()
        return parsed._replace(netloc=netloc, fragment="", query="").geturl()

    def _mark_corrupted_font(self, url: str) -> None:
        """Thread-safe add to the corrupted-fonts set.

        Concurrent workers call this from download_file / CSS processing,
        while _remove_corrupted_fonts_from_css iterates a snapshot under
        the same lock — so the set is never mutated mid-iteration.
        """
        with self._lock:
            self.corrupted_fonts.add(url)

    def _enqueue(self, raw_urls, frontier, enqueued, stop) -> None:
        """Thread-safe dedup + enqueue of discovered links.

        A link is pushed onto the frontier only if its normalized key has
        not already been enqueued and is not already in visited_urls.
        ``enqueued`` is keyed by the same ``_norm_track`` form as the
        visited-set, so www/non-www variants of one page collapse to a
        single fetch — and the O(1) lookup replaces the old O(n) linear
        scan of the pending list.
        """
        for raw in raw_urls:
            if not raw or raw.startswith("#"):
                continue
            try:
                track_key = self._norm_track(raw)
            except Exception:
                continue
            with self._lock:
                if track_key in enqueued or track_key in self.config.visited_urls:
                    continue
                enqueued.add(track_key)
            if stop.is_set():
                return
            frontier.put(raw)

    def _crawl_one(self, url, frontier, enqueued, stats, stop) -> None:
        """Download and process a single URL: fetch, detect type, write to
        disk, and enqueue any links it references. Runs on a worker thread;
        all shared-state mutations are guarded by ``self._lock``."""
        # Skip fragment-only URLs (like #page, #section, etc.)
        if url.startswith("#"):
            return

        # Normalize URL for tracking (remove query strings / www so the same
        # file isn't downloaded twice). Claiming the URL — the membership
        # check plus the add — happens atomically under the lock so two
        # workers can't both process it.
        normalized_for_tracking = self._norm_track(url)
        with self._lock:
            if normalized_for_tracking in self.config.visited_urls:
                stats["skipped"] += 1
                return
            self.config.visited_urls.add(normalized_for_tracking)
            self._file_counter += 1
            current_file_num = self._file_counter

        # Show status
        file_type = self._get_file_type_from_url(url)
        limit_info = f" (limit: {self.config.max_files})" if self.config.max_files else ""
        print(f"[{current_file_num}{limit_info}] Downloading {file_type}: {url}", flush=True)
        remaining = frontier.qsize()
        if remaining:
            print(f"         Queue: {remaining} files remaining", flush=True)

        content = self.download_file(url)
        if not content:
            # Try CDN fallback for critical jQuery files if Wayback fails
            if "jquery.min.js" in url.lower() and "cdn" not in url.lower():
                cdn_urls = [
                    "https://code.jquery.com/jquery-3.7.1.min.js",
                    "https://cdn.jsdelivr.net/npm/jquery@3.7.1/dist/jquery.min.js",
                ]
                for cdn_url in cdn_urls:
                    try:
                        print(f"         🔄 Trying CDN fallback: {cdn_url}", flush=True)
                        cdn_response = self.session.get(cdn_url, timeout=10, allow_redirects=True)
                        cdn_response.raise_for_status()
                        content = cdn_response.content
                        print("         ✓ Downloaded from CDN fallback", flush=True)
                        break
                    except Exception:
                        continue

            if not content:
                with self._lock:
                    stats["failed"] += 1
                print("         ⚠️  Failed to download", flush=True)
                return

        # Show file size
        size_kb = len(content) / 1024
        if size_kb < 1024:
            print(f"         ✓ Downloaded ({size_kb:.1f} KB)", flush=True)
        else:
            print(f"         ✓ Downloaded ({size_kb/1024:.1f} MB)", flush=True)

        with self._lock:
            stats["downloaded"] += 1
            reached_limit = bool(
                self.config.max_files
                and stats["downloaded"] >= self.config.max_files
                and not stop.is_set()
            )
            if reached_limit:
                stop.set()
        if reached_limit:
            print(f"\n{'='*70}", flush=True)
            print(f"⚠️  Reached MAX_FILES limit ({self.config.max_files}) - stopping download", flush=True)
            print(f"{'='*70}", flush=True)

        # Determine file type with robust detection
        try:
            parsed = urlparse(url)
            content_type, _ = mimetypes.guess_type(parsed.path)

            # Better content type detection from URL path
            # Check for Google Fonts CSS files first (they don't have .css extension)
            if "fonts.googleapis.com" in url and "/css" in url:
                content_type = "text/css"
            elif not content_type:
                path_lower = parsed.path.lower()
                # Check for specific extensions
                if path_lower.endswith(".css") or "/.css" in path_lower:
                    content_type = "text/css"
                elif path_lower.endswith((".js", ".mjs")) or "/.js" in path_lower:
                    content_type = "application/javascript"
                elif any(path_lower.endswith(ext) for ext in [".woff", ".woff2", ".ttf", ".eot", ".otf"]):
                    content_type = "font/woff2"  # Font file
                elif any(path_lower.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp", ".tiff"]):
                    content_type = "image/jpeg"  # Default, will be refined from actual content
                elif path_lower.endswith(".json"):
                    content_type = "application/json"
                elif path_lower.endswith(".xml"):
                    content_type = "application/xml"
                elif path_lower.endswith(".pdf"):
                    content_type = "application/pdf"
                elif any(path_lower.endswith(ext) for ext in [".mp4", ".webm", ".ogg"]):
                    content_type = "video/mp4"
                elif any(path_lower.endswith(ext) for ext in [".mp3", ".wav", ".ogg"]):
                    content_type = "audio/mpeg"

            # Try to detect from actual content if still unknown
            if not content_type and len(content) > 0:
                # Check content signatures
                if content.startswith(b'<!DOCTYPE') or content.startswith(b'<html') or content.startswith(b'<HTML'):
                    content_type = "text/html"
                elif content.startswith(b'/*') or content.startswith(b'@charset') or b'@media' in content[:200]:
                    content_type = "text/css"
                elif content.startswith(b'<?xml') or b'<svg' in content[:200]:
                    content_type = "image/svg+xml"
                elif content.startswith(b'\x89PNG'):
                    content_type = "image/png"
                elif content.startswith(b'\xff\xd8\xff'):
                    content_type = "image/jpeg"
                elif content.startswith(b'GIF'):
                    content_type = "image/gif"
                elif content.startswith(b'RIFF') and b'WEBP' in content[:12]:
                    content_type = "image/webp"
        except Exception as e:
            print(f"Warning: Error detecting content type for {url}: {e}")
            content_type = None

        # Use normalized URL (without query strings) for file paths
        # Exception: For Google Fonts CSS files, preserve query string in path for uniqueness
        if "fonts.googleapis.com" in url and "/css" in url:
            # For Google Fonts CSS, use query string hash to create unique filename
            parsed_original = urlparse(url)
            query_hash = hashlib.md5(parsed_original.query.encode()).hexdigest()[:8]
            font_path = f"fonts.googleapis.com/css-{query_hash}.css"
            local_path = self._get_local_path(f"http://{font_path}")
        else:
            local_path = self._get_local_path(normalized_for_tracking)
        local_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Check for Google Fonts CSS files first (they don't have .css extension)
            is_google_fonts_css = "fonts.googleapis.com" in url and "/css" in url

            # Process based on content type - be more conservative about what we treat as HTML
            is_html = (
                not is_google_fonts_css and (
                    content_type == "text/html" or
                    (not content_type and (
                        url.endswith(".html") or
                        url.endswith(".htm") or
                        (parsed.path and not os.path.splitext(parsed.path)[1] and "?" not in url and not any(parsed.path.lower().endswith(ext) for ext in [".css", ".js", ".json", ".xml", ".txt"]))
                    ))
                )
            )

            if is_html:
                # Process HTML
                try:
                    print("         Processing HTML and extracting links...", flush=True)
                    # Try to decode as UTF-8, fallback to latin-1 or detect encoding
                    try:
                        html = content.decode("utf-8", errors="strict")
                    except UnicodeDecodeError:
                        try:
                            html = content.decode("utf-8", errors="ignore")
                        except Exception:
                            # Last resort: try latin-1 which can decode any byte sequence
                            html = content.decode("latin-1", errors="ignore")

                    processed_html, new_links = self._process_html(html, url)
                    if new_links:
                        print(f"         Found {len(new_links)} new links to download", flush=True)
                except Exception as e:
                    print(f"Error processing HTML for {url}: {e}")
                    traceback.print_exc()
                    # Still save the raw HTML if processing fails
                    try:
                        with open(local_path, "wb") as f:
                            f.write(content)
                        with self._lock:
                            self.config.downloaded_files[url] = str(local_path)
                    except Exception as save_error:
                        print(f"Error saving file {local_path}: {save_error}")
                    return

                # Save HTML
                try:
                    with open(local_path, "w", encoding="utf-8", errors="replace") as f:
                        f.write(processed_html)
                    with self._lock:
                        self.config.downloaded_files[url] = str(local_path)
                except Exception as e:
                    print(f"Error saving HTML to {local_path}: {e}")
                    return

                # Add new links to the frontier (deduplicated)
                self._enqueue(new_links, frontier, enqueued, stop)

            elif content_type == "text/css":
                # Process CSS
                try:
                    css = content.decode("utf-8", errors="ignore")
                except Exception:
                    css = content.decode("latin-1", errors="ignore")

                # Point the per-worker relative-path base at this stylesheet's
                # own URL: _rewrite_css_urls -> _make_relative_path reads
                # _current_page_url, which is otherwise only set by
                # _process_html — so without this a CSS file gets rewritten
                # relative to whatever HTML page this worker handled last.
                previous_page_url = self._current_page_url
                self._current_page_url = url
                try:
                    print("         Processing CSS and extracting resources...", flush=True)
                    # Extract URLs from CSS (images, fonts, @import, etc.)
                    css_urls = self._extract_css_urls(css, url)
                    if css_urls:
                        print(f"         Found {len(css_urls)} resources in CSS", flush=True)
                    css_to_enqueue = []
                    for css_url in css_urls:
                        # Handle fonts.gstatic.com URLs - these are external but available on Wayback Machine
                        # They need to be downloaded to avoid CORS issues
                        is_google_font = "fonts.gstatic.com" in css_url or "fonts.googleapis.com" in css_url
                        is_squarespace_cdn = self._is_squarespace_cdn(css_url)
                        if self._is_internal_url(css_url) or is_google_font or is_squarespace_cdn:
                            css_to_enqueue.append(css_url)
                            if is_google_font:
                                print(f"         📥 Queued Google Font file for download: {css_url[:80]}...", flush=True)
                    self._enqueue(css_to_enqueue, frontier, enqueued, stop)

                    # Rewrite URLs in CSS to relative paths
                    css = self._rewrite_css_urls(css, url)

                    # Check font URLs in CSS and detect corrupted ones proactively
                    # This ensures we catch corrupted fonts even if they haven't been downloaded yet
                    css = self._check_and_remove_corrupted_fonts_in_css(css, url)

                    # Remove references to already-detected corrupted fonts
                    css = self._remove_corrupted_fonts_from_css(css)

                    # Proactively remove .eot and .svg font format references
                    # These are often corrupted (HTML error pages) and modern browsers don't need them
                    # Browsers will use .woff2, .woff, and .ttf which are more reliable
                    css = self._remove_legacy_font_formats_from_css(css)

                    css = self._minify_css(css)
                except Exception as e:
                    print(f"Warning: Error processing CSS for {url}: {e}")
                    # Use original content if processing fails
                    css = content.decode("utf-8", errors="ignore")
                finally:
                    self._current_page_url = previous_page_url

                try:
                    with open(local_path, "w", encoding="utf-8", errors="replace") as f:
                        f.write(css)
                    with self._lock:
                        self.config.downloaded_files[url] = str(local_path)
                except Exception as e:
                    print(f"Error saving CSS to {local_path}: {e}")
                    return

            elif content_type in ("application/javascript", "text/javascript"):
                # Process JavaScript
                js = content.decode("utf-8", errors="ignore")

                print("         Processing JavaScript and extracting URLs...", flush=True)
                # Extract URLs from JavaScript (may contain fetch, XMLHttpRequest, etc.)
                js_urls = self._extract_js_urls(js, url)
                if js_urls:
                    print(f"         Found {len(js_urls)} URLs in JavaScript", flush=True)
                self._enqueue(
                    [u for u in js_urls if self._is_internal_url(u)],
                    frontier, enqueued, stop,
                )

                js = self._minify_js(js)

                with open(local_path, "w", encoding="utf-8") as f:
                    f.write(js)

                with self._lock:
                    self.config.downloaded_files[url] = str(local_path)

            elif content_type and content_type.startswith("image/"):
                # Process images
                format_map = {
                    "image/jpeg": "JPEG",
                    "image/png": "PNG",
                    "image/gif": "GIF",
                    "image/webp": "WEBP",
                }
                img_format = format_map.get(content_type, "JPEG")
                optimized = self._optimize_image(content, img_format)

                with open(local_path, "wb") as f:
                    f.write(optimized)

                with self._lock:
                    self.config.downloaded_files[url] = str(local_path)

            elif content_type and content_type.startswith("font/"):
                # Save font files as-is
                with open(local_path, "wb") as f:
                    f.write(content)
                with self._lock:
                    self.config.downloaded_files[url] = str(local_path)

            else:
                # Save as-is
                with open(local_path, "wb") as f:
                    f.write(content)

                with self._lock:
                    self.config.downloaded_files[url] = str(local_path)
        except Exception as e:
            print(f"Error processing {url}: {e}")
            return

    def download(self):
        """Main download method.

        Crawls the site with ``config.workers`` worker threads sharing a
        single work-queue. Each worker claims a URL, downloads and processes
        it, and pushes any newly discovered links back onto the queue;
        dedup of both the visited-set and the frontier is O(1) under a
        shared lock. With ``workers == 1`` this is a straight sequential
        crawl, identical in behavior to the historical loop.
        """
        # Create output directory
        Path(self.config.output_dir).mkdir(parents=True, exist_ok=True)

        workers = max(1, int(getattr(self.config, "workers", 1) or 1))

        print(f"\n{'='*70}", flush=True)
        print("Wayback-Archive Downloader", flush=True)
        print(f"{'='*70}", flush=True)
        print(f"Starting URL: {self.config.base_url}", flush=True)
        print(f"Output directory: {self.config.output_dir}", flush=True)
        print(f"Crawl workers: {workers}", flush=True)
        if self.config.max_files:
            print(f"⚠️  TEST MODE: Limited to {self.config.max_files} files", flush=True)
        print(f"{'='*70}\n", flush=True)

        # Shared crawl state.
        frontier: "queue.Queue" = queue.Queue()
        enqueued: Set[str] = set()
        stats = {"downloaded": 0, "failed": 0, "skipped": 0}
        stop = threading.Event()
        _SENTINEL = object()

        base = self.config.base_url
        frontier.put(base)
        try:
            enqueued.add(self._norm_track(base))
        except Exception:
            pass

        def _worker() -> None:
            while True:
                item = frontier.get()
                if item is _SENTINEL:
                    frontier.task_done()
                    return
                try:
                    if not stop.is_set():
                        self._crawl_one(item, frontier, enqueued, stats, stop)
                except Exception as e:
                    print(f"Error processing {item}: {e}", flush=True)
                finally:
                    frontier.task_done()

        threads = [
            threading.Thread(target=_worker, name=f"wa-crawl-{i}", daemon=True)
            for i in range(workers)
        ]
        for t in threads:
            t.start()
        # Block until the frontier is fully drained (every put has a matching
        # task_done), then release the workers with one sentinel each.
        frontier.join()
        for _ in threads:
            frontier.put(_SENTINEL)
        for t in threads:
            t.join()

        print(f"\n{'='*70}", flush=True)
        print("Download Complete!", flush=True)
        print(f"{'='*70}", flush=True)
        print(f"Output directory: {self.config.output_dir}", flush=True)
        print(f"Files successfully downloaded: {stats['downloaded']}", flush=True)
        print(f"Files failed: {stats['failed']}", flush=True)
        print(f"Files skipped (duplicates): {stats['skipped']}", flush=True)
        if self.corrupted_fonts:
            print(f"Corrupted fonts detected and removed: {len(self.corrupted_fonts)}", flush=True)
        print(f"Total files processed: {len(self.config.visited_urls)}", flush=True)
        print(f"{'='*70}\n", flush=True)

    def repair(self, rel_paths, *, workers: int = 4,
               on_result=None, on_alt_lookup=None):
        """Re-fetch specific missing assets for an already-archived snapshot.

        Targeted re-fetch driven by an explicit list of snapshot-relative
        paths (as produced by an audit). For each rel:

          1. Compute the local path; skip if the file is already on disk
             and not an HTML-error masquerade.
          2. Fetch ``orig_url`` via this downloader's ``download_file``
             (which honors the resume / sandbox / masquerade Config
             flags).
          3. On miss, query Wayback CDX for alt timestamps and try each
             via the raw ``id_`` endpoint, rejecting masquerades.
          4. Classify failures: a `TransientCDXError` along the way means
             the asset stays retryable; only a clean exhaustion of every
             alt is permanently `unrecoverable=True`.
          5. Write the bytes atomically to the local path.

        Concurrency: each worker thread gets its own ``WaybackDownloader``
        constructed from the same Config so each has its own
        ``requests.Session`` (sessions are not thread-safe for parallel
        use). The on_result callback is invoked from the main thread in
        completion order; on_alt_lookup is invoked from worker threads.

        Returns a `RepairSummary`. The dashboard's repair shim emits its
        per-asset log lines from the on_result callback and merges
        unrecoverable rel paths into its `.unrecoverable.json` sidecar.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from urllib.parse import urlparse as _urlparse
        from wayback_archive.cdx import (
            alt_timestamps as _alt_timestamps,
            raw_fetch as _raw_fetch,
            TransientCDXError as _TransientCDXError,
        )
        from wayback_archive.io_atomic import atomic_write_bytes
        from wayback_archive.playback import looks_like_html_error, url_ext
        from wayback_archive.repair import RepairResult, RepairSummary

        # Dedupe + sanity.
        rel_paths = [r for r in dict.fromkeys(rel_paths) if r]
        total = len(rel_paths)
        if total == 0:
            return RepairSummary()

        # Derive scheme/host from the configured Wayback URL.
        base = self.config.base_url or ""
        parsed_base = _urlparse(base)
        scheme = f"{parsed_base.scheme}:" if parsed_base.scheme else "http:"
        host = parsed_base.netloc
        if not host:
            raise ValueError(
                f"repair() requires a WAYBACK_URL whose original URL has a netloc; got {base!r}"
            )
        ts_primary = getattr(self, "original_timestamp", "") or ""

        out_root = Path(self.config.output_dir).resolve()
        cfg = self.config
        workers = max(1, min(workers, total))

        # Per-thread downloader so each worker has its own requests.Session.
        # Sessions aren't thread-safe for parallel calls into a single
        # Session — sharing one would corrupt connection-pool state.
        _dl_local = threading.local()

        def _thread_dl():
            dl = getattr(_dl_local, "downloader", None)
            if dl is None:
                dl = WaybackDownloader(cfg)
                _dl_local.downloader = dl
            return dl

        def _fetch_one(index: int, rel: str) -> RepairResult:
            orig_url = _orig_url_from_rel(
                rel, scheme, host,
                query_string_suffix=cfg.query_string_suffix,
            )
            ext = url_ext(orig_url)
            result = RepairResult(
                rel=rel, orig_url=orig_url, index=index, total=total,
                status="fail",
            )
            try:
                local = (Path(cfg.output_dir) / rel).resolve()
                if out_root not in local.parents and local != out_root:
                    # Unsafe relative path — outside output_dir.
                    result.status = "skip"
                    return result

                # Already on disk and not a masquerade? Skip.
                if local.is_file() and local.stat().st_size > 0:
                    try:
                        with local.open("rb") as fh:
                            head = fh.read(512)
                    except OSError:
                        head = b""
                    if head and not looks_like_html_error(head, ext):
                        result.status = "skip"
                        result.bytes_written = local.stat().st_size
                        return result

                # Primary fetch.
                dl = _thread_dl()
                transient = False
                try:
                    content = dl.download_file(orig_url)
                except Exception:
                    content = None
                    transient = True
                used_ts = ts_primary if content else None
                if content and looks_like_html_error(content, ext):
                    content = None
                from_fallback = False

                # Alt-snapshot fallback if needed.
                if not content:
                    try:
                        alts = _alt_timestamps(
                            orig_url, ts_primary,
                            limit=cfg.auto_search_snapshots_limit,
                            urlopen=(cfg.cdx_urlopen
                                     if cfg.cdx_urlopen is not None
                                     else _default_urlopen),
                        )
                    except _TransientCDXError:
                        alts = []
                        transient = True
                    result.tried_alts = len(alts)
                    if on_alt_lookup is not None and alts:
                        try:
                            on_alt_lookup(rel, len(alts))
                        except Exception:
                            pass
                    for alt in alts:
                        try:
                            data = _raw_fetch(
                                dl.session, alt, orig_url,
                                on_response=cfg.cdx_response_observer,
                            )
                        except _TransientCDXError:
                            transient = True
                            continue
                        if not data or looks_like_html_error(data, ext):
                            continue
                        content = data
                        used_ts = alt
                        from_fallback = True
                        break

                if not content:
                    result.status = "fail"
                    result.unrecoverable = not transient
                    return result

                # Write atomically.
                try:
                    atomic_write_bytes(local, content)
                except Exception:
                    result.status = "fail"
                    result.unrecoverable = False
                    result.from_fallback = from_fallback
                    return result

                result.status = "ok"
                result.from_fallback = from_fallback
                result.used_ts = used_ts
                result.bytes_written = len(content)
                return result
            except Exception:
                # Catch-all so one worker error doesn't abort the pool.
                # Log with traceback so repair regressions stay
                # triageable from the run log — fail-open behavior is
                # preserved.
                log.error(
                    "repair worker crashed on rel=%s url=%s",
                    rel, orig_url, exc_info=True,
                )
                result.status = "fail"
                result.unrecoverable = False
                return result

        ok = failed = skipped = fallback_hits = 0
        unrecoverable: list[str] = []
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="wa-repair") as ex:
            futures = [ex.submit(_fetch_one, i, rel)
                       for i, rel in enumerate(rel_paths, 1)]
            for fut in as_completed(futures):
                r = fut.result()
                if r.status == "ok":
                    ok += 1
                    if r.from_fallback:
                        fallback_hits += 1
                elif r.status == "fail":
                    failed += 1
                    if r.unrecoverable:
                        unrecoverable.append(r.rel)
                elif r.status == "skip":
                    skipped += 1
                if on_result is not None:
                    try:
                        on_result(r)
                    except Exception:
                        pass

        return RepairSummary(
            ok=ok, failed=failed, skipped=skipped,
            fallback_hits=fallback_hits,
            unrecoverable=sorted(unrecoverable),
            duration_s=time.monotonic() - t0,
        )


def _default_urlopen(req, timeout=15):
    """Module-level default for repair()'s CDX queries — kept here so the
    callable is picklable / replaceable from outside tests."""
    import urllib.request
    return urllib.request.urlopen(req, timeout=timeout)


_QUERY_HASH_SUFFIX_RE = re.compile(r"\.q-[0-9a-f]{8}(?=\.[^.]+$|$)")
# Hostname-segment sniff: at least one dot, doesn't start with one, and
# ends in a 2-6 letter TLD-ish suffix. Rejects directory names that just
# happen to contain a dot — `.well-known`, `v1.2`, `node_modules`, etc.
_HOSTNAME_SEG_RE = re.compile(r"^(?!\.)[A-Za-z0-9._-]+\.[A-Za-z]{2,6}$")


def _orig_url_from_rel(
    rel: str,
    primary_scheme: str,
    primary_host: str,
    *,
    query_string_suffix: bool = False,
) -> str:
    """Reconstruct the original origin URL from a snapshot-rel path.

    `_get_local_path` preserves the full netloc as the first directory
    component for cross-origin assets (Google Fonts, Squarespace CDN,
    any URL whose host isn't the primary). A rel path of
    `fonts.googleapis.com/css/...` came from
    `https://fonts.googleapis.com/css/...`, NOT from the primary
    snapshot host.

    Heuristic: the first path segment is treated as an origin host only
    when it (a) is a *directory* — followed by `/`, and (b) matches
    `_HOSTNAME_SEG_RE` (TLD-ish suffix, doesn't start with `.`). That
    excludes legitimate path directories like `.well-known/`, `v1.2/`,
    `node_modules/`. Cross-origin URLs always use `https://` since modern
    CDNs (Google Fonts, Squarespace, jsDelivr…) are HTTPS-only — using
    the snapshot's own scheme would 308 / fail for any http-anchored
    archive.

    When `query_string_suffix` is True, strips any `.q-<8hex>` segment
    from the filename stem — that's the marker `_get_local_path`
    splices in for query-bearing URLs. We only do this when the
    config flag is on so a legitimate user filename like
    `logo.q-deadbeef.png` isn't mangled. The original query can't be
    recovered (sha1 is one-way), so the rebuilt URL has no query;
    repair() fetches the query-less variant as best-effort.
    """
    rel = rel.lstrip("/")
    if query_string_suffix:
        rel = _QUERY_HASH_SUFFIX_RE.sub("", rel)
    first_seg, sep, rest = rel.partition("/")
    if sep and _HOSTNAME_SEG_RE.match(first_seg):
        return f"https://{first_seg}/{rest}"
    return f"{primary_scheme}//{primary_host}/{rel}"


# --- Playwright render-thread singleton (one per process) -------------
#
# `_install_playwright_render` on each WaybackDownloader instance dispatches
# through these shared module-level objects so that 4 worker threads in
# `repair()` don't spawn 4 separate Chromium processes. The thread + the
# Playwright/browser/context it owns are created lazily on the first
# render request and torn down once at process exit.

_PLAYWRIGHT_LOCK = threading.Lock()
_PLAYWRIGHT_RENDER_Q = None  # type: Optional[queue.Queue]
_PLAYWRIGHT_THREAD = None  # type: Optional[threading.Thread]
_PLAYWRIGHT_SHUTDOWN = object()


def _ensure_playwright_singleton():
    """Return the process-wide render queue, starting the render thread on
    first call. Returns None if Playwright fails to import."""
    global _PLAYWRIGHT_RENDER_Q, _PLAYWRIGHT_THREAD
    if _PLAYWRIGHT_RENDER_Q is not None:
        return _PLAYWRIGHT_RENDER_Q
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    with _PLAYWRIGHT_LOCK:
        if _PLAYWRIGHT_RENDER_Q is not None:
            return _PLAYWRIGHT_RENDER_Q
        import atexit

        q = queue.Queue()

        def _render_loop():
            pw = browser = ctx = None
            try:
                while True:
                    item = q.get()
                    if item is _PLAYWRIGHT_SHUTDOWN:
                        return
                    html, fut = item
                    try:
                        if ctx is None:
                            pw = sync_playwright().start()
                            browser = pw.chromium.launch(
                                headless=True,
                                args=["--no-sandbox", "--disable-dev-shm-usage"],
                            )
                            ctx = browser.new_context(
                                user_agent="Mozilla/5.0 Wayback-Archive/Playwright",
                                ignore_https_errors=True,
                            )
                        page = ctx.new_page()
                        try:
                            page.set_content(
                                html, wait_until="networkidle", timeout=15000,
                            )
                            rendered = page.content()
                        finally:
                            page.close()
                        fut.set_result(rendered)
                    except Exception:
                        fut.set_result(None)
            finally:
                for closer in (
                    lambda: ctx and ctx.close(),
                    lambda: browser and browser.close(),
                    lambda: pw and pw.stop(),
                ):
                    try:
                        closer()
                    except Exception:
                        pass

        t = threading.Thread(target=_render_loop, name="wa-playwright-render",
                              daemon=True)
        t.start()

        def _shutdown_render():
            q.put(_PLAYWRIGHT_SHUTDOWN)
            t.join(timeout=10)

        atexit.register(_shutdown_render)
        _PLAYWRIGHT_RENDER_Q = q
        _PLAYWRIGHT_THREAD = t
        return q
