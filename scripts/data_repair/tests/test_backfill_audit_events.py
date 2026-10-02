"""Contract tests for the `audit_events` backfill.

The sources are replaced by canned rows; Postgres is real. What matters: the stream id becomes
the signed `event_uid`, every constraint-violating row is skipped with a reason instead of
failing the transaction, rows are kept once across sources and never overwrite a live row, the
apply is one transaction, and a dry-run writes nothing.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from scripts.data_repair import backfill_audit_events as backfill
from scripts.data_repair.backfill_audit_events import AuditRow, Skipped

_TS = datetime(2026, 7, 1, 12, tzinfo=UTC)


def _raw(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": uuid.uuid4().int % (1 << 62),
        "ts": _TS,
        "trace_id": None,
        "span_id": None,
        "agent_id": 7,
        "machine": "m",
        "process": "p",
        "category": "audit",
        "event_name": "spawn",
        "level": "info",
        "source": "user",
        "target_agent_id": 9,
        "attributes": {"machine": "m"},
    }
    base.update(over)
    return base


def _stream(name: str, *rows: AuditRow | Skipped) -> tuple[str, Any]:
    return name, iter(rows)


def _row(imported_from: str = "loki-live", **over: Any) -> AuditRow:
    return backfill.normalize(_raw(**over), imported_from=imported_from)


def _count(db: psycopg.Connection, source: str, imported_from: str | None = None) -> int:
    """Rows of one test (its unique `source`), optionally of one import source."""
    row = db.execute(
        "SELECT count(*) FROM audit_events WHERE source = %s "
        "AND (%s::text IS NULL OR imported_from = %s)",
        (source, imported_from, imported_from),
    ).fetchone()
    db.commit()
    assert row is not None
    return int(row[0])


def test_the_stream_id_becomes_the_signed_event_uid_and_the_row_is_normalized() -> None:
    row = backfill.normalize(
        _raw(id=(1 << 64) - 1, level="INFO", machine=None, attributes=None), imported_from="x"
    )

    assert row.event_uid == -1
    assert (row.level, row.machine, row.attributes) == ("info", "", {})


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"category": "telemetry"}, "not-audit"),
        ({"event_name": ""}, "no-event-name"),
        ({"level": "notice"}, "bad-level"),
        ({"id": None}, "no-id"),
        ({"ts": "not a time"}, "bad-ts"),
    ],
)
def test_a_row_the_table_would_reject_is_skipped_with_its_reason(
    over: dict[str, Any], reason: str
) -> None:
    with pytest.raises(backfill.SkippedRowError, match=reason):
        backfill.normalize(_raw(**over), imported_from="x")


def test_the_plan_keeps_each_event_once_in_source_precedence_and_counts_what_it_skips() -> None:
    shared = _row("loki-archive", id=5)
    found = backfill.plan(
        [
            _stream("loki-archive", shared, _row("loki-archive", event_name="send_message")),
            _stream("loki-live", _row("loki-live", id=5), Skipped("bad-level")),
            _stream("jsonl", _row("jsonl", event_name="exit")),
        ]
    )

    assert found.read == {"loki-archive": 2, "loki-live": 1, "jsonl": 1}
    assert found.duplicates == {"loki-live": 1}
    assert found.skipped == {("loki-live", "bad-level"): 1}
    assert len(found.uids) == 3
    assert found.by_name[("loki-archive", "spawn")] == 1


def test_apply_inserts_once_marks_the_source_and_is_idempotent(db_conn: psycopg.Connection) -> None:
    marker = uuid.uuid4().hex
    rows = [_row("loki-archive", source=marker), _row("jsonl", source=marker, event_name="exit")]

    first = backfill.apply([_stream("a", *rows)])
    second = backfill.apply([_stream("a", *rows)])

    assert first == (2, 0)
    assert second == (0, 2)
    assert _count(db_conn, marker) == 2
    assert _count(db_conn, marker, "loki-archive") == 1
    assert _count(db_conn, marker, "jsonl") == 1


def test_a_row_the_live_path_already_wrote_is_kept_untouched(db_conn: psycopg.Connection) -> None:
    marker = uuid.uuid4().hex
    live = _row("loki-live", source=marker, id=777_001)
    db_conn.execute(
        "INSERT INTO audit_events (event_uid, ts, machine, process, event_name, level, source) "
        "VALUES (%s, %s, 'live', 'live', 'spawn', 'info', %s)",
        (live.event_uid, _TS, marker),
    )
    db_conn.commit()

    inserted, present = backfill.apply([_stream("a", live)])

    assert (inserted, present) == (0, 1)
    row = db_conn.execute(
        "SELECT machine, imported_from FROM audit_events WHERE event_uid = %s", (live.event_uid,)
    ).fetchone()
    db_conn.commit()
    assert row == ("live", None)


def test_a_failing_row_rolls_the_whole_apply_back(db_conn: psycopg.Connection) -> None:
    marker = uuid.uuid4().hex
    good = _row(source=marker)
    broken = _row(source=marker)
    object.__setattr__(broken, "level", "loud")  # the CHECK rejects it at the database

    with pytest.raises(psycopg.errors.CheckViolation):
        backfill.apply([_stream("a", good, broken)])

    assert _count(db_conn, marker) == 0


def test_main_dry_run_prints_the_reconciliation_and_writes_nothing(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = uuid.uuid4().hex

    def canned(_dirs: list[Path], _until: datetime) -> list[tuple[str, Any]]:
        return [_stream("loki-live", _row(source=marker), Skipped("bad-level"))]

    monkeypatch.setattr(backfill, "sources", canned)

    assert backfill.main([]) == 0

    out = capsys.readouterr().out
    assert "would insert: 1" in out
    assert "skipped loki-live: bad-level x 1" in out
    assert "dry-run: nothing written" in out
    assert _count(db_conn, marker) == 0


def test_main_apply_writes_and_reports(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = uuid.uuid4().hex

    def canned(_dirs: list[Path], _until: datetime) -> list[tuple[str, Any]]:
        return [_stream("loki-live", _row(source=marker))]

    monkeypatch.setattr(backfill, "sources", canned)

    assert backfill.main(["--apply"]) == 0

    assert "inserted 1 rows" in capsys.readouterr().out
    assert _count(db_conn, marker) == 1


def test_a_full_loki_page_is_bisected_so_no_row_is_lost() -> None:
    start = _TS
    calls: list[tuple[datetime, datetime]] = []
    every = [_raw(id=n, ts=start + timedelta(hours=n)) for n in range(10)]

    def query(lo: datetime, hi: datetime, limit: int) -> tuple[list[dict[str, Any]], bool]:
        calls.append((lo, hi))
        rows = [row for row in every if lo <= row["ts"] <= hi]
        # A tiny page: more than 3 rows in a window reports an incomplete page.
        return (rows[:3], True) if len(rows) > 3 else (rows, False)

    got = list(backfill._loki_windows(start, start + timedelta(hours=12), query))

    assert {row["id"] for row in got} == set(range(10))
    assert len(calls) > 1


def test_jsonl_mirrors_yield_audit_rows_only_and_report_unparsable_lines(tmp_path: Path) -> None:
    audit = _raw(id=11, ts=_TS.isoformat())
    telemetry = _raw(id=12, ts=_TS.isoformat(), category="telemetry")
    late = _raw(id=13, ts=(_TS + timedelta(days=5)).isoformat())
    (tmp_path / "events-20260701.jsonl").write_text(
        "\n".join([json.dumps(audit), json.dumps(telemetry), "{broken", json.dumps(late)]) + "\n"
    )
    (tmp_path / "events-20260701.lineage.jsonl").write_text(
        json.dumps(_raw(id=14, ts=_TS.isoformat()))
    )

    rows = list(backfill.read_jsonl([tmp_path], until=_TS + timedelta(days=1)))

    assert sorted(row.event_uid for row in rows if isinstance(row, AuditRow)) == [11, 14]
    assert [row.reason for row in rows if isinstance(row, Skipped)] == ["unparsable-line"]
