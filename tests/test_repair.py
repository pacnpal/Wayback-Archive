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
        def __init__(self): self.content = content
        headers = {}
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
