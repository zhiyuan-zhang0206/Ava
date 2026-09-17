"""Recoverable observations: replay identity, bounded progress, and failed scans."""

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from services.events_maintenance import observed_metrics as replay
from shared.observed_metrics import observe_row, write_observations


def _source(db: psycopg.Connection, event_id: int = 1) -> dict[str, Any]:
    row = db.execute("INSERT INTO agents(label) VALUES ('replay') RETURNING id").fetchone()
    assert row is not None
    db.commit()
    return {
        "id": event_id,
        "agent_id": row[0],
        "ts": datetime.now(UTC).isoformat(),
        "event_name": "llm_usage",
        "category": "telemetry",
        "attributes": {"in_total": 5, "out_total": 2, "cost_usd": 0.25},
    }


def _calls(db: psycopg.Connection) -> int:
    row = db.execute("SELECT COALESCE(sum(usage_calls),0) FROM agent_metric_days").fetchone()
    assert row is not None
    db.commit()
    return int(row[0])


def test_jsonl_replay_commutes_with_live_projection_and_resumes_append(
    db_conn: psycopg.Connection,
    tmp_path: Path,
) -> None:
    row = _source(db_conn)
    observation = observe_row(row)
    assert observation is not None
    write_observations([observation], db=db_conn)
    db_conn.commit()
    path = tmp_path / "events-full.jsonl"
    path.write_text(json.dumps(row) + "\n")
    assert replay.replay_jsonl(db_conn, path, deadline=time.monotonic() + 5) == 0
    assert _calls(db_conn) == 1
    with path.open("a") as source:
        source.write(json.dumps({**row, "id": 2}) + "\n")
    assert replay.replay_jsonl(db_conn, path, deadline=time.monotonic() + 5) == 1
    assert _calls(db_conn) == 2
    assert replay.replay_jsonl(db_conn, path, deadline=time.monotonic() + 5) == 0


def test_partial_final_line_is_not_acknowledged(
    db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    row = _source(db_conn)
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(row))
    assert replay.replay_jsonl(db_conn, path, deadline=time.monotonic() + 5) == 0
    assert _calls(db_conn) == 0
    with path.open("a") as source:
        source.write("\n")
    assert replay.replay_jsonl(db_conn, path, deadline=time.monotonic() + 5) == 1


