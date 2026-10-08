"""Snapshot cursor isolation owned by the graph logging tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fresh_snapshot_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """`node_log._SNAPSHOT_CURSOR` is per-process module state — in production
    one host serves many agents and a test session outlives every test, so the
    cursor set by one test would silently switch a later test's node enter to
    the incremental path. Autouse: every test starts with a clean cursor (first
    enter = full-window snapshot, matching a fresh agent process)."""
    from agent.graph import node_log

    monkeypatch.setattr(node_log, "_SNAPSHOT_CURSOR", {})
