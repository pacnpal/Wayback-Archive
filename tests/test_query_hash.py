"""Tests for wayback_archive.query_hash."""
from wayback_archive.query_hash import suffix_for_query


def test_query_hash_stable():
    assert suffix_for_query("v=1") == suffix_for_query("v=1")


def test_query_hash_differs():
    assert suffix_for_query("v=1") != suffix_for_query("v=2")


def test_query_hash_empty():
    assert suffix_for_query("") == ""


def test_query_hash_format():
    s = suffix_for_query("v=1")
    assert s.startswith(".q-")
    assert len(s) == len(".q-") + 8
