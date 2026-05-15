"""Tests for downloader module."""

import os
import pytest
import requests
from bs4 import BeautifulSoup
from unittest.mock import Mock, patch, MagicMock
from wayback_archive.config import Config
from wayback_archive.downloader import WaybackDownloader


class TestWaybackDownloader:
    """Test downloader class."""

    def setup_method(self):
        """Set up test fixtures."""
        os.environ["WAYBACK_URL"] = "https://web.archive.org/web/20250417203037/http://example.com/"
        self.config = Config()
        self.downloader = WaybackDownloader(self.config)

    def teardown_method(self):
        """Clean up after tests."""
        os.environ.pop("WAYBACK_URL", None)

    def test_parse_wayback_url(self):
        """Test Wayback URL parsing."""
        assert self.downloader.config.base_url == "http://example.com/"
        assert self.downloader.config.domain == "example.com"

    def test_is_internal_url(self):
        """Test internal URL detection."""
        assert self.downloader._is_internal_url("http://example.com/page") is True
        assert self.downloader._is_internal_url("http://www.example.com/page") is True
        assert self.downloader._is_internal_url("http://other.com/page") is False
        assert self.downloader._is_internal_url("/relative/page") is True

    def test_is_tracker(self):
        """Test tracker detection."""
        assert self.downloader._is_tracker("https://www.google-analytics.com/ga.js") is True
        assert self.downloader._is_tracker("https://example.com/script.js") is False

    def test_is_ad(self):
        """Test ad detection."""
        assert self.downloader._is_ad("https://ads.example.com/banner.jpg") is True
        assert self.downloader._is_ad("https://example.com/image.jpg") is False

    def test_is_contact_link(self):
        """Test contact link detection."""
        assert self.downloader._is_contact_link("mailto:test@example.com") is True
        assert self.downloader._is_contact_link("tel:+1234567890") is True
        assert self.downloader._is_contact_link("http://example.com") is False

    def test_convert_to_wayback_url(self):
        """Test Wayback URL conversion."""
        wayback_url = self.downloader._convert_to_wayback_url("http://example.com/page")
        assert "web.archive.org" in wayback_url
        assert "20250417203037" in wayback_url

    def test_normalize_url(self):
        """Test URL normalization."""
        # Test relative to absolute
        normalized = self.downloader._normalize_url("/page", "http://example.com/")
        assert normalized == "http://example.com/page"

        # Test www removal
        normalized = self.downloader._normalize_url("http://www.example.com/page", "http://example.com/")
        assert "www." not in normalized

    def test_make_relative_path(self):
        """Test relative path conversion."""
        path = self.downloader._make_relative_path("http://example.com/page.html")
        assert path == "/page.html"

    def test_get_local_path(self):
        """Test local path generation."""
        path = self.downloader._get_local_path("http://example.com/page.html")
        assert "page.html" in str(path)
        assert path.name == "page.html"

    @patch("wayback_archive.downloader.requests.Session.get")
    def test_download_file(self, mock_get):
        """Test file downloading."""
        mock_response = Mock()
        mock_response.content = b"test content"
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        content = self.downloader.download_file("http://example.com/page")
        assert content == b"test content"

    def test_optimize_html(self):
        """Test HTML optimization."""
        html = "<html><body>  <p>Test</p>  </body></html>"
        optimized = self.downloader._optimize_html(html)
        assert len(optimized) <= len(html)

    def test_minify_js(self):
        """Test JavaScript minification."""
        js = """
        function test() {
            var x = 1;
            return x;
        }
        """
        minified = self.downloader._minify_js(js)
        assert len(minified) <= len(js)

    def test_minify_css(self):
        """Test CSS minification."""
        css = """
        body {
            margin: 0;
            padding: 0;
        }
        """
        minified = self.downloader._minify_css(css)
        assert len(minified) <= len(css)

    def test_process_html_rewrites_and_queues_frames(self):
        """Test that frame-based pages are rewritten and queued for download."""
        self.config.remove_external_iframes = True

        html = """
        <html>
            <frameset cols="25%,75%">
                <frame src="/web/20010405003907fw_/http://example.com/left.html">
                <frame src="content">
                <iframe src="/web/20010405003907if_/http://example.com/embed"></iframe>
                <iframe src="http://other.com/external.html"></iframe>
            </frameset>
        </html>
        """

        processed_html, links_to_follow = self.downloader._process_html(
            html,
            "http://example.com/index.html",
        )

        soup = BeautifulSoup(processed_html, "lxml")
        sources = [tag.get("src") for tag in soup.find_all(["frame", "iframe"], src=True)]

        assert "left.html" in sources
        assert "content.html" in sources
        assert "embed.html" in sources
        assert all("other.com" not in src for src in sources)

        assert "http://example.com/left.html" in links_to_follow
        assert "http://example.com/embed" in links_to_follow
        assert any(link == "content" or link.endswith("/content") for link in links_to_follow)

    def test_process_html_rewrites_legacy_background_attributes(self):
        """Test that HTML background= attrs in frame content are rewritten and images queued.

        Regression test for GitHub Issue #1: legacy frameset pages like lightpen.com
        use <body background="..."> and <table background="..."> with Wayback URLs
        that must be rewritten to relative paths and queued for download.
        """
        html = """
        <html>
            <body alink="midnightblue" background="/web/20010405005347im_/http://example.com/background.gif" bgcolor="gray">
                <table background="/web/20010405005347im_/http://example.com/background.gif" bgcolor="gray">
                    <tr>
                        <td>
                            <a href="/web/20010405003907/http://example.com/page.html">Link</a>
                        </td>
                    </tr>
                </table>
            </body>
        </html>
        """

        processed_html, links_to_follow = self.downloader._process_html(
            html,
            "http://example.com/left.html",
        )

        soup = BeautifulSoup(processed_html, "lxml")

        # background= attributes must be rewritten to relative paths
        body = soup.find("body")
        assert body["background"] == "background.gif"

        table = soup.find("table")
        assert table["background"] == "background.gif"

        # The background image must be queued for download
        assert "http://example.com/background.gif" in links_to_follow

    @patch("wayback_archive.downloader.requests.Session.get")
    def test_font_prefetch_checked_only_once_across_multiple_css_files(self, mock_get):
        """Each font URL is pre-checked at most once, even when multiple CSS files
        reference the same font.

        Regression test: _check_and_remove_corrupted_fonts_in_css used to re-download
        the same font once per CSS file that referenced it, producing the same large
        file with status=ok 5-6× in a single run.
        """
        # Simulate a non-corrupted font response (binary content, not HTML)
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = b"\x00\x01\x00\x00" * 100  # sfnt/TrueType header bytes, not HTML
        mock_get.return_value = mock_response

        font_url = "http://example.com/fonts/my-font.woff2"

        # Three CSS files all reference the same font
        css_template = f"@font-face {{ src: url('{font_url}') format('woff2'); }}"
        base_url = "http://example.com/css/style.css"

        self.downloader._check_and_remove_corrupted_fonts_in_css(css_template, base_url)
        self.downloader._check_and_remove_corrupted_fonts_in_css(css_template, base_url)
        self.downloader._check_and_remove_corrupted_fonts_in_css(css_template, base_url)

        # The font should have been fetched exactly once, not three times
        assert mock_get.call_count == 1

        # The font URL should be recorded in the pre-checked set
        normalized = self.downloader._normalize_url(font_url, base_url)
        assert normalized in self.downloader._font_prefetch_checked

    @patch("wayback_archive.downloader.requests.Session.get")
    def test_font_prefetch_checked_only_once_concurrent(self, mock_get):
        """Font URL is pre-checked at most once even when multiple threads process
        CSS files referencing the same font simultaneously.

        The cache is documented as lock-guarded; this test exercises the atomic
        reserve-then-fetch path so that concurrent workers cannot race past the
        membership check and issue duplicate downloads.
        """
        import threading

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = b"\x00\x01\x00\x00" * 100  # sfnt/TrueType header bytes, not HTML
        mock_get.return_value = mock_response

        font_url = "http://example.com/fonts/concurrent-font.woff2"
        css_template = f"@font-face {{ src: url('{font_url}') format('woff2'); }}"
        base_url = "http://example.com/css/style.css"

        barrier = threading.Barrier(3)
        errors = []

        def run():
            try:
                barrier.wait()  # synchronise all threads to start simultaneously
                self.downloader._check_and_remove_corrupted_fonts_in_css(css_template, base_url)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Worker thread raised: {errors}"
        # Despite concurrent execution, the font must have been fetched at most once
        assert mock_get.call_count <= 1


