"""The recovery drill: the arithmetic and the schedule, then the whole drill on a real Postgres.

The integration tests run a source Postgres that archives into the fake wal-g's store, take
a base backup of it (`pg_basebackup` standing in for `backup-push`), keep writing, and drill:
the backup is fetched back through the fake, recovered to the start of the newest archived
segment by a scratch postmaster, and read through the production checkpoint reader.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver

from base.db import pg_admin
from services.backup.walg import drill, restore
from services.backup.walg.backups import Backup, parse_backups
from services.backup.walg.drill import SourceFacts
from services.backup.walg.pg_target import PgTarget
from services.backup.walg.probe import DRILL_PERIOD, TICK_PERIOD
from services.backup.walg.state import DrillRecord
from services.backup.walg.tests.support import (
    PgInstance,
    Sandbox,
    archive_current_segment,
    archiving_postgres,
    make_sandbox,
    take_basebackup,
)

T0 = datetime(2026, 10, 2, 6, 25, 0, tzinfo=UTC)
SEGMENT = 16 * 1024 * 1024


def _backup(**overrides: Any) -> Backup:
    base = Backup(
        name="base_000000010000000000000003",
        start_time=T0,
        uncompressed_bytes=1000,
        compressed_bytes=100,
        start_lsn=0x3000028,
        finish_lsn=0x3000138,
    )
    return replace(base, **overrides)


def _facts(**overrides: Any) -> SourceFacts:
    base = SourceFacts(
        last_archived_wal="000000010000000000000008",
        segment_bytes=SEGMENT,
        max_wal_size_bytes=1 << 30,
        wal_since_backup_bytes=5 * SEGMENT,
    )
    return replace(base, **overrides)


# ── arithmetic ───────────────────────────────────────────────────────────────


def test_lsns_are_written_as_postgres_writes_them() -> None:
    assert drill.format_lsn(0x2000028) == "0/2000028"
    assert drill.format_lsn(0xA3000000) == "0/A3000000"
    assert drill.format_lsn((7 << 32) + 0x1A) == "7/1A"


@pytest.mark.parametrize(
    ("wal_file", "expected"),
    [
        ("000000010000000000000003", 3 * SEGMENT),
        ("0000000100000001000000FF", (1 * 256 + 255) * SEGMENT),
        ("0000000200000000000000A0", 0xA0 * SEGMENT),
        ("000000010000000000000003.partial", 3 * SEGMENT),
        ("000000010000000000000003.00000028.backup", 3 * SEGMENT),
        ("00000002.history", None),
        ("not-a-wal-file", None),
    ],
)
def test_a_segment_starts_at_its_number_times_the_segment_size(
    wal_file: str, expected: int | None
) -> None:
    assert drill.segment_start_lsn(wal_file, SEGMENT) == expected


def test_a_larger_segment_size_scales_the_start() -> None:
    assert (
        drill.segment_start_lsn("000000010000000000000003", 64 * 1024 * 1024)
        == 3 * 64 * 1024 * 1024
    )
    assert drill.segment_start_lsn("000000010000000100000000", 64 * 1024 * 1024) == 1 << 32


def test_the_target_is_the_start_of_the_newest_archived_segment() -> None:
    assert drill.target_lsn(_backup(), _facts()) == 8 * SEGMENT


@pytest.mark.parametrize(
    "facts",
    [
        _facts(last_archived_wal="000000010000000000000003"),  # the backup's own segment
        _facts(last_archived_wal="00000002.history"),
        _facts(last_archived_wal=None),
    ],
    ids=["nothing-newer-than-the-backup", "history-file", "nothing-archived"],
)
def test_without_a_newer_archived_segment_there_is_no_target(facts: SourceFacts) -> None:
    assert drill.target_lsn(_backup(), facts) is None


def test_the_scratch_space_is_built_from_known_quantities() -> None:
    assert drill.required_bytes(_backup(), _facts()) == 1000 + 5 * SEGMENT + (1 << 30)


# ── the schedule ─────────────────────────────────────────────────────────────


def _record(**overrides: Any) -> DrillRecord:
    base = DrillRecord(
        finished_at=T0,
        ok=True,
        backup="base_1",
        target_lsn=None,
        seconds=1.0,
        detail="",
        last_ok_at=T0,
    )
    return replace(base, **overrides)


def test_a_drill_is_due_when_a_backup_exists_and_none_ever_succeeded() -> None:
    assert drill.drill_due(None, [_backup()], T0)
    assert drill.drill_due(_record(ok=False, last_ok_at=None), [_backup()], T0)


def test_no_drill_is_due_without_a_backup() -> None:
    assert not drill.drill_due(None, [], T0)


def test_the_drill_falls_due_one_tick_period_before_a_full_period() -> None:
    due_at = DRILL_PERIOD - TICK_PERIOD
    assert not drill.drill_due(_record(), [_backup()], T0 + due_at - timedelta(minutes=1))
    assert drill.drill_due(_record(), [_backup()], T0 + due_at)


def test_a_failed_drill_does_not_postpone_the_next_one_but_a_success_does() -> None:
    failed_recently = _record(ok=False, finished_at=T0, last_ok_at=T0 - timedelta(days=7))
    assert drill.drill_due(failed_recently, [_backup()], T0 + timedelta(hours=1))
    assert not drill.drill_due(_record(), [_backup()], T0 + timedelta(days=1))


# ── a failure is a record, never an exception ────────────────────────────────


def _target() -> PgTarget:
    return PgTarget("postgresql://tester@/postgres?host=/s&port=1", Path("/pg"), "ava_main")


def _fail_reading(exc: Exception, monkeypatch: pytest.MonkeyPatch) -> None:
    def raising(_target: PgTarget, _backup: Backup) -> SourceFacts:
        raise exc

    monkeypatch.setattr(drill, "read_source_facts", raising)


def test_an_expected_failure_is_recorded_with_its_message_and_keeps_the_last_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_reading(RuntimeError("no archiver state"), monkeypatch)
    previous = _record(last_ok_at=T0 - timedelta(days=3))

    record = drill.run_drill(_target(), _backup(), previous, lambda _line: None, lambda: T0)

    assert (record.ok, record.detail, record.backup) == (False, "no archiver state", _backup().name)
    assert record.last_ok_at == previous.last_ok_at
    assert record.finished_at == T0


def test_an_unexpected_failure_names_its_type(monkeypatch: pytest.MonkeyPatch) -> None:
    _fail_reading(KeyError("boom"), monkeypatch)

    record = drill.run_drill(_target(), _backup(), None, lambda _line: None, lambda: T0)

    assert not record.ok and record.detail == "KeyError: 'boom'"
    assert record.last_ok_at is None


def test_a_long_failure_message_is_cut_for_the_state_file(monkeypatch: pytest.MonkeyPatch) -> None:
    _fail_reading(RuntimeError("x" * 5000), monkeypatch)

    record = drill.run_drill(_target(), _backup(), None, lambda _line: None, lambda: T0)

    assert len(record.detail) == drill._DETAIL_CHARS


# ── the whole drill, on a real Postgres ──────────────────────────────────────


def _lsn_value(text: str) -> int:
    high, low = text.split("/")
    return (int(high, 16) << 32) + int(low, 16)


def _seed_conversation(url: str, text: str) -> int:
    """One agent row and one checkpoint holding a conversation: what the drill reads back."""
    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    agent_id = int(row[0])
    checkpoint = empty_checkpoint()
    checkpoint["ts"] = datetime.now(UTC).isoformat()
    checkpoint["channel_values"] = {"messages": [HumanMessage(content=text)]}
    checkpoint["channel_versions"] = {"messages": "1", "__start__": "1"}
    with PostgresSaver.from_conn_string(url) as saver:
        saver.put(
            config={"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
            checkpoint=checkpoint,
            metadata={"source": "input", "step": 1, "parents": {}},
            new_versions={"messages": "1"},
        )
    return agent_id


class Source:
    """A source Postgres that archives, has been backed up once and written to since."""

    def __init__(self, sandbox: Sandbox, pg: PgInstance, *, conversations: bool = True) -> None:
        self.sandbox = sandbox
        self.pg = pg
        self.url = f"postgresql://ava@/postgres?host={pg.root}&port={pg.port}"
        self.target = PgTarget(self.url, pg.data, "postgres")
        conn = pg.connect()
        conn.execute("CREATE TABLE agents (id serial PRIMARY KEY)")
        # every pooled session of the production reader reads the code-version gate's row
        conn.execute("CREATE TABLE deployment_state (id int PRIMARY KEY, min_code_version bigint)")
        conn.execute("INSERT INTO deployment_state VALUES (1, 0)")
        with PostgresSaver.from_conn_string(self.url) as saver:
            saver.setup()
        self.agent_before_backup = (
            _seed_conversation(self.url, "before the backup") if conversations else 0
        )
        archive_current_segment(conn, sandbox)
        name = take_basebackup(sandbox, pg, "base_000000010000000000000001")
        label = (sandbox.store_dir / "basebackups" / name / "backup_label").read_text()
        start = re.search(r"START WAL LOCATION: ([0-9A-F]+/[0-9A-F]+)", label)
        assert start is not None
        finish = conn.execute("SELECT pg_current_wal_insert_lsn()").fetchone()
        assert finish is not None
        self.agent_after_backup = (
            _seed_conversation(self.url, "written after the backup") if conversations else 0
        )
        self.after_backup_segment = archive_current_segment(conn, sandbox)
        # the newest archived segment: its start is the target, so it holds nothing needed
        self.last_segment = archive_current_segment(conn, sandbox)
        conn.close()
        sandbox.put(
            "backups.json",
            json.dumps(
                [
                    {
                        "backup_name": name,
                        "start_time": "2026-10-02T06:00:00Z",
                        "uncompressed_size": 40_000_000,
                        "compressed_size": 8_000_000,
                        "start_lsn": _lsn_value(start[1]),
                        "finish_lsn": _lsn_value(str(finish[0])),
                    }
                ]
            ),
        )

    def newest_backup(self) -> Backup:
        return parse_backups((self.sandbox.store_dir / "backups.json").read_text())[-1]


@pytest.fixture(autouse=True)
def uncustodied_admin_dial(monkeypatch: pytest.MonkeyPatch) -> None:
    """The source here is a test instance with no native launch receipt: dial it without
    the custody proof the production admin dial demands."""

    @contextmanager
    def connect(
        url: str, *, expected_data_dir: Path | None = None, **kwargs: Any
    ) -> Generator[Any]:
        assert expected_data_dir is not None  # the drill must still name the data directory
        with psycopg.connect(url, **kwargs) as conn:
            yield conn

    monkeypatch.setattr(pg_admin, "connect", connect)


@pytest.fixture
def scratch_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "scratch-base"
    base.mkdir()

    def select(_required: int) -> Path:
        return base

    monkeypatch.setattr(drill, "select_throwaway_base", select)
    return base


def _run_drill(
    source: Source, previous: DrillRecord | None = None
) -> tuple[DrillRecord, list[str]]:
    lines: list[str] = []
    record = drill.run_drill(
        source.target, source.newest_backup(), previous, lines.append, lambda: T0
    )
    return record, lines


def test_the_drill_restores_the_newest_backup_to_the_newest_archived_segment_and_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scratch_base: Path
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)
    with archiving_postgres() as pg:
        source = Source(sandbox, pg)

        record, lines = _run_drill(source)

    assert record.ok, record.detail
    assert record.target_lsn == drill.format_lsn(
        drill.segment_start_lsn(source.last_segment, SEGMENT) or 0
    )
    # the conversation written after the backup is the newest one: reading it proves the
    # WAL behind the backup was replayed, not only the base backup restored
    assert f"conversation of agent {source.agent_after_backup} has 1 messages" in record.detail
    assert source.agent_after_backup != source.agent_before_backup
    assert record.last_ok_at == T0 and record.backup == source.newest_backup().name
    assert any(line.startswith("drill: restoring base_") for line in lines)
    assert list(scratch_base.iterdir()) == []  # the scratch copy is gone


def test_a_missing_segment_after_the_backup_fails_the_drill_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scratch_base: Path
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)
    sockets: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(prefix: str, dir: str) -> str:
        path = real_mkdtemp(prefix=prefix, dir=dir)
        sockets.append(Path(path))
        return path

    monkeypatch.setattr(restore.tempfile, "mkdtemp", recording_mkdtemp)
    with archiving_postgres() as pg:
        source = Source(sandbox, pg)
        (sandbox.store_dir / "store" / source.after_backup_segment).unlink()

        record, _ = _run_drill(source)

    assert not record.ok
    assert "recovery failed" in record.detail
    assert record.last_ok_at is None
    assert record.target_lsn is not None  # the target was chosen before the restore failed
    assert list(scratch_base.iterdir()) == []
    assert not [path for path in sockets if path.exists() and path.name.startswith("ava-walg-")]


def test_without_anything_archived_after_the_backup_the_drill_recovers_to_the_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scratch_base: Path
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)
    with archiving_postgres() as pg:
        source = Source(sandbox, pg)
        listed = json.loads((sandbox.store_dir / "backups.json").read_text())
        listed[0]["finish_lsn"] = 1 << 60  # the backup "ends" after everything archived
        sandbox.put("backups.json", json.dumps(listed))

        record, _ = _run_drill(source)

    assert record.ok, record.detail
    assert record.target_lsn is None
    assert f"conversation of agent {source.agent_after_backup}" in record.detail


def test_a_database_without_a_conversation_fails_the_content_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scratch_base: Path
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)
    with archiving_postgres() as pg:
        source = Source(sandbox, pg, conversations=False)

        record, _ = _run_drill(source)

    assert not record.ok
    assert "no readable agent conversation" in record.detail
    assert list(scratch_base.iterdir()) == []
