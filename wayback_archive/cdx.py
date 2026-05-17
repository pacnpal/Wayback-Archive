"""Wayback CDX index + raw-playback helpers.

Pure protocol layer: no rate-limiting, no proxy reporting. Callers that
need policy on top (a dashboard's gate, a proxy's outcome reporter)
inject it via two kwargs:

  * `urlopen` — replaces stdlib ``urllib.request.urlopen`` for the CDX
    query. A policy wrapper can gate, retry, or instrument it before the
    request goes out.
  * `on_response` — invoked from ``raw_fetch`` after a playback HTTP
    response, with the response object and the request URL. Policy
    callers use it to observe 429s, classify proxy cascade-502s,
    and report outcomes upstream. The hook must not raise.

When neither kwarg is supplied, the helpers behave as plain
HTTP clients.
"""
from __future__ import annotations
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

log = logging.getLogger("wayback_archive.cdx")


class TransientCDXError(Exception):
    """A CDX lookup or raw playback fetch failed for a *retryable* reason —
    the transport raised, IA returned 429, the response body was non-empty
    but unparseable, etc. — as opposed to a clean miss (the URL simply
    isn't archived at that timestamp).

    Callers that need to tell "try again later" apart from "permanently
    gone" should catch this; best-effort callers that only want content
    can treat it like an empty/None result.
    """


def alt_timestamps(
    url: str,
    prefer_ts: str,
    limit: int = 30,
    *,
    urlopen: Callable = urllib.request.urlopen,
) -> list[str]:
    """Ask Wayback CDX for timestamps that archived `url` with statuscode 200,
    excluding `prefer_ts`, sorted by proximity to it.

    Returns ``[]`` for a clean empty result. Raises ``TransientCDXError``
    when the lookup failed for a retryable reason.
    """
    params = {
        "url": url,
        "output": "json",
        "limit": str(limit),
        "fl": "timestamp,statuscode",
        "filter": "statuscode:200",
    }
    q = urllib.parse.urlencode(params)
    cdx = f"https://web.archive.org/cdx/search/cdx?{q}"
    try:
        req = urllib.request.Request(
            cdx, headers={"User-Agent": "Wayback-Archive/1.0"}
        )
        with urlopen(req, timeout=15) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        log.debug("cdx http error url=%s status=%s", url, getattr(e, "code", "?"))
        raise TransientCDXError(f"cdx http {getattr(e, 'code', '?')}: {url}") from e
    except Exception as e:
        log.debug("cdx lookup failed url=%s err=%s", url, e)
        raise TransientCDXError(f"cdx lookup failed: {url}: {e}") from e
    # CDX answers "this URL has no other captures" with an *empty body* —
    # a definitive no-alts result, NOT retryable. Only a non-empty but
    # unparseable body is a real transient failure.
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except ValueError as e:
        log.debug("cdx returned unparseable body url=%s err=%s", url, e)
        raise TransientCDXError(f"cdx unparseable body: {url}: {e}") from e
    if not isinstance(data, list) or len(data) < 2:
        return []
    timestamps = [row[0] for row in data[1:] if row and row[0] != prefer_ts]
    try:
        key = int(prefer_ts)
        timestamps.sort(key=lambda t: abs(int(t) - key))
    except (ValueError, TypeError):
        pass
    return timestamps


def raw_fetch(
    session,
    ts: str,
    url: str,
    timeout: int = 15,
    *,
    on_response: Optional[Callable] = None,
) -> Optional[bytes]:
    """Pull raw archived bytes (no Wayback scripts injected) at a specific
    timestamp.

    Hits the ``/web/<ts>id_/<url>`` playback endpoint. Returns content on
    a 200 with non-empty body, ``None`` on a clean miss (404, empty,
    forbidden, off-archive redirect), and raises ``TransientCDXError``
    when the fetch failed for a retryable reason (429, transport error).

    Redirects whose Location leaves web.archive.org return ``None`` —
    ``id_`` replays the captured response verbatim, so a snapshot of a
    302 to a long-dead origin should not be chased.

    The ``on_response(response, request_url)`` hook (if supplied) is
    invoked exactly once per HTTP exchange, including each intermediate
    redirect hop within the archive — so a policy caller can observe a
    429 or cascade-502 that happens mid-chain. The hook is also called
    with ``response=None`` on transport exceptions so the caller can
    drain any thread-local proxy attribution before the
    ``TransientCDXError`` propagates. It must not raise; exceptions
    inside the hook are swallowed and logged.
    """
    wb = f"https://web.archive.org/web/{ts}id_/{url}"
    current_url = wb
    try:
        r = session.get(current_url, timeout=timeout, allow_redirects=False)
        if on_response is not None:
            _safe_call(on_response, r, current_url)
        hops = 0
        while r.status_code in (301, 302, 303, 307, 308) and hops < 5:
            loc = r.headers.get("Location", "") if r.headers else ""
            if not loc:
                break
            nxt = urllib.parse.urljoin(current_url, loc)
            if urllib.parse.urlparse(nxt).hostname != "web.archive.org":
                return None
            hops += 1
            current_url = nxt
            r = session.get(current_url, timeout=timeout, allow_redirects=False)
            if on_response is not None:
                _safe_call(on_response, r, current_url)
        if r.status_code == 429:
            raise TransientCDXError(f"rate limited (429): {wb}")
        if r.status_code == 200 and r.content:
            return r.content
    except TransientCDXError:
        raise
    except Exception as e:
        if on_response is not None:
            _safe_call(on_response, None, current_url)
        log.debug("raw_fetch error url=%s err=%s", wb, e, exc_info=True)
        raise TransientCDXError(f"raw fetch transport error: {wb}") from e
    return None


def _safe_call(fn: Callable, *args) -> None:
    try:
        fn(*args)
    except Exception as e:
        log.debug("cdx response hook raised: %s", e, exc_info=True)
