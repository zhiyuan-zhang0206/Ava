"""The telemetry_events backfill: normalization, source precedence, dry-run and idempotent apply."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from scripts.data_repair import backfill_telemetry_events as backfill

_UNTIL = datetime(2030, 1, 1, tzinfo=UTC)


def _raw(name: str = "llm_usage", *, stream_id: int, machine: str, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ts": "2026-09-30T12:00:00+00:00",
        "trace_id": None,
        "span_id": None,
        "agent_id": 7,
        "machine": machine,
        "cluster": "c",
        "process": "p",
        "category": "telemetry",
        "event_name": name,
        "level": "info",
        "source": "system",
        "target_agent_id": None,
        "attributes": {"k": 1},
        "id": stream_id,
    }
    row.update(over)
    return row


def _mirror(directory: Path, rows: list[dict[str, Any]], day: str = "20260930") -> None:
    (directory / f"events-{day}.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows) + "not json\n", encoding="utf-8"
    )


def _stored(db: psycopg.Connection, machine: str) -> list[tuple[Any, ...]]:
    return db.execute(
        "SELECT event_name, cluster, imported_from, tableoid::regclass::text FROM telemetry_events "
        "WHERE machine = %s ORDER BY event_name",
        (machine,),
    ).fetchall()


def test_normalize_keeps_telemetry_and_log_rows_and_names_why_it_skips_the_rest() -> None:
    row = backfill.normalize(
        _raw(stream_id=(1 << 64) - 1, machine="m"), source="jsonl", cluster="x"
    )
    assert (row["event_uid"], row["cluster"], row["imported_from"]) == (-1, "c", "jsonl")

    from_loki = _raw(stream_id=5, machine="m")
    del from_loki["cluster"]
    assert backfill.normalize(from_loki, source="loki-live", cluster="x")["cluster"] == "x"

    for over, reason in (
        ({"category": "audit"}, "not-telemetry-or-log"),
        ({"event_name": ""}, "no-event-name"),
        ({"level": "loud"}, "bad-level"),
        ({"id": None}, "no-id"),
        ({"ts": "yesterday"}, "bad-ts"),
    ):
        raw = {**_raw(stream_id=1, machine="m"), **over}
        with pytest.raises(backfill.SkippedRowError, match=reason):
            backfill.normalize(raw, source="jsonl", cluster="x")


def test_a_row_two_sources_hold_is_kept_once_from_the_first(tmp_path: Path) -> None:
    mirror_dir = tmp_path / "logs"
    mirror_dir.mkdir()
    _mirror(
        mirror_dir,
        [_raw(stream_id=11, machine="m"), _raw("log", stream_id=12, machine="m", category="log")],
    )
    loki_row = _raw(stream_id=11, machine="m")
    del loki_row["cluster"]

    streams = [
        ("jsonl", backfill.read_jsonl([mirror_dir], _UNTIL, "x")),
        (
            "loki-live",
            iter([backfill.normalize(loki_row, source="loki-live", cluster="x")]),
        ),
    ]
    found = backfill.plan(streams)

    assert len(found.keys) == 2
    assert found.read == {"jsonl": 2, "loki-live": 1}
    assert found.duplicates == {"loki-live": 1}
    assert found.skipped == {("jsonl", "unparsable-line"): 1}


def test_apply_inserts_once_into_the_right_months_and_a_rerun_adds_nothing(
    db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    machine = f"backfill-{tmp_path.name}"
    mirror_dir = tmp_path / "logs"
    mirror_dir.mkdir()
    old = "2026-06-15T09:00:00+00:00"
    _mirror(
        mirror_dir,
        [
            _raw(stream_id=21, machine=machine),
            _raw("old_event", stream_id=22, machine=machine, ts=old),
            _raw("audit_row", stream_id=23, machine=machine, category="audit"),
        ],
    )

    def streams() -> list[backfill.Stream]:
        return [("jsonl", backfill.read_jsonl([mirror_dir], _UNTIL, "x"))]

    first = backfill.plan(streams())
    assert backfill.apply(streams(), first.first_ts) == (2, 2)
    assert backfill.apply(streams(), first.first_ts) == (0, 2)

    stored = _stored(db_conn, machine)
    assert [(row[0], row[2]) for row in stored] == [("llm_usage", "jsonl"), ("old_event", "jsonl")]
    # Each row is in its own month's partition, never the default one.
    assert [row[3] for row in stored] == ["telemetry_events_202609", "telemetry_events_202606"]


def test_a_dry_run_reports_what_is_already_in_the_table(
    db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    machine = f"dryrun-{tmp_path.name}"
    mirror_dir = tmp_path / "logs"
    mirror_dir.mkdir()
    _mirror(mirror_dir, [_raw(stream_id=31, machine=machine), _raw(stream_id=32, machine=machine)])
    found = backfill.plan([("jsonl", backfill.read_jsonl([mirror_dir], _UNTIL, "x"))])

    assert backfill._already_present(db_conn, found.keys) == 0
    backfill.apply([("jsonl", backfill.read_jsonl([mirror_dir], _UNTIL, "x"))], found.first_ts)
    assert backfill._already_present(db_conn, found.keys) == 2
    assert "would insert: 0" in backfill.render(found, 2)
