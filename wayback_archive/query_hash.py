"""Query-string disambiguation hash.

When a downloader saves `foo.png?v=1` and `foo.png?v=2` to disk, both URLs
would collide on the same filename if the query string were dropped. This
helper appends `.q-<sha1(query)[:8]>` to the filename stem so concurrent
variants of the same path coexist.

Pure function with no module state; safe to call from any thread.
"""
from __future__ import annotations
import hashlib

_QUERY_HASH_LEN = 8


def suffix_for_query(query: str) -> str:
    """Return `.q-<sha1(query)[:8]>` for a non-empty query, else empty string."""
    if not query:
        return ""
    h = hashlib.sha1(query.encode("utf-8")).hexdigest()[:_QUERY_HASH_LEN]
    return f".q-{h}"
