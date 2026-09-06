"""Per-slot schedule catch-up and idempotency contracts."""

from __future__ import annotations

import ast
import logging
import multiprocessing
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from schedules.catchup import catch_up, claimed_slot, fire_slot_once

_ROOT = Path(__file__).resolve().parents[2]


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
        datetime.fromisoformat(slot_iso),
        "payload",
        fire=lambda _payload: outcomes.put("fired"),
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


def test_claimed_slot_exposes_the_slot_around_the_fire_callback(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The winner's callback sees its slot via claimed_slot(); losers never
    run, and the slot is restored afterwards — the binding window-based
    schedules (C9 daily report) rely on."""
    schedule_id = _insert_schedule(db_conn, created_at=datetime(2026, 9, 6, 9, 30, tzinfo=UTC))
    monkeypatch.setenv("AVA_SCHEDULE_ID", str(schedule_id))
    slot = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    observed: list[datetime | None] = []

    def fire(_payload: str) -> None:
        observed.append(claimed_slot())

    assert claimed_slot() is None
    assert fire_slot_once(slot, "payload", fire=fire)
    assert not fire_slot_once(slot, "payload", fire=lambda _payload: None)
    assert observed == [slot]
    assert claimed_slot() is None
    assert _claimed_slots(db_conn, schedule_id) == [slot]


def test_catch_up_fires_only_the_two_most_recent_slots(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    schedule_id = _insert_schedule(db_conn, created_at=datetime(2026, 9, 6, 0, 30, tzinfo=UTC))
    monkeypatch.setenv("AVA_SCHEDULE_ID", str(schedule_id))
    fired: list[str] = []

    with caplog.at_level(logging.WARNING, logger="schedules.catchup"):
        slots = catch_up(
            [("0 * * * *", "hourly")],
            timezone="UTC",
            fire=fired.append,
            now=datetime(2026, 9, 6, 4, 30, tzinfo=UTC),
        )

    assert slots == [
        datetime(2026, 9, 6, 3, 0, tzinfo=UTC),
        datetime(2026, 9, 6, 4, 0, tzinfo=UTC),
    ]
    assert fired == ["hourly", "hourly"]
    assert _claimed_slots(db_conn, schedule_id) == slots
    assert "older missed slots remain unclaimed" in caplog.text


def test_online_schedule_with_latest_slot_claimed_has_no_catch_up(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schedule_id = _insert_schedule(db_conn, created_at=datetime(2026, 9, 5, 0, 0, tzinfo=UTC))
    monkeypatch.setenv("AVA_SCHEDULE_ID", str(schedule_id))
    last_slot = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    assert fire_slot_once(last_slot, "normal", fire=lambda _payload: None)
    fired: list[str] = []

    slots = catch_up(
        [("0 * * * *", "catch-up")],
        timezone="UTC",
        fire=fired.append,
        now=datetime(2026, 9, 6, 10, 30, tzinfo=UTC),
    )

    assert slots == []
    assert fired == []
    assert _claimed_slots(db_conn, schedule_id) == [last_slot]


def test_restart_after_missed_slot_fires_exactly_once(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schedule_id = _insert_schedule(db_conn, created_at=datetime(2026, 9, 6, 9, 30, tzinfo=UTC))
    monkeypatch.setenv("AVA_SCHEDULE_ID", str(schedule_id))
    fired: list[str] = []
    now = datetime(2026, 9, 6, 10, 30, tzinfo=UTC)

    first = catch_up([("0 * * * *", "missed")], timezone="UTC", fire=fired.append, now=now)
    second = catch_up(
        [("0 * * * *", "missed")],
        timezone="UTC",
        fire=fired.append,
        now=datetime(2026, 9, 6, 10, 45, tzinfo=UTC),
    )

    slot = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    assert first == [slot]
    assert second == []
    assert fired == ["missed"]
    assert _claimed_slots(db_conn, schedule_id) == [slot]


def test_claim_survives_fire_failure(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schedule_id = _insert_schedule(db_conn, created_at=datetime(2026, 9, 6, 9, 30, tzinfo=UTC))
    monkeypatch.setenv("AVA_SCHEDULE_ID", str(schedule_id))
    slot = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)

    with pytest.raises(RuntimeError, match="after claim"):
        fire_slot_once(
            slot, None, fire=lambda _payload: (_ for _ in ()).throw(RuntimeError("after claim"))
        )

    assert not fire_slot_once(slot, None, fire=lambda _payload: pytest.fail("duplicate fire"))
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

    assert "catch_up" in calls
    assert "fire_slot_once" in calls
