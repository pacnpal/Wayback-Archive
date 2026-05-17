"""Tests for wayback_archive.cdx — bare CDX/playback protocol helpers."""
import io
import json
import pytest
from unittest.mock import MagicMock

from wayback_archive.cdx import (
    TransientCDXError,
    alt_timestamps,
    raw_fetch,
)


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(body: bytes):
    def _open(req, timeout=15):
        return _FakeResp(body)
    return _open


def test_alt_timestamps_empty_body_returns_empty():
    """A clean 'no other captures' result is an empty body, NOT a transient
    failure. Must NOT raise."""
    got = alt_timestamps("http://x/", "20240101000000", urlopen=_fake_urlopen(b""))
    assert got == []


def test_alt_timestamps_parses_and_sorts_by_proximity():
    # Proximity is raw-int distance on the 14-digit timestamp, mirroring the
    # dashboard's heuristic. Same-month captures are closer than prior-year.
    body = json.dumps([
        ["timestamp", "statuscode"],
        ["20240101000000", "200"],   # excluded (== prefer)
        ["20240105120000", "200"],   # +4 days   → closest by raw int
        ["20240201000000", "200"],   # +1 month
        ["20230101000000", "200"],   # -1 year   → farthest
    ]).encode()
    got = alt_timestamps("http://x/", "20240101000000", urlopen=_fake_urlopen(body))
    assert got == ["20240105120000", "20240201000000", "20230101000000"]


def test_alt_timestamps_unparseable_body_raises_transient():
    bad = b"<html>error</html>"
    with pytest.raises(TransientCDXError):
        alt_timestamps("http://x/", "20240101000000", urlopen=_fake_urlopen(bad))


def test_alt_timestamps_transport_error_raises_transient():
    def boom(req, timeout=15):
        raise OSError("network down")
    with pytest.raises(TransientCDXError):
        alt_timestamps("http://x/", "20240101000000", urlopen=boom)


def test_alt_timestamps_empty_list_returns_empty():
    body = json.dumps([]).encode()
    got = alt_timestamps("http://x/", "20240101000000", urlopen=_fake_urlopen(body))
    assert got == []


def test_alt_timestamps_excludes_prefer_ts():
    body = json.dumps([
        ["timestamp", "statuscode"],
        ["20240101000000", "200"],
    ]).encode()
    got = alt_timestamps("http://x/", "20240101000000", urlopen=_fake_urlopen(body))
    assert got == []


# --- raw_fetch ---------------------------------------------------------------

class _Resp:
    def __init__(self, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


def test_raw_fetch_returns_content_on_200():
    session = MagicMock()
    session.get.return_value = _Resp(200, b"asset-bytes")
    got = raw_fetch(session, "20240101000000", "http://x/a.gif")
    assert got == b"asset-bytes"


def test_raw_fetch_clean_miss_404_returns_none():
    session = MagicMock()
    session.get.return_value = _Resp(404)
    assert raw_fetch(session, "20240101000000", "http://x/a.gif") is None


def test_raw_fetch_200_empty_body_returns_none():
    session = MagicMock()
    session.get.return_value = _Resp(200, b"")
    assert raw_fetch(session, "20240101000000", "http://x/a.gif") is None


def test_raw_fetch_429_raises_transient():
    session = MagicMock()
    session.get.return_value = _Resp(429)
    with pytest.raises(TransientCDXError):
        raw_fetch(session, "20240101000000", "http://x/a.gif")


def test_raw_fetch_transport_error_raises_transient():
    session = MagicMock()
    session.get.side_effect = OSError("boom")
    with pytest.raises(TransientCDXError):
        raw_fetch(session, "20240101000000", "http://x/a.gif")


def test_raw_fetch_off_archive_redirect_returns_none():
    # An id_ snapshot of a 302 → off-archive origin returns None instead of
    # chasing the live host.
    session = MagicMock()
    session.get.return_value = _Resp(302, b"", {"Location": "http://dead.example.com/"})
    assert raw_fetch(session, "20240101000000", "http://x/a.gif") is None


def test_raw_fetch_on_archive_redirect_followed():
    session = MagicMock()
    first = _Resp(302, b"", {"Location": "https://web.archive.org/web/20240101000000id_/http://x/b.gif"})
    second = _Resp(200, b"final")
    session.get.side_effect = [first, second]
    assert raw_fetch(session, "20240101000000", "http://x/a.gif") == b"final"


def test_raw_fetch_on_response_invoked_on_success():
    session = MagicMock()
    session.get.return_value = _Resp(200, b"ok")
    calls = []
    raw_fetch(session, "20240101000000", "http://x/a.gif",
              on_response=lambda r, u: calls.append((r.status_code, u)))
    assert len(calls) == 1
    assert calls[0][0] == 200


def test_raw_fetch_on_response_invoked_on_429():
    session = MagicMock()
    session.get.return_value = _Resp(429)
    calls = []
    with pytest.raises(TransientCDXError):
        raw_fetch(session, "20240101000000", "http://x/a.gif",
                  on_response=lambda r, u: calls.append(r.status_code))
    assert calls == [429]


def test_raw_fetch_on_response_hook_exceptions_swallowed():
    session = MagicMock()
    session.get.return_value = _Resp(200, b"ok")

    def boom(r, u):
        raise RuntimeError("hook broken")

    # Should not propagate the hook's RuntimeError.
    got = raw_fetch(session, "20240101000000", "http://x/a.gif", on_response=boom)
    assert got == b"ok"