class TestArchiveOnlyMode:
    """archive_only mode keeps the downloader on web.archive.org: no
    live-origin fallback after a Wayback miss, and no chasing a captured
    redirect whose target leaves the archive."""

    def teardown_method(self):
        for k in ("WAYBACK_URL", "ARCHIVE_ONLY"):
            os.environ.pop(k, None)

    def test_config_reads_archive_only_env(self):
        os.environ["ARCHIVE_ONLY"] = "1"
        assert Config().archive_only is True
        os.environ["ARCHIVE_ONLY"] = "0"
        assert Config().archive_only is False
        # Default off — the standalone CLI keeps its historical live-fallback
        # behavior; archive-only is a deliberate opt-in (the dashboard's
        # default, not the engine's).
        os.environ.pop("ARCHIVE_ONLY")
        assert Config().archive_only is False

    @staticmethod
    def _wayback_404_live_ok(url, *a, **kw):
        # Every web.archive.org request 404s; the live origin would serve
        # the asset. Used to prove the flag gates the live fallback.
        if "web.archive.org" in url:
            raise requests.exceptions.HTTPError(response=Mock(status_code=404))
        resp = Mock()
        resp.content = b"LIVE-BYTES"
        resp.raise_for_status = Mock()
        return resp

    @patch("wayback_archive.downloader.requests.Session.get")
    def test_archive_only_skips_live_origin_fallback(self, mock_get):
        os.environ["WAYBACK_URL"] = (
            "https://web.archive.org/web/20200101000000/http://example.com/"
        )
        os.environ["ARCHIVE_ONLY"] = "1"
        mock_get.side_effect = self._wayback_404_live_ok
        dl = WaybackDownloader(Config())
        # The asset is gone from Wayback; archive_only must NOT reach for
        # the live origin, so the result is a clean miss.
        assert dl.download_file("http://example.com/logo.png") is None
        # Every request stayed on web.archive.org.
        assert mock_get.call_count > 0
        assert all("web.archive.org" in c.args[0]
                   for c in mock_get.call_args_list)

    @patch("wayback_archive.downloader.requests.Session.get")
    def test_live_fallback_still_works_when_archive_only_off(self, mock_get):
        os.environ["WAYBACK_URL"] = (
            "https://web.archive.org/web/20200101000000/http://example.com/"
        )
        os.environ.pop("ARCHIVE_ONLY", None)  # default: off
        mock_get.side_effect = self._wayback_404_live_ok
        dl = WaybackDownloader(Config())
        # Same setup, flag off — the live fallback still recovers the asset,
        # so the gating in the test above is genuinely the flag's doing.
        assert dl.download_file("http://example.com/logo.png") == b"LIVE-BYTES"

    def test_archive_only_gates_off_archive_redirects(self):
        os.environ["WAYBACK_URL"] = (
            "https://web.archive.org/web/20200101000000/http://example.com/"
        )
        os.environ["ARCHIVE_ONLY"] = "1"
        dl = WaybackDownloader(Config())

        def _resp(location):
            r = requests.Response()
            r.url = "https://web.archive.org/web/20200101000000id_/http://example.com/a"
            r.status_code = 302
            r.headers["location"] = location
            return r

        # A captured redirect whose target leaves web.archive.org is dropped.
        assert dl.session.get_redirect_target(
            _resp("http://example.com/live-redirect-target")
        ) is None
        # A redirect that stays on web.archive.org is still followed.
        on_archive = "/web/20200101000000id_/http://example.com/b"
        assert dl.session.get_redirect_target(_resp(on_archive)) == on_archive
        # Hostname comparison is case-insensitive (RFC 4343) — an uppercase
        # archive host must still be recognized as on-archive.
        upper = "https://WEB.ARCHIVE.ORG/web/20200101000000id_/http://example.com/c"
        assert dl.session.get_redirect_target(_resp(upper)) == upper

    def test_no_redirect_gate_installed_when_archive_only_off(self):
        os.environ["WAYBACK_URL"] = (
            "https://web.archive.org/web/20200101000000/http://example.com/"
        )
        os.environ.pop("ARCHIVE_ONLY", None)
        dl = WaybackDownloader(Config())
        # Off-archive redirect targets pass through untouched when the flag
        # is off — the gate is only installed in archive_only mode.
        r = requests.Response()
        r.url = "https://web.archive.org/web/20200101000000id_/http://example.com/a"
        r.status_code = 302
        r.headers["location"] = "http://example.com/live"
        assert dl.session.get_redirect_target(r) == "http://example.com/live"


