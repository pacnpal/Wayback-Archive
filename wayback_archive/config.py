"""Configuration management for Wayback-Archive."""

import os
from typing import Optional, Tuple
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


def get_bool_env(key: str, default: bool = False) -> bool:
    """Get boolean environment variable."""
    value = os.getenv(key, "").lower()
    return value in ("true", "1", "yes", "on") if value else default


def get_str_env(key: str, default: Optional[str] = None) -> Optional[str]:
    """Get string environment variable."""
    return os.getenv(key, default)


class Config:
    """Configuration class for Wayback-Archive."""

    def __init__(self):
        # Required
        self.wayback_url: Optional[str] = get_str_env("WAYBACK_URL")

        # Output
        self.output_dir: str = get_str_env("OUTPUT_DIR", "./output")

        # HTML optimization
        self.optimize_html: bool = get_bool_env("OPTIMIZE_HTML", True)

        # Image optimization
        self.optimize_images: bool = get_bool_env("OPTIMIZE_IMAGES", False)

        # Minification
        self.minify_js: bool = get_bool_env("MINIFY_JS", False)
        self.minify_css: bool = get_bool_env("MINIFY_CSS", False)

        # Content removal
        self.remove_trackers: bool = get_bool_env("REMOVE_TRACKERS", True)
        self.remove_ads: bool = get_bool_env("REMOVE_ADS", True)
        self.remove_clickable_contacts: bool = get_bool_env("REMOVE_CLICKABLE_CONTACTS", True)
        self.remove_external_iframes: bool = get_bool_env("REMOVE_EXTERNAL_IFRAMES", False)

        # External links handling
        self.remove_external_links_keep_anchors: bool = get_bool_env(
            "REMOVE_EXTERNAL_LINKS_KEEP_ANCHORS", True
        )
        self.remove_external_links_remove_anchors: bool = get_bool_env(
            "REMOVE_EXTERNAL_LINKS_REMOVE_ANCHORS", False
        )

        # Link conversion
        self.make_internal_links_relative: bool = get_bool_env("MAKE_INTERNAL_LINKS_RELATIVE", True)
        self.make_non_www: bool = get_bool_env("MAKE_NON_WWW", True)
        self.make_www: bool = get_bool_env("MAKE_WWW", False)

        # Redirections
        self.keep_redirections: bool = get_bool_env("KEEP_REDIRECTIONS", False)

        # Download limit (for testing - set MAX_FILES to limit downloads)
        # If MAX_FILES is not set, downloads are unlimited
        max_files_str = get_str_env("MAX_FILES")
        if max_files_str and max_files_str.strip().isdigit():
            self.max_files: Optional[int] = int(max_files_str.strip())
        else:
            self.max_files: Optional[int] = None  # Unlimited downloads

        # Crawl concurrency. The main download loop runs this many worker
        # threads against a shared work-queue. Default 1 keeps behavior
        # identical to the historical sequential crawl; the dashboard sets
        # FETCH_WORKERS to fan the crawl out.
        workers_str = get_str_env("FETCH_WORKERS")
        if workers_str and workers_str.strip().isdigit():
            self.workers: int = max(1, int(workers_str.strip()))
        else:
            self.workers: int = 1

        # Archive-only mode. When set, the downloader never touches a
        # non-web.archive.org host: it skips the live-origin fallback that
        # download_file() would otherwise try after every Wayback miss, and
        # it drops any captured redirect whose target leaves web.archive.org.
        # Default False keeps the standalone CLI's historical behavior (live
        # fallback on); the dashboard sets ARCHIVE_ONLY=1 by default so a
        # crawl of a long-dead host never fans requests out to origins that
        # haven't existed in decades.
        self.archive_only: bool = get_bool_env("ARCHIVE_ONLY", False)

        # Auto-search snapshots via the Wayback CDX index. When a file is
        # missing at the original timestamp and the brute-force +/- timeframe
        # scan turns up nothing, the downloader queries
        # web.archive.org/cdx/search/cdx for every successful capture of the
        # URL and tries the captures closest to the original timestamp.
        # This is how pieces of a partially-archived site get put back
        # together: an asset that was only captured at a totally different
        # date than the page that references it still gets pulled in.
        # Default on. Set AUTO_SEARCH_SNAPSHOTS=0 to disable and skip the
        # extra CDX request per miss.
        self.auto_search_snapshots: bool = get_bool_env("AUTO_SEARCH_SNAPSHOTS", True)

        # How many CDX-discovered candidate timestamps to try per URL before
        # giving up. The list is sorted by proximity to the original
        # timestamp, so a small limit still favors the closest captures.
        cdx_limit_str = get_str_env("AUTO_SEARCH_SNAPSHOTS_LIMIT")
        if cdx_limit_str and cdx_limit_str.strip().isdigit():
            self.auto_search_snapshots_limit: int = max(1, int(cdx_limit_str.strip()))
        else:
            self.auto_search_snapshots_limit: int = 10

        # Internal state
        self.base_url: Optional[str] = None
        self.domain: Optional[str] = None
        self.visited_urls: set = set()
        self.downloaded_files: dict = {}  # URL -> local path mapping

    def validate(self) -> tuple[bool, Optional[str]]:
        """Validate configuration."""
        if not self.wayback_url:
            return False, "WAYBACK_URL environment variable is required"
        return True, None

    def __repr__(self) -> str:
        """String representation of config."""
        return f"Config(wayback_url={self.wayback_url}, output_dir={self.output_dir})"
