"""Tests for `WaybackDownloader.download_file`'s general HTML-error
masquerade rejection (absorbed from the dashboard's wayback_resume_shim).
Distinct from the existing font-corruption check, which catches only
WOFF/TTF/EOT/OTF responses with HTML bodies — the masquerade gate covers
every binary slot."""
import pytest

from wayback_archive.config import Config
from wayback_archive.downloader import WaybackDownloader


@pytest.fixture
def dl(tmp_path, monkeypatch):
    monkeypatch.setenv("WAYBACK_URL", "https://web.archive.org/web/20240101000000/http://x.com/")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    cfg = Config()
    # reject_html_masquerade defaults True; explicit for clarity.
    cfg.reject_html_masquerade = True
    return WaybackDownloader(cfg)


class _Resp:
    def __init__(self, content, status=200):
        self.status_code = status
        self.content = content
    def raise_for_status(self):
        if self.status_code >= 400:
            from requests.exceptions import HTTPError
            err = HTTPError(); err.response = self  # noqa: E702
            raise err


def test_html_in_png_slot_returns_none(dl, monkeypatch):
    monkeypatch.setattr(dl.session, "get",
                        lambda *a, **kw: _Resp(b"<!DOCTYPE html><html>err</html>"))
    assert dl.download_file("http://x.com/a.png") is None


def test_html_in_css_slot_returns_none(dl, monkeypatch):
    monkeypatch.setattr(dl.session, "get",
                        lambda *a, **kw: _Resp(b"<html>error</html>"))
    assert dl.download_file("http://x.com/styles.css") is None


def test_real_png_bytes_pass_through(dl, monkeypatch):
    monkeypatch.setattr(dl.session, "get",
                        lambda *a, **kw: _Resp(b"\x89PNG\r\n\x1a\nrealdata"))
    assert dl.download_file("http://x.com/a.png") == b"\x89PNG\r\n\x1a\nrealdata"


def test_html_in_html_slot_passes(dl, monkeypatch):
    """An HTML page returning HTML is fine."""
    monkeypatch.setattr(dl.session, "get",
                        lambda *a, **kw: _Resp(b"<!DOCTYPE html><html><body></body></html>"))
    got = dl.download_file("http://x.com/page.html")
    assert got is not None
    assert b"<body>" in got


def test_flag_off_keeps_corrupt_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("WAYBACK_URL", "https://web.archive.org/web/20240101000000/http://x.com/")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    cfg = Config()
    cfg.reject_html_masquerade = False
    dl = WaybackDownloader(cfg)
    monkeypatch.setattr(dl.session, "get",
                        lambda *a, **kw: _Resp(b"<!DOCTYPE html>"))
    # With the gate off, the bytes are returned verbatim (consumer must
    # handle the corruption themselves — the standalone CLI's historical
    # behavior).
    assert dl.download_file("http://x.com/a.png") == b"<!DOCTYPE html>"
