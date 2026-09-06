"""Contract tests for the C9 daily CI-minute reconciliation schedule."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import psycopg
import pytest

from schedules.catchup import catch_up, claimed_slot, fire_slot_once

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEDULE_PATH = REPO_ROOT / "schedules" / "c9-daily-report-schedule.py"


def _load_schedule_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("c9_daily_report", SCHEDULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _insert_schedule(conn: psycopg.Connection, *, created_at: datetime) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (name, script, command, enabled, status, created_at) "
            "VALUES (%s, 'x', 'python schedule.py', true, 'stopped', %s) RETURNING id",
            (f"c9-test-{created_at.isoformat()}", created_at),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def _entry(agent_id: int | None, *, linux: int, macos: int) -> dict[str, Any]:
    return {
        "run_id": 1 if agent_id is None else agent_id,
        "agent_id": agent_id,
        "linux_minutes": linux,
        "macos_minutes": macos,
    }


class _FakeAccounting(SimpleNamespace):
    """Records collect() windows and returns fixed entries per call."""

    def __init__(self, module: ModuleType) -> None:
        super().__init__()
        self.module = module
        self.windows: list[tuple[str, str]] = []
        self.DEFAULT_REPO = "zhiyuan-zhang0206/Ava"
        self.DEFAULT_LEDGER = Path("ledger.jsonl")
        self.entries = [_entry(5811, linux=10, macos=0)]

    def collect(self, repo: str, since: str, until: str) -> list[dict]:
        del repo
        self.windows.append((since, until))
        return self.entries

    def append_ledger(self, path: Path, fresh: list[dict]) -> int:
        del path, fresh
        return 1


def _install_fake_accounting(monkeypatch: pytest.MonkeyPatch, accounting: Any) -> list[dict]:
    sys.modules["ci_accounting"] = accounting  # type: ignore[assignment]
    emitted: list[dict] = []

    def record_emit(category: str, event_name: str, **kwargs: Any) -> None:
        emitted.append({"category": category, "event_name": event_name, **kwargs})

    monkeypatch.setattr("shared.telemetry.emit", record_emit)
    return emitted


def test_manifest_declares_c9_daily_report_schedule() -> None:
    manifest = json.loads((REPO_ROOT / "schedules" / "manifest.json").read_text())
    entry = next(
        item for item in manifest["builtin_schedules"] if item["name"] == "c9-daily-report"
    )

    assert SCHEDULE_PATH.is_file()
    assert entry["script"] == SCHEDULE_PATH.name
    assert entry["command"] == "python c9-daily-report-schedule.py"
    assert entry["class"] == "product"
    assert entry["default_enabled"] is True
    assert "05:00 cluster time" in entry["description"]


def test_window_bounds_covers_the_slot_day_in_cluster_timezone() -> None:
    module = _load_schedule_module()
    # 05:00 Asia/Shanghai on 2026-09-07 == 21:00 UTC on 2026-09-06: the UTC
    # date differs from the cluster day, which is the label that must win.
    slot_end = datetime(2026, 9, 6, 21, 0, tzinfo=UTC)

    since, until, day = module.window_bounds(slot_end)

    assert since == "2026-09-05T21:00:00Z"
    assert until == "2026-09-06T21:00:00Z"
    assert day == "2026-09-07"


def test_summarize_splits_attribution_and_meters_minutes() -> None:
    module = _load_schedule_module()
    entries = [
        _entry(5811, linux=10, macos=20),
        _entry(405, linux=5, macos=0),
        _entry(None, linux=7, macos=3),
    ]

    payload = module.summarize(
        entries,
        day="2026-09-07",
        window_start="2026-09-05T21:00:00Z",
        window_end="2026-09-06T21:00:00Z",
        appended_runs=2,
    )

    assert payload == {
        "day": "2026-09-07",
        "window_start": "2026-09-05T21:00:00Z",
        "window_end": "2026-09-06T21:00:00Z",
        "runs": 3,
        "attributed_runs": 2,
        "unattributed_runs": 1,
        "total_minutes": 45,
        "attributed_minutes": 35,
        "linux_minutes": 22,
        "macos_minutes": 23,
        "appended_runs": 2,
        "est_usd": round(22 * 0.006 + 23 * 0.062, 2),
    }


def test_fire_reconciles_the_claimed_slot_window_and_emits(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_fire derives its window from the CLAIMED slot (the fire_slot_once
    binding), not from the wall clock — the layer QA's simulation flagged."""
    module = _load_schedule_module()

    def fake_init_gateway(name: str) -> None:
        del name

    monkeypatch.setattr(module, "init_gateway_process", fake_init_gateway)
    accounting = _FakeAccounting(module)
    sys.modules["ci_accounting"] = accounting  # type: ignore[assignment]
    emitted = _install_fake_accounting(monkeypatch, accounting)
    slot = datetime(2026, 9, 6, 21, 0, tzinfo=UTC)  # 05:00 cluster
    schedule_id = _insert_schedule(db_conn, created_at=datetime(2026, 9, 6, 20, 0, tzinfo=UTC))
    monkeypatch.setenv("AVA_SCHEDULE_ID", str(schedule_id))

    try:
        assert fire_slot_once(slot, None, fire=module._fire)
    finally:
        sys.modules.pop("ci_accounting", None)

    assert accounting.windows == [("2026-09-05T21:00:00Z", "2026-09-06T21:00:00Z")]
    assert len(emitted) == 1
    event = emitted[0]
    assert event["category"] == "telemetry"
    assert event["event_name"] == "ci_usage_daily"
    attributes = cast(dict, event["attributes"])
    assert attributes["day"] == "2026-09-07"
    assert attributes["window_start"] == "2026-09-05T21:00:00Z"
    assert attributes["window_end"] == "2026-09-06T21:00:00Z"


