"""Tests for `WaybackDownloader.repair()` — targeted re-fetch of an
explicit rel-path list (absorbed from the dashboard's
wayback_repair_shim._fetch_rel loop)."""
import json
import threading
from unittest.mock import patch

import pytest

from wayback_archive import cdx as upstream_cdx
from wayback_archive.config import Config
from wayback_archive.downloader import WaybackDownloader
from wayback_archive.repair import RepairResult, RepairSummary


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("WAYBACK_URL", "https://web.archive.org/web/20240101000000/http://x.com/")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    return Config()


def _make_resp(status, content=b""):
    class _R:
        status_code = status
        def __init__(self):
            self.content = content
            self.headers = {}
    return _R()


def test_all_ok_at_primary(cfg, monkeypatch):
    dl = WaybackDownloader(cfg)

    def fake_download(self, url):
        return f"bytes:{url}".encode()

    monkeypatch.setattr(WaybackDownloader, "download_file", fake_download)
    summary = dl.repair(["a.png", "b.css", "c.js"], workers=2)
    assert summary.ok == 3
    assert summary.failed == 0
    assert summary.skipped == 0
    assert summary.fallback_hits == 0
    assert summary.unrecoverable == []


def test_alt_snapshot_recovers_after_primary_miss(cfg, monkeypatch):
    dl = WaybackDownloader(cfg)

    def fake_download(self, url):
        return None  # primary miss
    monkeypatch.setattr(WaybackDownloader, "download_file", fake_download)

    monkeypatch.setattr(
        "wayback_archive.cdx.alt_timestamps",
        lambda url, prefer_ts, limit, urlopen=None: ["20230501000000"],
    )
    monkeypatch.setattr(
        "wayback_archive.cdx.raw_fetch",
        lambda session, ts, url, on_response=None: b"alt-bytes",
    )

    received = []
    summary = dl.repair(["a.png"], workers=1, on_result=received.append)
    assert summary.ok == 1
    assert summary.fallback_hits == 1
    assert received[0].used_ts == "20230501000000"
    assert received[0].from_fallback is True


def test_all_alts_404_marks_unrecoverable(cfg, monkeypatch):
    dl = WaybackDownloader(cfg)

    monkeypatch.setattr(WaybackDownloader, "download_file", lambda self, url: None)
    monkeypatch.setattr(
        "wayback_archive.cdx.alt_timestamps",
        lambda url, prefer_ts, limit, urlopen=None: ["20230101000000", "20230201000000"],
    )
    monkeypatch.setattr(
        "wayback_archive.cdx.raw_fetch",
        lambda session, ts, url, on_response=None: None,
    )

    summary = dl.repair(["dead.png"], workers=1)
    assert summary.ok == 0
    assert summary.failed == 1
    assert summary.unrecoverable == ["dead.png"]


def test_transient_cdx_stays_recoverable(cfg, monkeypatch):
    dl = WaybackDownloader(cfg)
    monkeypatch.setattr(WaybackDownloader, "download_file", lambda self, url: None)

    def boom(*a, **kw):
        raise upstream_cdx.TransientCDXError("network down")

    monkeypatch.setattr("wayback_archive.cdx.alt_timestamps", boom)
    summary = dl.repair(["maybe.png"], workers=1)
    assert summary.failed == 1
    # Not unrecoverable — transient failure must stay retryable.
    assert summary.unrecoverable == []


def test_already_on_disk_skipped(cfg, monkeypatch, tmp_path):
    dl = WaybackDownloader(cfg)
    local = tmp_path / "present.png"
    local.write_bytes(b"GIF89a\x00real")

    def boom_download(self, url):
        raise AssertionError("should not fetch — file is already present")

    monkeypatch.setattr(WaybackDownloader, "download_file", boom_download)
    summary = dl.repair(["present.png"], workers=1)
    assert summary.ok == 0  # skipped, not counted as a download
    assert summary.skipped == 1
    assert summary.fallback_hits == 0


