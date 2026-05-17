"""Atomic write helpers — write to a same-directory tempfile, then rename.

Crash-safety primitive used wherever a partial write would be worse than
no write at all: saved asset bytes that another worker may resume from,
sidecar JSON files that a consumer reads back, etc. `os.replace` is atomic
on POSIX and Windows for paths on the same filesystem.
"""
from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically. Creates parent dirs if missing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".part.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, obj: Any) -> None:
    """Serialize `obj` to JSON and write it to `path` atomically. Parent
    must exist; mirrors the dashboard's sidecar-writer behavior."""
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
