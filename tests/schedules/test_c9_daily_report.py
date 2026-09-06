"""Contract tests for the C9 daily CI-minute reconciliation schedule."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

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


def _entry(module: ModuleType, *, agent_id: int | None, linux: int, macos: int) -> dict[str, Any]:
    return {
        "run_id": 1 if agent_id is None else agent_id,
        "agent_id": agent_id,
        "linux_minutes": linux,
        "macos_minutes": macos,
    }


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
        _entry(module, agent_id=5811, linux=10, macos=20),
        _entry(module, agent_id=405, linux=5, macos=0),
        _entry(module, agent_id=None, linux=7, macos=3),
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


def test_fire_collects_the_slot_window_appends_and_emits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_schedule_module()
    entries = [_entry(module, agent_id=5811, linux=10, macos=0)]
    accounting = SimpleNamespace(
        collect=lambda _repo, _since, _until: entries,
        append_ledger=lambda _path, _fresh: 1,
        DEFAULT_REPO="zhiyuan-zhang0206/Ava",
        DEFAULT_LEDGER=Path("ledger.jsonl"),
    )
    sys.modules["ci_accounting"] = accounting  # type: ignore[assignment]
    emitted: list[dict] = []

    def fake_emit(category: str, event_name: str, **kwargs: Any) -> None:
        emitted.append({"category": category, "event_name": event_name, **kwargs})

    def fake_previous_fire(_crontab: str, *, before: datetime, timezone: str | None) -> datetime:
        del before, timezone
        return datetime(2026, 9, 6, 21, 0, tzinfo=UTC)

    monkeypatch.setattr(module, "previous_fire", fake_previous_fire)

    def fake_init(name: str) -> None:
        del name

    monkeypatch.setattr(module, "init_gateway_process", fake_init)
    import shared.telemetry

    monkeypatch.setattr(shared.telemetry, "emit", fake_emit)

    try:
        module._fire(None)
    finally:
        sys.modules.pop("ci_accounting", None)

    assert len(emitted) == 1
    event = emitted[0]
    assert event["category"] == "telemetry"
    assert event["event_name"] == "ci_usage_daily"
    attributes = cast(dict, event["attributes"])
    assert attributes["day"] == "2026-09-07"
    assert attributes["runs"] == 1
    assert attributes["attributed_runs"] == 1
    assert attributes["total_minutes"] == 10
    assert attributes["appended_runs"] == 1


def test_fire_reports_failure_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_schedule_module()

    def broken_collect(repo: str, since: str, until: str) -> list[dict]:
        raise RuntimeError("gh api down")

    sys.modules["ci_accounting"] = SimpleNamespace(  # type: ignore[assignment]
        collect=broken_collect,
        append_ledger=lambda _path, _fresh: 0,
        DEFAULT_REPO="r",
        DEFAULT_LEDGER=Path("l.jsonl"),
    )
    failures: list[str] = []

    monkeypatch.setattr(module, "_report_failure", failures.append)

    def fake_previous_fire(_crontab: str, *, before: datetime, timezone: str | None) -> datetime:
        del before, timezone
        return datetime(2026, 9, 6, 21, 0, tzinfo=UTC)

    monkeypatch.setattr(module, "previous_fire", fake_previous_fire)

    try:
        module._fire(None)
    finally:
        sys.modules.pop("ci_accounting", None)

    assert len(failures) == 1
    assert "RuntimeError" in failures[0]
    assert "gh api down" in failures[0]