class TestAutoSearchSnapshots:
    """Tests for the CDX-backed snapshot auto-search fallback.

    When a URL is gone at the original timestamp AND the brute-force
    +/- timeframe scan finds nothing, the downloader asks the Wayback
    CDX index which timestamps actually have a 200 capture of that URL
    and tries the captures closest to the original timestamp first.
    """

    def setup_method(self):
        os.environ["WAYBACK_URL"] = (
            "https://web.archive.org/web/20200601000000/http://example.com/"
        )
        os.environ["ARCHIVE_ONLY"] = "1"  # block live-origin fallback for clarity
        self.dl = WaybackDownloader(Config())

    def teardown_method(self):
        os.environ.pop("WAYBACK_URL", None)
        os.environ.pop("ARCHIVE_ONLY", None)
        os.environ.pop("AUTO_SEARCH_SNAPSHOTS", None)
        os.environ.pop("AUTO_SEARCH_SNAPSHOTS_LIMIT", None)

    def test_auto_search_snapshots_default_on(self):
        os.environ.pop("AUTO_SEARCH_SNAPSHOTS", None)
        assert Config().auto_search_snapshots is True

    def test_auto_search_snapshots_can_be_disabled(self):
        os.environ["AUTO_SEARCH_SNAPSHOTS"] = "0"
        assert Config().auto_search_snapshots is False

    def test_cdx_results_sorted_by_proximity_to_original(self):
        """The closest captures must be tried first.

        Original timestamp is 2020-06-01. CDX returns four captures, in
        arbitrary order. We expect them sorted by absolute distance.
        """
        cdx_rows = [
            ["timestamp", "statuscode", "mimetype"],
            ["20100101000000", "200", "image/png"],   # ~10 years before
            ["20200701000000", "200", "image/png"],   # ~1 month  after  <- closest
            ["20250101000000", "200", "image/png"],   # ~4.5 years after
            ["20200601000000", "200", "image/png"],   # original itself — dropped
        ]
        resp = Mock()
        resp.json = Mock(return_value=cdx_rows)
        resp.raise_for_status = Mock()
        with patch.object(self.dl.session, "get", return_value=resp) as mock_get:
            order = self.dl._search_snapshots_via_cdx("http://example.com/logo.png")
        # Original timestamp is dropped; remaining sorted closest-first.
        # 2020-07 is ~1 mo from 2020-06; 2025-01 is ~4.6 yrs; 2010-01 is ~10.4 yrs.
        assert order == ["20200701000000", "20250101000000", "20100101000000"]
        # CDX endpoint was actually hit.
        assert "cdx/search/cdx" in mock_get.call_args.args[0]

    def test_cdx_results_cached_per_url(self):
        cdx_rows = [["timestamp", "statuscode", "mimetype"],
                    ["20200701000000", "200", "image/png"]]
        resp = Mock()
        resp.json = Mock(return_value=cdx_rows)
        resp.raise_for_status = Mock()
        with patch.object(self.dl.session, "get", return_value=resp) as mock_get:
            self.dl._search_snapshots_via_cdx("http://example.com/logo.png")
            self.dl._search_snapshots_via_cdx("http://example.com/logo.png")
        # Second call must be served from cache, no second CDX request.
        assert mock_get.call_count == 1

    def test_cdx_recovers_asset_after_timeframe_scan_fails(self):
        """End-to-end: a 404'd asset is recovered via a CDX snapshot."""
        # Track which Wayback URLs hand back content. The CDX snapshot at
        # 20200701000000 has the asset; every other Wayback URL 404s.
        good_wayback = (
            "https://web.archive.org/web/20200701000000im_/"
            "http://example.com/logo.png"
        )

        def _fake_get(url, *a, **kw):
            if "cdx/search/cdx" in url:
                r = Mock()
                r.json = Mock(return_value=[
                    ["timestamp", "statuscode", "mimetype"],
                    ["20200701000000", "200", "image/png"],
                ])
                r.raise_for_status = Mock()
                return r
            if url == good_wayback:
                r = Mock()
                r.status_code = 200
                r.content = b"RECOVERED-PNG-BYTES"
                r.raise_for_status = Mock()
                return r
            # All other Wayback hits behave like a 404.
            if "web.archive.org" in url:
                r = Mock()
                r.status_code = 404
                err = requests.exceptions.HTTPError(response=r)
                r.raise_for_status = Mock(side_effect=err)
                return r
            # No request should reach the live origin in archive_only mode.
            raise AssertionError(f"unexpected non-archive request: {url}")

        with patch.object(self.dl.session, "get", side_effect=_fake_get):
            content = self.dl.download_file("http://example.com/logo.png")
        assert content == b"RECOVERED-PNG-BYTES"

    def test_cdx_skipped_when_auto_search_disabled(self):
        os.environ["AUTO_SEARCH_SNAPSHOTS"] = "0"
        dl = WaybackDownloader(Config())

        def _all_404(url, *a, **kw):
            if "cdx/search/cdx" in url:
                raise AssertionError("CDX must not be queried when disabled")
            r = Mock()
            r.status_code = 404
            err = requests.exceptions.HTTPError(response=r)
            r.raise_for_status = Mock(side_effect=err)
            return r

        with patch.object(dl.session, "get", side_effect=_all_404):
            assert dl.download_file("http://example.com/logo.png") is None
