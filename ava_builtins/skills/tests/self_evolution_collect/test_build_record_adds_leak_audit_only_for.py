"""Self evolution collect cases: build record adds leak audit only for."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from ava_builtins.skills.tests.test_self_evolution_collect import _build_minimal_record
from ava_builtins.skills.tests.test_self_evolution_collect import (
    test_fetch_exhausted_diagnostics_are_bounded as test_fetch_exhausted_diagnostics_are_bounded,
)
from base.db.tests.fakes import patch_database
from tests.skills import load_skill_script


def test_build_record_adds_leak_audit_only_for_eval_collection(
    collect_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Weekly collection remains unchanged; eval collection records invalidating reads."""
    paths = collect_mod.LeakPaths(
        memory_pool=str(tmp_path / "memory"),
        self_evolution=str(tmp_path / "self_evolution"),
        workspaces_root=str(tmp_path / "workspaces"),
        agent_workspace=str(tmp_path / "workspaces" / "1"),
    )
    weekly = _build_minimal_record(
        collect_mod,
        monkeypatch,
        [("code", {"body": f"open('{paths.self_evolution}/reports/result.md')"})],
    )
    audited = collect_mod.build_record(
        agent_id=1,
        week="2026-W99",
        events=[("code", {"body": f"open('{paths.self_evolution}/reports/result.md')"})],
        log_events=[],
        inbounds=[],
        meta=("user", "completed", "done"),
        leak_paths=paths,
    )

    assert "leak_audit" not in weekly
    assert "invalidated" not in weekly
    assert audited["invalidated"] is True
    assert audited["leak_audit"][0]["surface"] == "results"


def test_test_label_ids_matches_prefix_only(collect_mod: Any, db_conn: psycopg.Connection) -> None:
    """Only TEST- prefixed role labels are test spawns: a plain agent label
    and a NULL label must stay in the dataset."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents (id, label) VALUES (9001, 'TEST-bench-on-t1'), "
            "(9002, 'test-agent-9002'), (9003, NULL)"
        )
    db_conn.commit()

    with db_conn.cursor() as cur:
        ids = collect_mod._test_label_ids(cur, [9001, 9002, 9003, 99999])

    assert ids == {9001}


def test_test_label_ids_excludes_direct_children_of_test_orchestrators(
    collect_mod: Any, db_conn: psycopg.Connection
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents (id, label) VALUES (9010, 'TEST-orchestrator'), "
            "(9011, 'ordinary-child'), (9012, 'ordinary-agent')"
        )
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES "
            "(9010, 'user', 'terminated'), (9011, 'agent:9010', 'terminated'), "
            "(9012, 'user', 'terminated')"
        )
    db_conn.commit()

    with db_conn.cursor() as cur:
        ids = collect_mod._test_label_ids(cur, [9010, 9011, 9012])

    assert ids == {9010, 9011}


def test_collect_with_counts_keeps_records_and_reports_filters(
    collect_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """collect_with_counts() returns the same records as collect() plus the
    pre-record counts the daily sentinel keys on: distinct window agents
    seen, TEST- drops, missing-lifecycle drops (QA review of PR #698)."""
    rows: list[dict[str, object]] = [
        {"agent_id": 1, "event_name": "turn_end", "attributes": {}},
        {"agent_id": 2, "event_name": "turn_end", "attributes": {}},
        {"agent_id": 3, "event_name": "turn_end", "attributes": {}},
        {"agent_id": 4, "event_name": "turn_end", "attributes": {}},
    ]

    def _fetch(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return rows

    def _no_inbounds(*_args: object, **_kwargs: object) -> dict[int, Any]:
        return {}

    def _meta_for(*_args: object, **_kwargs: object) -> dict[int, tuple[str, str, str]]:
        return dict.fromkeys((1, 2, 3), ("user", "completed", "done"))

    def _test_ids(*_args: object, **_kwargs: object) -> set[int]:
        return {2}

    monkeypatch.setattr(collect_mod, "_fetch_events_window", _fetch)
    monkeypatch.setattr(collect_mod, "_inbounds_by_agent", _no_inbounds)
    monkeypatch.setattr(collect_mod, "_meta_by_agent", _meta_for)
    monkeypatch.setattr(collect_mod, "_test_label_ids", _test_ids)

    # collect() uses `with connect() as conn`, which closes the connection
    # at block exit (psycopg 3 context-manager semantics) — give each call a
    # fresh connection like the real pipeline gets.
    def _fresh_conn() -> psycopg.Connection:
        return psycopg.connect(collect_mod.settings.data_plane.db_url)

    patch_database(monkeypatch, connect=_fresh_conn)

    def _fake_build_record(agent_id: int, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"agent_id": agent_id, "label": "ok"}

    monkeypatch.setattr(collect_mod, "build_record", _fake_build_record)

    records, counts = collect_mod.collect_with_counts(1, "2026-08-26")

    assert counts == {"seen": 4, "excluded_test": 1, "skipped_meta": 1}
    assert [r["agent_id"] for r in records] == [1, 3]

    # include_test opts the TEST- spawns back in for measurement runs.
    records_all, counts_all = collect_mod.collect_with_counts(1, "2026-08-26", include_test=True)
    assert counts_all == {"seen": 4, "excluded_test": 0, "skipped_meta": 1}
    assert [r["agent_id"] for r in records_all] == [1, 2, 3]

    # The plain collect() wrapper stays records-only for existing callers.
    assert collect_mod.collect(1, "2026-08-26") == records


@pytest.fixture(scope="module")
def collect_mod() -> Any:
    """The collection logic module, loaded by path (kebab-case skill dirs never import)."""
    return load_skill_script("platform", "ava-self-evolution", "scripts", "collect.py")
