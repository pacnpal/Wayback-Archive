"""Tests for `WaybackDownloader.download_file`'s resume-from-disk behavior
(absorbed from the dashboard's wayback_resume_shim)."""
import os
import time

import pytest

from wayback_archive.config import Config
from wayback_archive.downloader import WaybackDownloader


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("WAYBACK_URL", "https://web.archive.org/web/20240101000000/http://x.com/")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    c = Config()
    c.resume_from_disk = True
    return c


def test_cache_hit_serves_disk_bytes_without_network(cfg, monkeypatch, capsys):
    dl = WaybackDownloader(cfg)
    url = "http://x.com/a.png"
    local = dl._get_local_path(url)
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"GIF89a\x00on-disk")

    # If a network call happens, this fails — the cache short-circuit
    # must return BEFORE session.get.
    def fail_get(*a, **kw):
        raise AssertionError("network call should not happen on cache hit")

    monkeypatch.setattr(dl.session, "get", fail_get)
    got = dl.download_file(url)
    assert got == b"GIF89a\x00on-disk"
    out = capsys.readouterr().out
    assert "[resumed from disk]" in out
    cache_hits, _, _, _ = dl.snapshot_metrics()
    assert cache_hits == 1


def test_cache_bust_on_html_masquerade(cfg, monkeypatch):
    """An on-disk file whose body sniffs as an HTML-error masquerade
    must be unlinked and the call falls through to the network."""
    dl = WaybackDownloader(cfg)
    url = "http://x.com/a.png"
    local = dl._get_local_path(url)
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"<!DOCTYPE html><html>error</html>")

    called = []

    class _Resp:
        status_code = 200
        content = b"GIF89a\x00real"
        def raise_for_status(self): pass

    def fake_get(*a, **kw):
        called.append(1)
        return _Resp()

    monkeypatch.setattr(dl.session, "get", fake_get)
    got = dl.download_file(url)
    assert got == b"GIF89a\x00real"
    assert called  # network was hit
    # The masquerade file got unlinked before the network call.
    # (The new bytes won't be on disk yet — download_file doesn't write,
    # the crawl loop does. So we don't check local.exists() here.)


def test_cache_miss_falls_through_to_network(cfg, monkeypatch):
    dl = WaybackDownloader(cfg)
    url = "http://x.com/missing.png"  # no on-disk file

    class _Resp:
        status_code = 200
        content = b"GIF89a\x00fresh"
        def raise_for_status(self): pass

    monkeypatch.setattr(dl.session, "get", lambda *a, **kw: _Resp())
    got = dl.download_file(url)
    assert got == b"GIF89a\x00fresh"
    cache_hits, net_calls, _, _ = dl.snapshot_metrics()
    assert cache_hits == 0
    assert net_calls == 1


def test_resume_flag_off_skips_disk_check(tmp_path, monkeypatch):
    """With `resume_from_disk=False`, an on-disk file is ignored — the
    network call happens regardless."""
    monkeypatch.setenv("WAYBACK_URL", "https://web.archive.org/web/20240101000000/http://x.com/")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    cfg = Config()
    cfg.resume_from_disk = False
    dl = WaybackDownloader(cfg)
    url = "http://x.com/a.png"
    local = dl._get_local_path(url)
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"GIF89a\x00on-disk-but-ignored")

    class _Resp:
        status_code = 200
        content = b"GIF89a\x00from-net"
        def raise_for_status(self): pass

    monkeypatch.setattr(dl.session, "get", lambda *a, **kw: _Resp())
    got = dl.download_file(url)
    assert got == b"GIF89a\x00from-net"
