"""Dataclasses for `WaybackDownloader.repair()` results.

Kept in a separate module so callers can import the result types without
pulling the full downloader. The repair() method itself lives on
`WaybackDownloader` (see downloader.py).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal, Optional

RepairStatus = Literal["ok", "fail", "skip"]


@dataclass
class RepairResult:
    """Outcome for a single rel path passed to `WaybackDownloader.repair`."""
    rel: str
    orig_url: str
    index: int
    total: int
    status: RepairStatus
    from_fallback: bool = False
    used_ts: Optional[str] = None
    unrecoverable: bool = False
    bytes_written: int = 0
    tried_alts: int = 0


@dataclass
class RepairSummary:
    """Aggregated outcome of a `WaybackDownloader.repair` run."""
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    fallback_hits: int = 0
    unrecoverable: list[str] = field(default_factory=list)
    duration_s: float = 0.0
