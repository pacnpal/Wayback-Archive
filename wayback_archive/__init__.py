"""Wayback-Archive - A comprehensive tool for downloading websites from Wayback Machine."""

__version__ = "1.4.0"

from wayback_archive.playback import (
    BINARY_EXTS,
    HTML_SNIFF,
    looks_like_html_error,
    url_ext,
)
from wayback_archive.query_hash import suffix_for_query
from wayback_archive.io_atomic import atomic_write_bytes, atomic_write_json
from wayback_archive.cdx import TransientCDXError, alt_timestamps, raw_fetch

__all__ = [
    "BINARY_EXTS",
    "HTML_SNIFF",
    "TransientCDXError",
    "alt_timestamps",
    "atomic_write_bytes",
    "atomic_write_json",
    "looks_like_html_error",
    "raw_fetch",
    "suffix_for_query",
    "url_ext",
]


