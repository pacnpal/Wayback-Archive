"""Tests for wayback_archive.playback — HTML-error masquerade sniffing."""
import pytest

from wayback_archive.playback import (
    BINARY_EXTS,
    looks_like_html_error,
    url_ext,
)


def test_html_sniff_rejects_html_in_binary_slot():
    assert looks_like_html_error(b"<!DOCTYPE html><html>", ".gif") is True
    assert looks_like_html_error(b"<HTML>", ".png") is True
    assert looks_like_html_error(b"  <!-- saved from url -->", ".css") is True


def test_html_sniff_accepts_html_in_html_slot():
    assert looks_like_html_error(b"<!DOCTYPE html>", ".html") is False
    assert looks_like_html_error(b"<html>", ".htm") is False
    # Unknown extension — binary-slot sniff disabled.
    assert looks_like_html_error(b"<html>", "") is False


def test_html_sniff_accepts_real_binary():
    assert looks_like_html_error(b"GIF89a\x00", ".gif") is False
    assert looks_like_html_error(b"\x89PNG\r\n\x1a\n", ".png") is False
    assert looks_like_html_error(b"%PDF-1.4", ".pdf") is False


def test_html_sniff_allows_svg_xml_prolog():
    assert looks_like_html_error(b'<?xml version="1.0"?><svg></svg>', ".svg") is False


def test_html_sniff_empty():
    assert looks_like_html_error(b"", ".gif") is False


def test_html_sniff_strips_utf8_bom():
    # BOM must not defeat the sniff.
    assert looks_like_html_error(b"\xef\xbb\xbf<!DOCTYPE html>", ".gif") is True


def test_html_sniff_covers_xhtml_prolog():
    assert looks_like_html_error(b'<?xml version="1.0"?><html><body></body></html>', ".css") is True


def test_url_ext_with_query():
    assert url_ext("http://x/a/b.png?q=1") == ".png"


def test_url_ext_no_ext():
    assert url_ext("http://x/a/") == ""
    assert url_ext("http://x") == ""


def test_url_ext_dot_in_path_segment_before_filename():
    assert url_ext("http://x/v1.0/foo.css") == ".css"


def test_url_ext_lowercases():
    assert url_ext("http://x/a/IMAGE.PNG") == ".png"


def test_binary_exts_covers_common_assets():
    for e in (".gif", ".jpg", ".png", ".css", ".js", ".woff2", ".pdf", ".map"):
        assert e in BINARY_EXTS
