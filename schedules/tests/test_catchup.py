"""Per-slot schedule catch-up and idempotency contracts."""

from __future__ import annotations

import ast
import importlib.util
import logging
import multiprocessing
import os
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import ANY, Mock

import psycopg
import pytest

from base.config import settings
from base.db import Database
from schedules.catchup import catch_up, fire_slot_once

_ROOT = Path(__file__).resolve().parents[2]
_DAILY_SCRIPTS = ("c9-daily-report-schedule.py", "dev-ci-metrics-schedule.py")


def _load_daily_script(filename: str) -> ModuleType:
    path = _ROOT / "schedules" / filename
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


class _LoopStoppedError(Exception):
    pass


@pytest.mark.parametrize("filename", _DAILY_SCRIPTS)
def test_daily_loop_skips_an_already_seen_slot_and_sleeps_until_next_fire(
    filename: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_daily_script(filename)
    host = sys.modules[module.run_daily_loop.__module__]
    start = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    now = datetime(2026, 9, 6, 10, 1, tzinfo=UTC)
    next_slot = datetime(2026, 9, 6, 10, 2, tzinfo=UTC)
    clock = iter((start, now))

    class FakeDatetime:
        @staticmethod
        def now(zone: Any) -> datetime:
            assert zone == UTC
            return next(clock)

    catch_up_call = Mock()
    next_fire_call = Mock(side_effect=(start, next_slot))
    claim = Mock()
    sleep = Mock(side_effect=_LoopStoppedError)
    monkeypatch.setattr(host, "datetime", FakeDatetime)
    monkeypatch.setattr(host, "catch_up", catch_up_call)
    monkeypatch.setattr(host, "next_fire", next_fire_call)
    monkeypatch.setattr(host, "fire_slot_once", claim)
    monkeypatch.setattr(host.time, "sleep", sleep)

    with pytest.raises(_LoopStoppedError):
        module._main_loop()

    catch_up_call.assert_called_once_with(
        ANY, [(module.CRON, None)], timezone=settings.general.timezone, fire=module._fire
    )
    assert [call.kwargs["after"] for call in next_fire_call.call_args_list] == [
        now - timedelta(minutes=2),
        start,
    ]
    claim.assert_not_called()
    sleep.assert_called_once_with(60)


@pytest.mark.parametrize("filename", _DAILY_SCRIPTS)
def test_daily_loop_claims_one_due_slot_then_waits_without_retrying(
    filename: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_daily_script(filename)
    host = sys.modules[module.run_daily_loop.__module__]
    start = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    now = datetime(2026, 9, 6, 10, 5, tzinfo=UTC)
    slot = datetime(2026, 9, 6, 10, 4, tzinfo=UTC)
    clock = iter((start, now, now))

    class FakeDatetime:
        @staticmethod
        def now(zone: Any) -> datetime:
            assert zone == UTC
            return next(clock)

    claim = Mock(return_value=True)
    sleep = Mock(side_effect=_LoopStoppedError)
    monkeypatch.setattr(host, "datetime", FakeDatetime)
    monkeypatch.setattr(host, "catch_up", Mock())
    monkeypatch.setattr(host, "next_fire", Mock(return_value=slot))
    monkeypatch.setattr(host, "fire_slot_once", claim)
    monkeypatch.setattr(host.time, "sleep", sleep)

    with pytest.raises(_LoopStoppedError):
        module._main_loop()

    claim.assert_called_once_with(ANY, slot, None, fire=module._fire)
    sleep.assert_called_once_with(120)


def _insert_schedule(conn: psycopg.Connection, *, created_at: datetime) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (name, script, command, enabled, status, created_at) "
            "VALUES (%s, 'x', 'python schedule.py', true, 'stopped', %s) RETURNING id",
            (f"catch-up-{created_at.timestamp()}", created_at),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def _claimed_slots(conn: psycopg.Connection, schedule_id: int) -> list[datetime]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT slot_fire_at FROM schedule_fire_log "
            "WHERE schedule_id = %s ORDER BY slot_fire_at",
            (schedule_id,),
        )
        return [row[0] for row in cur.fetchall()]


def _claim_worker(
    schedule_id: int,
    slot_iso: str,
    ready: Any,
    start: Any,
    outcomes: Any,
) -> None:
    os.environ["AVA_SCHEDULE_ID"] = str(schedule_id)
    ready.put(True)
    if not start.wait(timeout=10):
        outcomes.put("timeout")
        return
    claimed = fire_slot_once(
        Database.from_settings(),
        datetime.fromisoformat(slot_iso),
        "payload",
        fire=lambda _slot, _payload: outcomes.put("fired"),
    )
    outcomes.put("claimed" if claimed else "lost")


def test_concurrent_processes_execute_a_slot_at_most_once(
    db_conn: psycopg.Connection,
) -> None:
    schedule_id = _insert_schedule(db_conn, created_at=datetime(2026, 9, 6, 9, 30, tzinfo=UTC))
    slot = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    outcomes = context.Queue()
    workers = [
        context.Process(
            target=_claim_worker,
            args=(schedule_id, slot.isoformat(), ready, start, outcomes),
        )
        for _ in range(2)
    ]

    for worker in workers:
        worker.start()
    for _ in workers:
        assert ready.get(timeout=15) is True
    start.set()
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0

    observed = sorted(outcomes.get(timeout=5) for _ in range(3))
    assert observed == ["claimed", "fired", "lost"]
    assert _claimed_slots(db_conn, schedule_id) == [slot]


def test_winner_receives_utc_slot_and_payload_after_claim_commits(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    schedule_id = _insert_schedule(db_conn, created_at=datetime(2026, 9, 6, 9, 30, tzinfo=UTC))
    monkeypatch.setenv("AVA_SCHEDULE_ID", str(schedule_id))
    local_slot = datetime(2026, 9, 6, 18, 0, tzinfo=timezone(timedelta(hours=8)))
    slot = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    payload = object()
    observed: list[tuple[datetime, object]] = []

    def fire(claimed: datetime, received: object) -> None:
        assert claimed.tzinfo is UTC
        assert received is payload
        # A separate connection must already see the durable claim.
        assert _claimed_slots(db_conn, schedule_id) == [slot]
        observed.append((claimed, received))

    assert fire_slot_once(database, local_slot, payload, fire=fire)
    assert not fire_slot_once(
        database, slot, payload, fire=lambda _slot, _payload: pytest.fail("duplicate fire")
    )
    assert observed == [(slot, payload)]


def test_nested_dispatch_keeps_each_callbacks_explicit_slot(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    schedule_id = _insert_schedule(db_conn, created_at=datetime(2026, 9, 6, 9, 30, tzinfo=UTC))
    monkeypatch.setenv("AVA_SCHEDULE_ID", str(schedule_id))
    outer_slot = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    inner_slot = datetime(2026, 9, 6, 11, 0, tzinfo=UTC)
    observed: list[tuple[datetime, str]] = []

    def outer_fire(slot: datetime, payload: str) -> None:
        observed.append((slot, payload))
        assert fire_slot_once(
            database,
            inner_slot,
            "inner",
            fire=lambda slot, payload: observed.append((slot, payload)),
        )
        observed.append((slot, payload))

    assert fire_slot_once(database, outer_slot, "outer", fire=outer_fire)
    assert observed == [(outer_slot, "outer"), (inner_slot, "inner"), (outer_slot, "outer")]
    assert _claimed_slots(db_conn, schedule_id) == [outer_slot, inner_slot]


def test_catch_up_fires_only_the_two_most_recent_slots(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    database: Database,
) -> None:
    schedule_id = _insert_schedule(db_conn, created_at=datetime(2026, 9, 6, 0, 30, tzinfo=UTC))
    monkeypatch.setenv("AVA_SCHEDULE_ID", str(schedule_id))
    fired: list[tuple[datetime, str]] = []

    with caplog.at_level(logging.WARNING, logger="schedules.catchup"):
        slots = catch_up(
            database,
            [("0 * * * *", "hourly")],
            timezone="UTC",
            fire=lambda slot, payload: fired.append((slot, payload)),
            now=datetime(2026, 9, 6, 4, 30, tzinfo=UTC),
        )

    assert slots == [
        datetime(2026, 9, 6, 3, 0, tzinfo=UTC),
        datetime(2026, 9, 6, 4, 0, tzinfo=UTC),
    ]
    assert fired == [(slot, "hourly") for slot in slots]
    assert _claimed_slots(db_conn, schedule_id) == slots
    assert "older missed slots remain unclaimed" in caplog.text


def test_online_schedule_with_latest_slot_claimed_has_no_catch_up(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    schedule_id = _insert_schedule(db_conn, created_at=datetime(2026, 9, 5, 0, 0, tzinfo=UTC))
    monkeypatch.setenv("AVA_SCHEDULE_ID", str(schedule_id))
    last_slot = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    assert fire_slot_once(database, last_slot, "normal", fire=lambda _slot, _payload: None)
    fired: list[tuple[datetime, str]] = []

    slots = catch_up(
        database,
        [("0 * * * *", "catch-up")],
        timezone="UTC",
        fire=lambda slot, payload: fired.append((slot, payload)),
        now=datetime(2026, 9, 6, 10, 30, tzinfo=UTC),
    )

    assert slots == []
    assert fired == []
    assert _claimed_slots(db_conn, schedule_id) == [last_slot]


def test_restart_after_missed_slot_fires_exactly_once(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    schedule_id = _insert_schedule(db_conn, created_at=datetime(2026, 9, 6, 9, 30, tzinfo=UTC))
    monkeypatch.setenv("AVA_SCHEDULE_ID", str(schedule_id))
    fired: list[tuple[datetime, str]] = []
    now = datetime(2026, 9, 6, 10, 30, tzinfo=UTC)

    first = catch_up(
        database,
        [("0 * * * *", "missed")],
        timezone="UTC",
        fire=lambda slot, payload: fired.append((slot, payload)),
        now=now,
    )
    second = catch_up(
        database,
        [("0 * * * *", "missed")],
        timezone="UTC",
        fire=lambda slot, payload: fired.append((slot, payload)),
        now=datetime(2026, 9, 6, 10, 45, tzinfo=UTC),
    )

    slot = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    assert first == [slot]
    assert second == []
    assert fired == [(slot, "missed")]
    assert _claimed_slots(db_conn, schedule_id) == [slot]


def test_claim_survives_fire_failure(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    schedule_id = _insert_schedule(db_conn, created_at=datetime(2026, 9, 6, 9, 30, tzinfo=UTC))
    monkeypatch.setenv("AVA_SCHEDULE_ID", str(schedule_id))
    slot = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)

    with pytest.raises(RuntimeError, match="after claim"):
        fire_slot_once(
            database,
            slot,
            None,
            fire=lambda _slot, _payload: (_ for _ in ()).throw(RuntimeError("after claim")),
        )

    assert not fire_slot_once(
        database, slot, None, fire=lambda _slot, _payload: pytest.fail("duplicate fire")
    )
    assert _claimed_slots(db_conn, schedule_id) == [slot]


@pytest.mark.parametrize(
    "script_path",
    sorted((_ROOT / "schedules").glob("*-schedule.py")),
    ids=lambda path: path.name,
)
def test_builtin_schedule_templates_use_catch_up_and_slot_claims(script_path: Path) -> None:
    tree = ast.parse(script_path.read_text(encoding="utf-8"), filename=str(script_path))
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    if script_path.name in _DAILY_SCRIPTS:
        assert "run_daily_loop" in calls
    else:
        assert "catch_up" in calls
        assert "fire_slot_once" in calls


def test_daily_host_owns_catch_up_and_slot_claims() -> None:
    helper = _ROOT / "schedules" / "daily_host.py"
    tree = ast.parse(helper.read_text(encoding="utf-8"), filename=str(helper))
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert {"catch_up", "fire_slot_once"} <= calls


def test_cluster_timezone_follows_the_setting_when_it_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from schedules.catchup import cluster_timezone

    monkeypatch.setattr(settings.general, "timezone", "Asia/Kathmandu")
    assert cluster_timezone() == "Asia/Kathmandu"
    monkeypatch.setattr(settings.general, "timezone", "America/Los_Angeles")
    assert cluster_timezone() == "America/Los_Angeles"
