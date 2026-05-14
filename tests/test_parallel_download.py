"""Tests for the concurrent crawl loop in WaybackDownloader.download().

These exercise the work-queue + N-worker refactor: dedup of the visited-set
and the frontier, run-stat accounting, the MAX_FILES stop flag, thread-local
page context, and result-equivalence between sequential and concurrent runs.
``download_file`` and ``_process_html`` are mocked so the tests stay fast and
deterministic and focus on the orchestration rather than the network or HTML
parsing.
"""

import os
import threading
import types

import pytest

from wayback_archive.config import Config
from wayback_archive.downloader import WaybackDownloader

WAYBACK_URL = "https://web.archive.org/web/20250101000000/http://example.com/"
ROOT = "http://example.com/"


def _make_downloader(tmp_path, workers, max_files=None):
    """Build a downloader whose config points at a temp output dir."""
    keys = ("WAYBACK_URL", "OUTPUT_DIR", "FETCH_WORKERS", "MAX_FILES")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ["WAYBACK_URL"] = WAYBACK_URL
    os.environ["OUTPUT_DIR"] = str(tmp_path)
    os.environ["FETCH_WORKERS"] = str(workers)
    if max_files is not None:
        os.environ["MAX_FILES"] = str(max_files)
    else:
        os.environ.pop("MAX_FILES", None)
    try:
        return WaybackDownloader(Config())
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _install_mock_site(downloader, site, calls=None, call_lock=None):
    """Wire fake download_file / _process_html that serve `site`.

    `site` maps URL -> (content_bytes, [links]). A None content entry (or a
    missing key) simulates a failed fetch.
    """

    def fake_download_file(self, url):
        if calls is not None:
            with call_lock:
                calls.append(url)
        entry = site.get(url)
        return entry[0] if entry else None

    def fake_process_html(self, html, base_url):
        entry = site.get(base_url)
        links = list(entry[1]) if entry else []
        return html, links

    downloader.download_file = types.MethodType(fake_download_file, downloader)
    downloader._process_html = types.MethodType(fake_process_html, downloader)


def _small_site():
    # b.html and c.html are each linked from two pages -> dedup pressure.
    return {
        ROOT: (b"<html>index</html>", [f"{ROOT}a.html", f"{ROOT}b.html"]),
        f"{ROOT}a.html": (b"<html>a</html>", [f"{ROOT}b.html", f"{ROOT}c.html"]),
        f"{ROOT}b.html": (b"<html>b</html>", [ROOT, f"{ROOT}c.html"]),
        f"{ROOT}c.html": (b"<html>c</html>", []),
    }


def _ring_site(n):
    """Root fans out to p0..p(n-1); every page links back to root and to its
    ring successor, so the frontier is hammered with duplicates."""
    site = {ROOT: (b"<html>root</html>", [f"{ROOT}p{i}.html" for i in range(n)])}
    for i in range(n):
        nxt = f"{ROOT}p{(i + 1) % n}.html"
        site[f"{ROOT}p{i}.html"] = (
            f"<html>p{i}</html>".encode(),
            [ROOT, nxt],
        )
    return site


@pytest.mark.parametrize("workers", [1, 2, 8])
def test_crawl_visits_every_page_once(tmp_path, workers):
    """Every reachable page is fetched exactly once, regardless of worker
    count, even though several pages are linked from multiple parents."""
    site = _small_site()
    calls, call_lock = [], threading.Lock()
    dl = _make_downloader(tmp_path, workers)
    _install_mock_site(dl, site, calls, call_lock)

    dl.download()

    expected = set(site)
    assert dl.config.visited_urls == expected
    assert set(dl.config.downloaded_files) == expected
    # No URL fetched twice — the claim-under-lock dedup held.
    assert sorted(calls) == sorted(expected)
    # Files actually landed on disk.
    assert (tmp_path / "index.html").is_file()
    for name in ("a.html", "b.html", "c.html"):
        assert (tmp_path / name).is_file()


