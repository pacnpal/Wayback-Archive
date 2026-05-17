"""Tests for wayback_archive.io_atomic."""
import json
import os
import pytest

from wayback_archive.io_atomic import atomic_write_bytes, atomic_write_json


def test_atomic_write_bytes_round_trip(tmp_path):
    p = tmp_path / "sub" / "a.bin"
    atomic_write_bytes(p, b"\x00\x01\x02hello")
    assert p.read_bytes() == b"\x00\x01\x02hello"


def test_atomic_write_bytes_overwrites(tmp_path):
    p = tmp_path / "a.bin"
    atomic_write_bytes(p, b"old")
    atomic_write_bytes(p, b"new")
    assert p.read_bytes() == b"new"


def test_atomic_write_bytes_no_partial_on_failure(tmp_path, monkeypatch):
    # If os.replace fails, neither the tempfile nor the target should remain.
    p = tmp_path / "a.bin"

    def boom(*a, **kw):
        raise OSError("boom")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_bytes(p, b"data")
    assert not p.exists()
    # Tempfile cleaned up too.
    leftovers = [f for f in tmp_path.iterdir() if f.name.startswith(".part.")]
    assert leftovers == []


def test_atomic_write_json_round_trip(tmp_path):
    p = tmp_path / "data.json"
    obj = {"a": 1, "b": ["x", "y"]}
    atomic_write_json(p, obj)
    assert json.loads(p.read_text()) == obj


def test_atomic_write_json_overwrites(tmp_path):
    p = tmp_path / "data.json"
    atomic_write_json(p, [1, 2, 3])
    atomic_write_json(p, {"new": True})
    assert json.loads(p.read_text()) == {"new": True}
