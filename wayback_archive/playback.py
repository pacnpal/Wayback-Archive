"""Wayback-playback quirks shared between the crawler and any consumer.

When IA's Wayback Machine has no real capture of an asset, it often replays
the request as an HTML error page served under the asset's original content
type — a `.gif` URL whose body is `<!DOCTYPE html>…`. Any consumer that
saves these bytes to disk ends up with corrupt binaries.

`looks_like_html_error()` is a fast (first-512-byte) sniff that returns True
when a response body starts with HTML markup but the URL's extension says it
should be a non-HTML asset. Callers use it to reject Wayback error
masquerades before they hit disk.
"""
from __future__ import annotations
import re
from urllib.parse import urlparse

# Extensions whose bodies must NOT start with HTML. Includes binary asset
# formats plus the few text formats (CSS/JS/JSON/XML) Wayback also returns
# as HTML error pages, and `.map` for NCSA/CERN-format image-map files
# (1993–late-1990s) whose live CGI handler Wayback no longer runs.
BINARY_EXTS: frozenset[str] = frozenset({
    ".gif", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".ico", ".svg",
    ".tif", ".tiff", ".pdf", ".zip", ".gz", ".tar", ".7z", ".rar",
    ".swf", ".mp3", ".mp4", ".m4a", ".m4v", ".mov", ".wav", ".ogg",
    ".webm", ".avi", ".mkv", ".flv", ".woff", ".woff2", ".ttf", ".otf",
    ".eot", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pps", ".pptx",
    ".css", ".js", ".json", ".xml", ".rss", ".atom",
    ".map",
})

HTML_SNIFF = re.compile(
    rb"^\s*(<!doctype\s+html|<html[\s>]|<head[\s>]|<!--\s*saved|<\?xml[^>]*>\s*<html)",
    re.IGNORECASE,
)


def url_ext(url: str) -> str:
    """Return the URL's path-filename extension (lowercased, with leading dot),
    or empty string for no extension. Query strings are ignored."""
    try:
        path = urlparse(url).path
    except Exception:
        return ""
    _, _, last = path.rpartition("/")
    _, dot, ext = last.rpartition(".")
    return ("." + ext.lower()) if dot else ""


def looks_like_html_error(body: bytes, ext: str) -> bool:
    """True if `body` starts with HTML-ish bytes but the URL's extension
    indicates a non-HTML asset. Used to reject Wayback 404 / error pages
    that come back masquerading as binary assets."""
    if not body or ext not in BINARY_EXTS:
        return False
    head = body[:512].lstrip(b"\xef\xbb\xbf")
    # SVG is XML-ish so allow <?xml at the start if we're actually an SVG.
    if ext == ".svg" and head.startswith(b"<?xml"):
        return False
    return bool(HTML_SNIFF.match(head))