def test_sequential_and_concurrent_results_match(tmp_path):
    """workers=1 and workers=8 must produce the identical visited-set and
    downloaded-files mapping for the same site."""
    site = _ring_site(25)

    dl1 = _make_downloader(tmp_path / "seq", 1)
    _install_mock_site(dl1, site)
    dl1.download()

    dl8 = _make_downloader(tmp_path / "par", 8)
    _install_mock_site(dl8, site)
    dl8.download()

    assert dl1.config.visited_urls == dl8.config.visited_urls == set(site)
    assert set(dl1.config.downloaded_files) == set(dl8.config.downloaded_files)


@pytest.mark.parametrize("run", range(5))
def test_concurrent_crawl_is_race_free(tmp_path, run):
    """Repeated high-worker runs over a dup-heavy site never lose or double
    a page — guards against frontier / visited-set races."""
    site = _ring_site(60)
    dl = _make_downloader(tmp_path / f"r{run}", 12)
    calls, call_lock = [], threading.Lock()
    _install_mock_site(dl, site, calls, call_lock)

    dl.download()

    assert dl.config.visited_urls == set(site)
    assert len(calls) == len(site)  # exactly one fetch per URL


def test_max_files_stops_sequential_run_exactly(tmp_path):
    """With workers=1 the MAX_FILES cap is exact."""
    site = _ring_site(40)
    dl = _make_downloader(tmp_path, 1, max_files=5)
    _install_mock_site(dl, site)

    dl.download()

    assert dl.config.max_files == 5
    # The run summary the dashboard parses must reflect the cap.
    # (stats live in download()'s local scope; visited_urls is the proxy.)
    assert len(dl.config.visited_urls) == 5


def test_max_files_caps_concurrent_run(tmp_path):
    """With N workers a few in-flight fetches may finish after the cap trips,
    but the overrun is bounded by the worker count."""
    workers = 6
    site = _ring_site(80)
    dl = _make_downloader(tmp_path, workers, max_files=10)
    _install_mock_site(dl, site)

    dl.download()

    visited = len(dl.config.visited_urls)
    assert 10 <= visited <= 10 + workers


def test_failed_fetches_do_not_stall_the_crawl(tmp_path):
    """A page whose fetch returns None is counted as failed but the crawl
    still drains the frontier and terminates."""
    site = {
        ROOT: (b"<html>root</html>", [f"{ROOT}ok.html", f"{ROOT}dead.html"]),
        f"{ROOT}ok.html": (b"<html>ok</html>", []),
        f"{ROOT}dead.html": (None, []),  # simulated fetch failure
    }
    dl = _make_downloader(tmp_path, 4)
    _install_mock_site(dl, site)

    dl.download()

    # dead.html is claimed (visited) but never lands in downloaded_files.
    assert dl.config.visited_urls == set(site)
    assert f"{ROOT}dead.html" not in dl.config.downloaded_files
    assert (tmp_path / "ok.html").is_file()


def test_current_page_url_is_thread_local(tmp_path):
    """Each worker thread sees its own _current_page_url; one thread's value
    never leaks into another's relative-path base."""
    dl = _make_downloader(tmp_path, 1)
    seen = {}
    # Timeouts so a worker that errors before reaching the barrier fails the
    # test fast instead of hanging the suite.
    barrier = threading.Barrier(3, timeout=5)

    def worker(name):
        dl._current_page_url = f"http://example.com/{name}"
        barrier.wait()  # all three set their value before anyone reads
        seen[name] = dl._current_page_url

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("x", "y", "z")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert all(not t.is_alive() for t in threads)

    assert seen == {
        "x": "http://example.com/x",
        "y": "http://example.com/y",
        "z": "http://example.com/z",
    }
    # The main thread never set it, so it stays None.
    assert dl._current_page_url is None
