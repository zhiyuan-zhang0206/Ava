"""`scripts/lint_time_bomb.py` — the fixed-instant / real-clock test invariant.

A test that asserts an exact equality on a value derived from a repo fixed
instant while the derivation can reach the real clock is correct only while
the wall clock keeps a particular relation to the constant — a relation that
expires (2026-08-30: two such tests went deterministic red within seven days,
each ejecting the Mergify batch queue). The lint has two halves:

- source: a function that accepts a clock parameter must thread it into the
  fixed-instant window boundary instead of letting the callee fall back to
  the real clock (`compute_rollup(now_utc=...)` -> `split_index_label_window`
  without `now=` — the rollup bomb's seedling);
- test: exact `==` on a fixed-instant-derived expression inside a function
  whose derivation reaches an unpinned real-now path (the inspect bomb:
  `client.get(...)` + `== INDEX_LABEL_CUTOVER_AT`).

The two known bombs are reproduced as regression fixtures.
"""

from __future__ import annotations

import importlib
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

_lint = importlib.import_module("scripts.lint_time_bomb")


@pytest.fixture()
def scratch(tmp_path: Path, monkeypatch) -> tuple[Path, Callable[[], Any]]:
    """Point the lint at a scratch repo; returns (root, build_index) so
    fixtures are written BEFORE the index is built (the index is a snapshot)."""
    monkeypatch.setattr(_lint, "_REPO_ROOT", tmp_path)
    for d in (*_lint._SCAN_DIRS, "tests"):
        (tmp_path / d).mkdir(exist_ok=True)

    def build() -> Any:
        return _lint._Index(tmp_path, tuple(_lint._SCAN_DIRS))

    return tmp_path, build


FIXED_FAMILY = """
from datetime import UTC, datetime, timedelta

INDEX_LABEL_CUTOVER_AT = datetime(2026, 8, 23, 11, 0, tzinfo=UTC)
LEGACY_READ_EXPIRES_AT = INDEX_LABEL_CUTOVER_AT + timedelta(hours=168, minutes=10)


def retention_floor(now: datetime | None = None) -> datetime:
    return (now if now is not None else datetime.now(UTC)) - timedelta(hours=168)


def split_index_label_window(start: datetime, end: datetime, now: datetime | None = None):
    current = now if now is not None else datetime.now(UTC)
    return ("legacy", "indexed") if current < LEGACY_READ_EXPIRES_AT else ("indexed",)
"""


