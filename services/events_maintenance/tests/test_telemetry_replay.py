"""Replaying the local JSONL mirror into telemetry_events."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.telemetry.emitter import event_id
from services.events_maintenance import telemetry_replay as replay

_TS = "2026-10-02T12:00:00+00:00"


def _line(name: str, category: str, marker: str, *, with_id: bool = True) -> bytes:
    body: dict[str, Any] = {
        "ts": _TS,
        "trace_id": None,
        "span_id": None,
        "agent_id": 7,
        "machine": marker,
        "cluster": "c",
        "process": "p",
        "category": category,
        "event_name": name,
        "level": "info",
        "source": "system",
        "target_agent_id": None,
        "attributes": {"n": name},
    }
    text = json.dumps(body, separators=(",", ":"))
    if with_id:
        body["id"] = event_id(text, int(1_790_000_000 * 1e9))
    return (json.dumps(body, separators=(",", ":")) + "\n").encode()


def _stored(db: psycopg.Connection, marker: str) -> list[str]:
    return [
        row[0]
        for row in db.execute(
            "SELECT event_name FROM telemetry_events WHERE machine = %s ORDER BY event_name",
            (marker,),
        ).fetchall()
    ]


def test_a_pass_stores_telemetry_and_log_rows_and_resumes_after_a_partial_line(
    db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = f"replay-{time.monotonic_ns()}"
    mirror = tmp_path / "events-20261002.jsonl"
    tail = _line("turn_end", "telemetry", marker)
    mirror.write_bytes(
        _line("llm_usage", "telemetry", marker)
        + _line("log", "log", marker)
        + _line("spawn", "audit", marker)
        + tail[:20]
    )
    monkeypatch.setattr(replay, "logs_dir", lambda: tmp_path)

    assert replay.recover_telemetry_events(db_conn) == 2
    assert _stored(db_conn, marker) == ["llm_usage", "log"]
    assert replay.recover_telemetry_events(db_conn) == 0

    with mirror.open("ab") as handle:
        handle.write(tail[20:])
    assert replay.recover_telemetry_events(db_conn) == 1
    assert _stored(db_conn, marker) == ["llm_usage", "log", "turn_end"]


def test_a_row_the_live_writer_already_stored_is_not_stored_twice(
    db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = f"replay-{time.monotonic_ns()}"
    (tmp_path / "events-20261002.jsonl").write_bytes(_line("llm_usage", "telemetry", marker))
    monkeypatch.setattr(replay, "logs_dir", lambda: tmp_path)
    row = json.loads((tmp_path / "events-20261002.jsonl").read_bytes())
    db_conn.execute(
        "INSERT INTO telemetry_events (event_uid, ts, machine, cluster, process, category, "
        "event_name, level, source) VALUES (%s, %s, %s, 'c', 'p', 'telemetry', 'llm_usage', "
        "'info', 'system')",
        (row["id"] - (1 << 64) if row["id"] >= 1 << 63 else row["id"], _TS, marker),
    )
    db_conn.commit()

    assert replay.recover_telemetry_events(db_conn) == 0
    assert _stored(db_conn, marker) == ["llm_usage"]


def test_rows_written_before_the_mirror_held_an_id_get_the_derived_one(
    db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = f"replay-{time.monotonic_ns()}"
    (tmp_path / "events-20261002.jsonl").write_bytes(
        _line("llm_usage", "telemetry", marker, with_id=False)
    )
    monkeypatch.setattr(replay, "logs_dir", lambda: tmp_path)

    assert replay.recover_telemetry_events(db_conn) == 1
    assert _stored(db_conn, marker) == ["llm_usage"]


def test_the_rollup_and_other_derived_files_are_not_replayed(
    db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = f"replay-{time.monotonic_ns()}"
    (tmp_path / "events-20261002.rollup.jsonl").write_bytes(_line("llm_usage", "telemetry", marker))
    monkeypatch.setattr(replay, "logs_dir", lambda: tmp_path)

    assert replay.recover_telemetry_events(db_conn) == 0
