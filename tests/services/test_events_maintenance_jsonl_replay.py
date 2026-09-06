"""Rollup-source JSONL aggregation and pre-Loki-floor ledger gap replay."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest

from services.events_maintenance import jsonl_replay
from services.events_maintenance.jsonl_replay import (
    ReplayResult,
    aggregate_rollup_file,
    replay_gap_days,
)

_NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(db_conn: psycopg.Connection) -> psycopg.Connection:
    db_conn.autocommit = True
    return db_conn


def _agent(db: psycopg.Connection, label: str = "replay-agent") -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES (%s) RETURNING id", (label,))
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _event(
    event_name: str,
    agent_id: int | str | None,
    attributes: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "ts": "2026-06-01T12:00:00+00:00",
        "agent_id": agent_id,
        "event_name": event_name,
        "attributes": attributes or {},
    }


def _write_rollup_file(root: Path, day: date, events: list[dict[str, object]]) -> Path:
    path = root / f"events-{day:%Y%m%d}.rollup.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return path


def _seed_watermark(db: psycopg.Connection, agent_id: int, day: date) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_metrics_daily "
            "(agent_id, day, turn_total, turn_ok, turn_dur_sum, exec_ok, exec_failed) "
            "VALUES (%s, %s, 1, 1, 1.0, 0, 0)",
            (agent_id, day),
        )


def _token_days(db: psycopg.Connection) -> list[date]:
    with db.cursor() as cur:
        cur.execute("SELECT day FROM agent_model_tokens_daily ORDER BY day")
        return [row[0] for row in cur.fetchall()]


def test_aggregate_rollup_file_matches_loki_rollup_semantics(tmp_path: Path) -> None:
    path = _write_rollup_file(
        tmp_path,
        date(2026, 6, 1),
        [
            _event(
                "llm_usage",
                11,
                {
                    "model": "m1",
                    "in_total": 10,
                    "out_total": 5,
                    "cache_read": 2,
                    "reasoning": 1,
                    "cost_usd": 0.25,
                },
            ),
            _event("llm_usage", "11", {"model": "m1", "in_total": 2}),
            _event("llm_usage", 99, {"model": "", "cost_usd": ""}),
            _event("turn_end", 11, {"ok": True, "duration_seconds": 2.5}),
            _event("turn_end", 11, {"ok": False, "duration_seconds": 4.0}),
            _event("exec", 11),
            _event("exec_failed", 11),
            _event("exec(timeout)", 11),
            _event("fork", 11),
            _event("llm_usage", None, {"model": "ignored", "in_total": 999}),
            _event("turn_end", "", {"ok": True}),
        ],
    )

    tokens_rows, metrics_rows, source_agent_ids = aggregate_rollup_file(path)

    assert tokens_rows == [
        jsonl_replay.TokensRow(11, "m1", 2, 1, 1, 12, 5, 2, 1, 0.25),
        jsonl_replay.TokensRow(99, "", 1, 0, 1, 0, 0, 0, 0, 0.0),
    ]
    assert metrics_rows == [jsonl_replay.MetricsRow(11, 2, 1, 6.5, 2.5, 4.0, {2: 1, 4: 1}, 1, 2)]
    assert source_agent_ids == {11, 99}


def test_replay_uses_pre_floor_watermark_and_only_days_strictly_below_floor(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent_id = _agent(db)
    _seed_watermark(db, agent_id, date(2026, 5, 31))
    _seed_watermark(db, agent_id, date(2026, 6, 9))
    monkeypatch.setattr(jsonl_replay, "logs_dir", lambda: tmp_path)
    for day in (date(2026, 6, 1), date(2026, 6, 3), date(2026, 6, 7), date(2026, 6, 8)):
        _write_rollup_file(tmp_path, day, [_event("llm_usage", agent_id, {"model": "m1"})])

    result = replay_gap_days(db, now_utc=_NOW)

    assert result.days_replayed == [date(2026, 6, 1), date(2026, 6, 3), date(2026, 6, 7)]
    assert result.days_failed == []
    assert _token_days(db) == [date(2026, 6, 1), date(2026, 6, 3), date(2026, 6, 7)]


def test_replay_writes_rows_and_second_run_is_a_noop(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent_id = _agent(db)
    _seed_watermark(db, agent_id, date(2026, 5, 31))
    monkeypatch.setattr(jsonl_replay, "logs_dir", lambda: tmp_path)
    _write_rollup_file(
        tmp_path,
        date(2026, 6, 1),
        [
            _event("llm_usage", agent_id, {"model": "m1", "in_total": 7, "cost_usd": 0.5}),
            _event("turn_end", agent_id, {"ok": True, "duration_seconds": 3}),
            _event("llm_usage", 999_999, {"model": "unknown", "in_total": 100}),
        ],
    )

    first = replay_gap_days(db, now_utc=_NOW)
    second = replay_gap_days(db, now_utc=_NOW)

    assert first == ReplayResult([date(2026, 6, 1)], [], 1, 1)
    assert second == ReplayResult([], [], 0, 0)
    with db.cursor() as cur:
        cur.execute(
            "SELECT llm_calls, tokens_in, cost_usd, costed_calls, unpriced_calls "
            "FROM agent_model_tokens_daily WHERE agent_id = %s AND day = '2026-06-01'",
            (agent_id,),
        )
        assert cur.fetchone() == (1, 7, 0.5, 1, 0)
        cur.execute(
            "SELECT turn_total, turn_ok, turn_dur_sum, turn_dur_min, turn_dur_max "
            "FROM agent_metrics_daily WHERE agent_id = %s AND day = '2026-06-01'",
            (agent_id,),
        )
        assert cur.fetchone() == (1, 1, 3.0, 3.0, 3.0)


def test_zero_row_day_fails_without_writing(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent_id = _agent(db)
    _seed_watermark(db, agent_id, date(2026, 5, 31))
    monkeypatch.setattr(jsonl_replay, "logs_dir", lambda: tmp_path)
    _write_rollup_file(tmp_path, date(2026, 6, 1), [_event("fork", agent_id)])
    _write_rollup_file(
        tmp_path,
        date(2026, 6, 2),
        [_event("llm_usage", agent_id, {"model": "m1"})],
    )

    result = replay_gap_days(db, now_utc=_NOW)

    assert result == ReplayResult([], [date(2026, 6, 1)], 0, 0)
    assert _token_days(db) == []

    _write_rollup_file(
        tmp_path,
        date(2026, 6, 1),
        [_event("llm_usage", agent_id, {"model": "m1"})],
    )
    retry = replay_gap_days(db, now_utc=_NOW)

    assert retry == ReplayResult([date(2026, 6, 1), date(2026, 6, 2)], [], 2, 0)
    assert _token_days(db) == [date(2026, 6, 1), date(2026, 6, 2)]


def test_all_unknown_agent_rows_fail_without_writing(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    known_agent_id = _agent(db)
    _seed_watermark(db, known_agent_id, date(2026, 5, 31))
    monkeypatch.setattr(jsonl_replay, "logs_dir", lambda: tmp_path)
    _write_rollup_file(
        tmp_path,
        date(2026, 6, 1),
        [_event("llm_usage", 999_999, {"model": "unknown", "in_total": 10})],
    )

    result = replay_gap_days(db, now_utc=_NOW)

    assert result == ReplayResult([], [date(2026, 6, 1)], 0, 0)
    assert _token_days(db) == []


def test_dry_run_reports_rows_without_writing(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent_id = _agent(db)
    _seed_watermark(db, agent_id, date(2026, 5, 31))
    monkeypatch.setattr(jsonl_replay, "logs_dir", lambda: tmp_path)
    _write_rollup_file(
        tmp_path,
        date(2026, 6, 1),
        [_event("llm_usage", agent_id, {"model": "m1", "in_total": 5})],
    )

    result = replay_gap_days(db, now_utc=_NOW, dry_run=True)

    assert result == ReplayResult([date(2026, 6, 1)], [], 1, 0)
    assert _token_days(db) == []


def test_cli_filters_days_and_exits_nonzero_for_failed_day(
    db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent_id = _agent(db)
    _seed_watermark(db, agent_id, date(2026, 5, 31))
    monkeypatch.setattr(jsonl_replay, "logs_dir", lambda: tmp_path)
    _write_rollup_file(
        tmp_path,
        date(2026, 6, 1),
        [_event("llm_usage", agent_id, {"model": "m1"})],
    )
    _write_rollup_file(
        tmp_path,
        date(2026, 6, 2),
        [_event("llm_usage", 999_999, {"model": "unknown"})],
    )

    class _ConnectionContext:
        def __enter__(self) -> psycopg.Connection:
            return db

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(jsonl_replay.shared.db, "connect", _ConnectionContext)

    assert jsonl_replay.main(["--dry-run", "--days", "20260601"]) == 0
    success_output = capsys.readouterr().out
    assert "replayed=2026-06-01" in success_output
    assert "2026-06-02" not in success_output
    assert jsonl_replay.main(["--dry-run", "--days", "20260602"]) == 1
    assert "failed=2026-06-02" in capsys.readouterr().out