def _write(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


# ── rule 1: source clock threading into the fixed-instant world ────────────


def test_unthreaded_split_inside_clocked_function_is_rejected(scratch) -> None:
    root, build = scratch
    _write(root, "shared/loki_index_labels.py", FIXED_FAMILY)
    _write(
        root,
        "services/events_maintenance.py",
        """
        from shared.loki_index_labels import split_index_label_window


        def compute_rollup(*, now_utc):
            day = now_utc.date()
            return _aggregate(day)


        def _aggregate(day):
            return split_index_label_window(day, day)
        """,
    )
    idx = build()
    errors = _lint._lint_source(idx, [idx.root / "services" / "events_maintenance.py"])
    assert len(errors) == 1
    assert "compute_rollup" in errors[0]
    assert "now_utc" in errors[0]
    assert "_aggregate" in errors[0]


def test_threaded_split_is_allowed(scratch) -> None:
    root, build = scratch
    _write(root, "shared/loki_index_labels.py", FIXED_FAMILY)
    _write(
        root,
        "services/events_maintenance.py",
        """
        from shared.loki_index_labels import split_index_label_window


        def compute_rollup(*, now_utc):
            now = now_utc
            return _aggregate(now)


        def _aggregate(now):
            return split_index_label_window(now, now, now=now)
        """,
    )
    errors = _lint._lint_source(build(), [build().root / "services" / "events_maintenance.py"])
    assert errors == []


def test_clocked_function_pinning_the_callee_is_allowed(scratch) -> None:
    """A `now` parameter threaded into the fixed-instant callee is fine."""
    root, build = scratch
    _write(root, "shared/loki_index_labels.py", FIXED_FAMILY)
    _write(
        root,
        "services/noop.py",
        """
        from shared.loki_index_labels import retention_floor


        def plan(window_start, now):
            floor = retention_floor(now)
            return floor, now
        """,
    )
    errors = _lint._lint_source(build(), [build().root / "services" / "noop.py"])
    assert errors == []


# ── rule 2: test-side exact equality on an unpinned fixed instant ──────────


def test_inspect_bomb_exact_equality_after_opaque_http_is_rejected(scratch) -> None:
    """Regression: the 2026-08-30 agent-inspect bomb — `client.get(...)` then
    `assert ... == INDEX_LABEL_CUTOVER_AT`."""
    root, build = scratch
    _write(root, "shared/loki_index_labels.py", FIXED_FAMILY)
    _write(
        root,
        "tests/test_inspect.py",
        """
        from shared.loki_index_labels import INDEX_LABEL_CUTOVER_AT, retention_floor


        class _Client:
            def get(self, url):
                return {"from_": retention_floor()}


        def test_inspect_uses_indexed_window():
            client = _Client()
            lifecycle = client.get("/inspect")
            assert lifecycle["from_"] == INDEX_LABEL_CUTOVER_AT
        """,
    )
    errors = _lint._lint_tests(build(), [build().root / "tests"])
    assert len(errors) == 1
    assert "test_inspect.py:" in errors[0]
    assert "time-bomb test" in errors[0]
    assert "opaque HTTP" in errors[0]


def test_rollup_bomb_unpinned_callee_derivation_is_rejected(scratch) -> None:
    """Regression: the 2026-08-30 rollup bomb — the test pins `now_utc` but the
    production clock is unthreaded, so the exact aggregate still rides the
    wall clock."""
    root, build = scratch
    _write(root, "shared/loki_index_labels.py", FIXED_FAMILY)
    _write(
        root,
        "services/events_maintenance.py",
        """
        from shared.loki_index_labels import split_index_label_window


        def compute_rollup(*, now_utc):
            return _aggregate(now_utc.date())


        def _aggregate(day):
            return split_index_label_window(day, day)
        """,
    )
    _write(
        root,
        "tests/test_rollup.py",
        """
        from shared.loki_index_labels import INDEX_LABEL_CUTOVER_AT

        from services.events_maintenance import compute_rollup


        def test_nonzero_rewrites_day():
            day = INDEX_LABEL_CUTOVER_AT.date()
            result = compute_rollup(db, now_utc=INDEX_LABEL_CUTOVER_AT)
            assert result == (day, day, 1, 1)
        """,
    )
    errors = _lint._lint_tests(build(), [build().root / "tests"])
    assert len(errors) == 1
    assert "clock not pinned" in errors[0]


def test_pinned_clock_exact_equality_is_allowed(scratch) -> None:
    """The fleet-graph shape: the call site passes a fixed-derived `now`, so
    `== INDEX_LABEL_CUTOVER_AT` is deterministic forever."""
    root, build = scratch
    _write(root, "shared/loki_index_labels.py", FIXED_FAMILY)
    _write(
        root,
        "gateway/routers/fleet_graph.py",
        """
        from shared.loki_index_labels import INDEX_LABEL_CUTOVER_AT


        def fetch_edges(*, now):
            return {"to": min(INDEX_LABEL_CUTOVER_AT, now)}
        """,
    )
    _write(
        root,
        "tests/test_fleet.py",
        """
        from datetime import timedelta

        from gateway.routers.fleet_graph import fetch_edges
        from shared.loki_index_labels import INDEX_LABEL_CUTOVER_AT


        def test_edge_tail_is_scoped():
            now = INDEX_LABEL_CUTOVER_AT + timedelta(days=1)
            calls = fetch_edges(now=now)
            assert calls["to"] == INDEX_LABEL_CUTOVER_AT
        """,
    )
    errors = _lint._lint_tests(build(), [build().root / "tests"])
    assert errors == []


def test_tolerance_assertion_is_allowed(scratch) -> None:
    """The post-fix inspect shape: a drift-tolerant compare, never an exact."""
    root, build = scratch
    _write(root, "shared/loki_index_labels.py", FIXED_FAMILY)
    _write(
        root,
        "tests/test_inspect.py",
        """
        from shared.loki_index_labels import retention_floor


        def test_window_floor():
            from_ = retention_floor()
            assert abs((from_ - retention_floor()).total_seconds()) < 10
        """,
    )
    errors = _lint._lint_tests(build(), [build().root / "tests"])
    assert errors == []


def test_explicit_now_to_boundary_function_is_allowed(scratch) -> None:
    """The shared-slice test shape: every boundary call pins `now=`."""
    root, build = scratch
    _write(root, "shared/loki_index_labels.py", FIXED_FAMILY)
    _write(
        root,
        "tests/test_labels.py",
        """
        from datetime import timedelta

        from shared import loki_index_labels as labels


        def test_split_before_cutover():
            cutover = labels.INDEX_LABEL_CUTOVER_AT
            before = labels.split_index_label_window(
                cutover - timedelta(hours=2),
                cutover - timedelta(hours=1),
                now=cutover,
            )
            assert before == ("legacy",)
        """,
    )
    errors = _lint._lint_tests(build(), [build().root / "tests"])
    assert errors == []


def test_opt_out_comment_suppresses(scratch) -> None:
    root, build = scratch
    _write(root, "shared/loki_index_labels.py", FIXED_FAMILY)
    _write(
        root,
        "tests/test_inspect.py",
        """
        from shared.loki_index_labels import INDEX_LABEL_CUTOVER_AT, retention_floor


        class _Client:
            def get(self, url):
                return {"from_": retention_floor()}


        def test_acknowledged():
            client = _Client()
            lifecycle = client.get("/inspect")
            assert lifecycle["from_"] == INDEX_LABEL_CUTOVER_AT  # time-bomb-ok: endpoint pins the constant by definition
        """,
    )
    errors = _lint._lint_tests(build(), [build().root / "tests"])
    assert errors == []


def test_literal_datetime_assertions_are_allowed(scratch) -> None:
    """A test-local `datetime(...)` literal is deterministic by construction —
    no repo fixed instant participates, so no bomb."""
    root, build = scratch
    _write(root, "shared/loki_index_labels.py", FIXED_FAMILY)
    _write(
        root,
        "tests/test_pricing.py",
        """
        from datetime import UTC, datetime


        def test_interval_boundary():
            assert datetime(2026, 8, 10, tzinfo=UTC) == fixed_boundary
        """,
    )
    errors = _lint._lint_tests(build(), [build().root / "tests"])
    assert errors == []