def test_on_disk_html_masquerade_busts_and_refetches(cfg, monkeypatch, tmp_path):
    dl = WaybackDownloader(cfg)
    local = tmp_path / "fake.png"
    local.write_bytes(b"<!DOCTYPE html><html>err</html>")

    fetched = []

    def fake_download(self, url):
        fetched.append(url)
        return b"\x89PNG\r\n\x1a\nreal"

    monkeypatch.setattr(WaybackDownloader, "download_file", fake_download)
    summary = dl.repair(["fake.png"], workers=1)
    assert summary.ok == 1
    assert summary.skipped == 0
    assert fetched, "masquerade must trigger a real fetch"
    assert local.read_bytes() == b"\x89PNG\r\n\x1a\nreal"


def test_on_result_called_once_per_rel(cfg, monkeypatch):
    dl = WaybackDownloader(cfg)
    monkeypatch.setattr(WaybackDownloader, "download_file",
                        lambda self, url: f"x:{url}".encode())
    rels = ["a", "b", "c", "d", "e"]
    seen = []
    summary = dl.repair(rels, workers=3, on_result=seen.append)
    assert len(seen) == len(rels)
    assert {r.rel for r in seen} == set(rels)


def test_dedups_duplicate_rel_paths(cfg, monkeypatch):
    dl = WaybackDownloader(cfg)
    fetched = []
    monkeypatch.setattr(
        WaybackDownloader, "download_file",
        lambda self, url: (fetched.append(url) or b"data"),
    )
    summary = dl.repair(["x.png", "x.png", "x.png"], workers=1)
    assert summary.ok == 1
    assert len(fetched) == 1


def test_unsafe_path_traversal_skipped(cfg, monkeypatch):
    dl = WaybackDownloader(cfg)

    def boom(self, url):
        raise AssertionError("should not fetch unsafe rel")

    monkeypatch.setattr(WaybackDownloader, "download_file", boom)
    summary = dl.repair(["../../etc/passwd"], workers=1)
    # The unsafe path is skipped silently.
    assert summary.ok == 0
    assert summary.failed == 0


def test_thread_local_downloaders(cfg, monkeypatch):
    """Each worker thread gets its own WaybackDownloader (own session)."""
    dl = WaybackDownloader(cfg)
    seen_sessions = []
    seen_lock = threading.Lock()

    barrier = threading.Barrier(3)

    def fake_download(self, url):
        with seen_lock:
            seen_sessions.append(id(self.session))
        barrier.wait(timeout=5)
        return b"x"

    monkeypatch.setattr(WaybackDownloader, "download_file", fake_download)
    rels = ["a.png", "b.png", "c.png"]
    dl.repair(rels, workers=3)
    assert len(set(seen_sessions)) == 3  # three distinct sessions


def test_cross_origin_rel_routes_to_correct_host_https(cfg, monkeypatch):
    """A rel path whose first segment is a hostname (Google Fonts,
    Squarespace CDN, etc.) must be fetched from THAT host over HTTPS —
    modern CDNs are HTTPS-only and using the snapshot's own http://
    would 308 / fail."""
    dl = WaybackDownloader(cfg)
    fetched = []

    def fake_download(self, url):
        fetched.append(url)
        return b"bytes"

    monkeypatch.setattr(WaybackDownloader, "download_file", fake_download)
    dl.repair(
        [
            "fonts.googleapis.com/css/foo.css",
            "assets/local.png",
            "images.squarespace-cdn.com/content/x.jpg",
        ],
        workers=1,
    )
    # Cross-origin rels go HTTPS; primary-host rel uses snapshot scheme.
    assert "https://fonts.googleapis.com/css/foo.css" in fetched
    assert "http://x.com/assets/local.png" in fetched
    assert "https://images.squarespace-cdn.com/content/x.jpg" in fetched


def test_long_tld_hosts_are_recognized(cfg, monkeypatch):
    """Modern long TLDs (.technology, .engineering, .international, etc.)
    must match the hostname heuristic — capping at 6 chars excluded them."""
    dl = WaybackDownloader(cfg)
    fetched = []

    def fake_download(self, url):
        fetched.append(url)
        return b"bytes"

    monkeypatch.setattr(WaybackDownloader, "download_file", fake_download)
    dl.repair(
        [
            "cdn.example.technology/app.js",
            "api.foo.international/endpoint",
            "x.bar.engineering/style.css",
        ],
        workers=1,
    )
    assert "https://cdn.example.technology/app.js" in fetched
    assert "https://api.foo.international/endpoint" in fetched
    assert "https://x.bar.engineering/style.css" in fetched