def test_failed_projection_does_not_advance_file_cursor(
    db_conn: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _source(db_conn)
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(row) + "\n")

    def unavailable(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("projection failed")

    with monkeypatch.context() as patch:
        patch.setattr(replay, "write_observations", unavailable)
        with pytest.raises(RuntimeError, match="projection failed"):
            replay.replay_jsonl(db_conn, path, deadline=time.monotonic() + 5)
    cursor = db_conn.execute(
        "SELECT count(*) FROM agent_metric_file_cursors WHERE source_key LIKE %s",
        ("%:" + str(path.resolve()),),
    ).fetchone()
    assert cursor == (0,)
    db_conn.commit()
    assert replay.replay_jsonl(db_conn, path, deadline=time.monotonic() + 5) == 1


def test_full_loki_page_bisects_without_silently_losing_rows(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _source(db_conn)
    del row["id"]
    data = [(10, json.dumps(row)), (15, json.dumps(row)), (20, json.dumps(row))]

    def fetch(_query: str, lo: int, hi: int, _deadline: float) -> list[tuple[int, str]]:
        return [(ts, line) for ts, line in data if lo <= ts < hi][:2]

    monkeypatch.setattr(replay, "_PAGE_LIMIT", 2)
    monkeypatch.setattr(replay, "_fetch", fetch)
    assert replay._recover_range(db_conn, "query", 10, 21, time.monotonic() + 5) == 3
    assert _calls(db_conn) == 3


def test_loki_timestamp_overflow_is_explicit_failure(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(replay, "_PAGE_LIMIT", 2)

    def full_page(*_args: Any) -> list[tuple[int, str]]:
        return [(10, "{}"), (10, "{}")]

    monkeypatch.setattr(replay, "_fetch", full_page)
    with pytest.raises(RuntimeError, match="scan incomplete"):
        replay._recover_range(db_conn, "query", 10, 11, time.monotonic() + 5)


def test_deadline_stops_replay_without_restarting_completed_batches(
    db_conn: psycopg.Connection,
    tmp_path: Path,
) -> None:
    row = _source(db_conn)
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(row) + "\n")
    with pytest.raises(TimeoutError):
        replay.replay_jsonl(db_conn, path, deadline=time.monotonic() - 1)
    assert replay.replay_jsonl(db_conn, path, deadline=time.monotonic() + 5) == 1


def test_archive_replay_preserves_unlabeled_cluster_and_last_row(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    from shared.loki_index_labels import archive_stream_selector

    freeze = replay.ARCHIVE_FREEZE_AT
    monkeypatch.setattr(replay, "ARCHIVE_FLOOR_AT", freeze - timedelta(minutes=1))
    queries: list[tuple[str, int, int]] = []

    def recover(_conn: Any, query: str, lo: int, hi: int, _deadline: float) -> int:
        queries.append((query, lo, hi))
        return 0

    monkeypatch.setattr(replay, "_recover_range", recover)
    replay.replay_loki(db_conn, now=datetime.now(UTC), deadline=time.monotonic() + 5, archive=True)
    assert queries
    assert queries[-1][2] > int(freeze.timestamp() * 1e9)
    assert all(query.startswith(archive_stream_selector()) for query, _, _ in queries)
    assert all('or metric_cluster=""' in query for query, _, _ in queries)
    queries.clear()
    replay.replay_loki(db_conn, now=freeze + timedelta(minutes=1), deadline=time.monotonic() + 5)
    assert queries[0][1] > int(freeze.timestamp() * 1e9)


def test_loki_failure_does_not_block_jsonl_repair(
    db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _source(db_conn)
    (tmp_path / "events-20260917.jsonl").write_text(json.dumps(row) + "\n")
    monkeypatch.setattr(replay, "logs_dir", lambda: tmp_path)

    def unavailable(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("Loki offline")

    monkeypatch.setattr(replay, "replay_loki", unavailable)
    with pytest.raises(ExceptionGroup, match="incomplete"):
        replay.recover_observations(db_conn)
    assert _calls(db_conn) == 1


def test_pre_id_live_jsonl_derives_shared_loki_identity(
    db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    from shared.telemetry import event_id

    row = _source(db_conn)
    del row["id"]
    line = json.dumps(row, separators=(",", ":"), ensure_ascii=False)
    ts_ns = int(datetime.fromisoformat(row["ts"]).timestamp() * 1e9)
    path = tmp_path / "old-live.jsonl"
    path.write_text(line + "\n")
    assert replay.replay_jsonl(db_conn, path, deadline=time.monotonic() + 5) == 1
    assert replay._persist_lines(db_conn, [(ts_ns, line)]) == 0
    persisted = db_conn.execute(
        "SELECT event_id FROM agent_metric_observations WHERE agent_id=%s", (row["agent_id"],)
    ).fetchone()
    assert persisted == (event_id(line, ts_ns),)


def test_archive_owned_jsonl_rows_are_recorded_without_stranding_live_rows(
    db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    row = _source(db_conn)
    old = {
        **row,
        "ts": replay.ARCHIVE_FREEZE_AT.isoformat(),
        "id": 999,
        "attributes": {"cost_usd": "legacy-invalid"},
    }
    path = tmp_path / "mixed-era.jsonl"
    path.write_text(json.dumps(old) + "\n" + json.dumps(row) + "\n")
    assert replay.replay_jsonl(db_conn, path, deadline=time.monotonic() + 5) == 1
    assert _calls(db_conn) == 1
    cursor = db_conn.execute(
        "SELECT position,excluded_archive_rows FROM agent_metric_file_cursors WHERE source_key LIKE %s",
        ("%:" + str(path.resolve()),),
    ).fetchone()
    assert cursor == (path.stat().st_size, 1)


def test_repair_reserves_time_for_independent_jsonl_source(
    db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "events-20260917.jsonl").touch()
    deadlines: dict[str, float] = {}
    monkeypatch.setattr(replay, "logs_dir", lambda: tmp_path)

    def loki(_conn: Any, *, now: datetime, deadline: float) -> int:
        deadlines["loki"] = deadline
        return 0

    def jsonl(_conn: Any, _path: Path, *, deadline: float) -> int:
        deadlines["jsonl"] = deadline
        return 0

    monkeypatch.setattr(replay, "replay_loki", loki)
    monkeypatch.setattr(replay, "replay_jsonl", jsonl)
    replay.recover_observations(db_conn)
    assert deadlines["jsonl"] - deadlines["loki"] == replay._PASS_SECONDS / 2
