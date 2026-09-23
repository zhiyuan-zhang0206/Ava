"""Durable --disable-service marker — the bridge that keeps an operator's
`ava start --disable-service X` honored by the watchdog. See
shared/disabled_services.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shared import disabled_services as ds


@pytest.fixture(autouse=True)
def _marker_in_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the marker at a per-test tmp file (default resolves under $AVA_HOME)."""
    monkeypatch.setattr(ds, "disabled_services_file", lambda: tmp_path / "disabled_services")


def test_read_absent_marker_is_empty() -> None:
    assert ds.read_skipped() == set()


def test_write_then_read_roundtrips() -> None:
    ds.write_skipped({"labeler", "memory-indexer"})
    assert ds.read_skipped() == {"labeler", "memory-indexer"}


def test_write_empty_clears() -> None:
    ds.write_skipped({"labeler"})
    assert ds.read_skipped() == {"labeler"}
    ds.write_skipped(set())
    assert ds.read_skipped() == set()


def test_names_normalize_across_kebab_and_snake() -> None:
    # session name is kebab (memory-indexer); the watchdog check name is snake
    # (memory_indexer). Both must compare equal so a skip is not silently missed.
    ds.write_skipped({"memory_indexer"})
    assert ds.read_skipped() == {"memory-indexer"}
    assert ds.is_skipped("memory_indexer", ds.read_skipped())
    assert ds.is_skipped("memory-indexer", ds.read_skipped())


def test_resolve_launch_skip_persist_writes_marker() -> None:
    """Operator start: persist=True records the durable intent and returns it."""
    launch = ds.resolve_launch_skip({"labeler"}, persist=True)
    assert launch == {"labeler"}
    assert ds.read_skipped() == {"labeler"}


def test_resolve_launch_skip_no_persist_reads_and_unions_without_writing() -> None:
    """Internal restart: persist=False keeps the marker, returns marker ∪ transient."""
    ds.write_skipped({"labeler"})  # operator's durable intent already on disk
    launch = ds.resolve_launch_skip({"frontend"}, persist=False)
    assert launch == {"labeler", "frontend"}  # union for this launch
    assert ds.read_skipped() == {"labeler"}  # marker unchanged — frontend was transient