def test_well_known_path_is_not_treated_as_hostname(cfg, monkeypatch):
    """`.well-known/security.txt` is a legitimate path under the primary
    host — must NOT be treated as hostname `.well-known`."""
    dl = WaybackDownloader(cfg)
    fetched = []

    def fake_download(self, url):
        fetched.append(url)
        return b"bytes"

    monkeypatch.setattr(WaybackDownloader, "download_file", fake_download)
    dl.repair([".well-known/security.txt", "v1.2/app.js"], workers=1)
    assert "http://x.com/.well-known/security.txt" in fetched
    assert "http://x.com/v1.2/app.js" in fetched
    # No bogus hostnames.
    assert not any("//.well-known/" in u or "//v1.2/" in u for u in fetched)


def test_search_snapshots_via_cdx_routes_through_cdx_urlopen(cfg, monkeypatch):
    """`_search_snapshots_via_cdx` is the CDX recovery used by normal
    crawl runs (not just repair). Must honor `cdx_urlopen` so the
    dashboard's shared rate-limit budget covers both code paths."""
    calls = []

    class _Resp:
        def __init__(self, body): self._body = body
        def read(self): return self._body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=15):
        calls.append(req.full_url)
        return _Resp(b'[["timestamp","statuscode","mimetype"]]')

    cfg.cdx_urlopen = fake_urlopen
    dl = WaybackDownloader(cfg)
    dl._search_snapshots_via_cdx("http://x.com/missing.png")
    # Two windowed queries (before-anchor + after-anchor).
    assert len(calls) == 2
    assert all("cdx/search/cdx" in u for u in calls)


def test_query_hash_suffix_kept_when_flag_off(cfg, monkeypatch):
    """A filename literally containing `.q-<8hex>` (a legitimate user
    file, not a query-suffix marker) must NOT be stripped when
    `query_string_suffix` is off."""
    cfg.query_string_suffix = False
    dl = WaybackDownloader(cfg)
    fetched = []

    def fake_download(self, url):
        fetched.append(url)
        return b"bytes"

    monkeypatch.setattr(WaybackDownloader, "download_file", fake_download)
    dl.repair(["assets/logo.q-deadbeef.png"], workers=1)
    assert fetched == ["http://x.com/assets/logo.q-deadbeef.png"]


def test_root_level_file_is_primary_host_not_treated_as_hostname(cfg, monkeypatch):
    """A bare root-level file like `foo.png` (dot in name, no `/` after)
    must NOT be interpreted as a hostname `foo.png`."""
    dl = WaybackDownloader(cfg)
    fetched = []

    def fake_download(self, url):
        fetched.append(url)
        return b"bytes"

    monkeypatch.setattr(WaybackDownloader, "download_file", fake_download)
    dl.repair(["foo.png"], workers=1)
    assert fetched == ["http://x.com/foo.png"]


def test_query_hash_suffix_is_stripped_from_rel(cfg, monkeypatch):
    """Rel paths with a `.q-<hash>` suffix (produced by
    `query_string_suffix` on-disk filenames) lose the suffix on repair —
    the exact query can't be reconstructed from the sha1, so we fetch
    the query-less variant as best-effort. Only when the flag is on."""
    cfg.query_string_suffix = True
    dl = WaybackDownloader(cfg)
    fetched = []

    def fake_download(self, url):
        fetched.append(url)
        return b"bytes"

    monkeypatch.setattr(WaybackDownloader, "download_file", fake_download)
    dl.repair(["assets/foo.q-abcd1234.png"], workers=1)
    assert fetched == ["http://x.com/assets/foo.png"]


def test_masquerade_check_applied_to_alt_timestamp_fetch(cfg, monkeypatch):
    """`_fetch_at_timestamp` must reject Wayback-error-page masquerade
    bytes returned for a binary URL, not just the primary fetch."""
    from wayback_archive.config import Config

    cfg2 = Config()
    cfg2.reject_html_masquerade = True
    dl = WaybackDownloader(cfg2)

    class _R:
        status_code = 200
        content = b"<!DOCTYPE html><html>err</html>"
        def raise_for_status(self): pass

    monkeypatch.setattr(dl, "session", type("S", (), {"get": lambda *a, **kw: _R()})())
    out = dl._fetch_at_timestamp("http://x.com/foo.png", "20240101000000", False)
    assert out is None