def test_catch_up_boot_with_two_missed_slots_reconciles_each_own_window(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA regression (2026-09-07): a boot that missed two 05:00 slots must
    reconcile each slot's own day — gapless windows, one event per day."""
    module = _load_schedule_module()

    def fake_init_gateway(name: str) -> None:
        del name

    monkeypatch.setattr(module, "init_gateway_process", fake_init_gateway)
    accounting = _FakeAccounting(module)
    sys.modules["ci_accounting"] = accounting  # type: ignore[assignment]
    emitted = _install_fake_accounting(monkeypatch, accounting)
    schedule_id = _insert_schedule(db_conn, created_at=datetime(2026, 9, 6, 0, 30, tzinfo=UTC))
    monkeypatch.setenv("AVA_SCHEDULE_ID", str(schedule_id))

    try:
        fired_slots = catch_up(
            [(module.CRON, None)],
            timezone="UTC",
            fire=module._fire,
            now=datetime(2026, 9, 8, 10, 30, tzinfo=UTC),
        )
    finally:
        sys.modules.pop("ci_accounting", None)

    # Missed 05:00 slots: 09-06, 09-07, 09-08 — catch-up keeps the two most
    # recent, and each must reconcile its OWN day.
    assert fired_slots == [
        datetime(2026, 9, 7, 5, 0, tzinfo=UTC),
        datetime(2026, 9, 8, 5, 0, tzinfo=UTC),
    ]
    assert accounting.windows == [
        ("2026-09-06T05:00:00Z", "2026-09-07T05:00:00Z"),
        ("2026-09-07T05:00:00Z", "2026-09-08T05:00:00Z"),
    ]
    days = [cast(dict, event["attributes"])["day"] for event in emitted]
    assert len(days) == 2
    assert len(set(days)) == 2


def test_fire_reports_failure_without_raising(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_schedule_module()
    accounting = _FakeAccounting(module)

    def broken_collect(repo: str, since: str, until: str) -> list[dict]:
        del repo, since, until
        raise RuntimeError("gh api down")

    accounting.collect = broken_collect  # type: ignore[method-assign]
    sys.modules["ci_accounting"] = accounting  # type: ignore[assignment]
    failures: list[str] = []
    monkeypatch.setattr(module, "_report_failure", failures.append)
    slot = datetime(2026, 9, 6, 21, 0, tzinfo=UTC)
    schedule_id = _insert_schedule(db_conn, created_at=datetime(2026, 9, 6, 20, 0, tzinfo=UTC))
    monkeypatch.setenv("AVA_SCHEDULE_ID", str(schedule_id))

    try:
        assert fire_slot_once(slot, None, fire=module._fire)
    finally:
        sys.modules.pop("ci_accounting", None)

    assert len(failures) == 1
    assert "RuntimeError" in failures[0]
    assert "gh api down" in failures[0]


def test_fire_outside_a_claim_fails_loud() -> None:
    module = _load_schedule_module()
    assert claimed_slot() is None
    with pytest.raises(RuntimeError, match="outside a claimed slot"):
        module._fire(None)
