"""The weekly recovery drill: prove the newest backup and the WAL behind it restore, and read.

Run by the daily tick, before that day's backup, once a week (and by hand through
`ava backup walg drill`). It restores what a lost host would need right now, the newest
backup, with the same code as a real restore (`restore.py`), into a scratch directory,
and checks three things:

- **The chain is continuous.** The recovery target is the *start* LSN of the newest WAL
  segment that is already archived (`pg_stat_archiver.last_archived_wal`), not its end:
  Postgres stops at the first record at or beyond the target, and the first record after
  a segment's end is in a segment that is not archived yet and would never be read. A
  missing or unreadable segment anywhere between the backup and that point ends recovery
  in Postgres' own FATAL, so a drill that promotes proves every segment in between is
  present, decrypts and replays. Without a newer segment than the backup's end there is
  nothing to prove, and the drill recovers to the end of the archive instead; the record
  says so (`target_lsn` is None).
- **The data is readable.** The promoted database goes through the same check the logical
  restore drill uses (`verify_restored_database`): the required tables, row counts and a
  real agent conversation read back through the production checkpoint reader.
- **The target was reached** (`pg_current_wal_lsn()` on the promoted instance).

The scratch copy lives on `select_throwaway_base(required_bytes)`, where `required_bytes`
is the backup's uncompressed size, the WAL archived since it started (read from the source)
and `max_wal_size`: all known quantities, no multiplier. It is removed in every outcome.

A drill never raises and never stops the backup that follows it: the outcome is a
`DrillRecord` for the state file, whose failure the health probe reports.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import psycopg

from base.cluster.dataplane.pg_throwaway_base import select_throwaway_base
from base.db import Database, connect_url, pg_admin
from base.log import logger
from services.backup.walg.backups import Backup
from services.backup.walg.config import WalgConfigError
from services.backup.walg.pg_target import PgTarget
from services.backup.walg.probe import DRILL_PERIOD, TICK_PERIOD
from services.backup.walg.restore import RecoveryTarget, RestoreError, restored_instance
from services.backup.walg.runner import WalgCommandError
from services.backup.walg.state import DrillRecord

Report = Callable[[str], None]

_CONNECT_TIMEOUT_S = 5
_DETAIL_CHARS = 1000

# `<timeline><log><segment>` in hex, optionally followed by `.partial` or `.<offset>.backup`.
_SEGMENT_FILE = re.compile(r"[0-9A-F]{8}([0-9A-F]{8})([0-9A-F]{8})(?:\..+)?")
_LSN_SPLIT = 1 << 32

_EXPECTED_FAILURES = (
    RestoreError,
    WalgCommandError,
    WalgConfigError,
    psycopg.Error,
    OSError,
    RuntimeError,  # insufficient scratch space, a failed content check
)


def drill_due(previous: DrillRecord | None, backups: list[Backup], now: datetime) -> bool:
    """Whether a drill should run now: a backup exists and none succeeded for a period.

    The test is one tick period short of the full period, because ticks are a period
    apart: with the full period, a drill that finished a few minutes into its tick would
    be a few minutes short at the next one and wait a whole extra day.
    """
    if not backups:
        return False
    last_ok = None if previous is None else previous.last_ok_at
    return last_ok is None or now - last_ok >= DRILL_PERIOD - TICK_PERIOD


def format_lsn(value: int) -> str:
    """`33554472` as Postgres writes it: `0/2000028`."""
    return f"{value // _LSN_SPLIT:X}/{value % _LSN_SPLIT:X}"


def segment_start_lsn(wal_file: str, segment_bytes: int) -> int | None:
    """The LSN at which the WAL segment named `wal_file` starts; None for a file that is no segment.

    Timeline history files have no segment number. A `.partial` segment or a backup label
    file belongs to the segment it names.
    """
    match = _SEGMENT_FILE.fullmatch(wal_file)
    if match is None:
        return None
    log, segment = int(match[1], 16), int(match[2], 16)
    return (log * (_LSN_SPLIT // segment_bytes) + segment) * segment_bytes


@dataclass(frozen=True)
class SourceFacts:
    """What the live database says about its archive, read once before the restore."""

    last_archived_wal: str | None
    segment_bytes: int
    max_wal_size_bytes: int
    wal_since_backup_bytes: int


def read_source_facts(target: PgTarget, backup: Backup) -> SourceFacts:
    with pg_admin.connect(
        target.admin_url,
        expected_data_dir=target.data_dir,
        autocommit=True,
        connect_timeout=_CONNECT_TIMEOUT_S,
    ) as conn:
        row = conn.execute(
            "SELECT last_archived_wal, pg_size_bytes(current_setting('wal_segment_size')), "
            "pg_size_bytes(current_setting('max_wal_size')), "
            "pg_wal_lsn_diff(pg_current_wal_lsn(), %s::pg_lsn) FROM pg_stat_archiver",
            (format_lsn(backup.start_lsn),),
        ).fetchone()
    if row is None:
        raise RuntimeError("Postgres returned no archiver state")
    return SourceFacts(
        last_archived_wal=None if row[0] is None else str(row[0]),
        segment_bytes=int(row[1]),
        max_wal_size_bytes=int(row[2]),
        wal_since_backup_bytes=max(int(row[3]), 0),
    )


def required_bytes(backup: Backup, facts: SourceFacts) -> int:
    """Room the scratch copy needs: the backup, the WAL since it started, one `max_wal_size`."""
    return backup.uncompressed_bytes + facts.wal_since_backup_bytes + facts.max_wal_size_bytes


def target_lsn(backup: Backup, facts: SourceFacts) -> int | None:
    """The start of the newest archived segment, if it is newer than the backup's end."""
    if facts.last_archived_wal is None:
        return None
    start = segment_start_lsn(facts.last_archived_wal, facts.segment_bytes)
    return start if start is not None and start > backup.finish_lsn else None


def _admin_user(target: PgTarget) -> str:
    user = urlsplit(target.admin_url).username
    if not user:
        raise RuntimeError("the admin URL names no user")
    return user


def _verify(
    instance_url: str, reached: int | None, *, database_for_url: Callable[[str], Database]
) -> str:
    """Content check and target check on the promoted scratch instance; returns the summary."""
    from scripts.data_plane_ops.restore_drill import verify_restored_database

    report = verify_restored_database(instance_url, database_for_url=database_for_url)
    if reached is not None:
        with connect_url(instance_url, autocommit=True, connect_timeout=_CONNECT_TIMEOUT_S) as conn:
            row = conn.execute(
                "SELECT pg_current_wal_lsn() >= %s::pg_lsn", (format_lsn(reached),)
            ).fetchone()
        if row is None or not row[0]:
            raise RestoreError("the recovered instance was promoted before the recovery target")
    return (
        f"{report.agents} agents, {report.checkpoints} checkpoints, "
        f"conversation of agent {report.sample_agent_id} has {report.sample_message_count} messages"
    )


def _restore_and_verify(
    target: PgTarget,
    backup: Backup,
    facts: SourceFacts,
    reached: int | None,
    report: Report,
    *,
    path_reader: Callable[[], Path | None],
    database_for_url: Callable[[str], Database],
) -> str:
    """Restore `backup` to the newest archived segment and verify it; returns the summary."""
    base = select_throwaway_base(required_bytes(backup, facts))
    report(
        f"drill: restoring {backup.name} on {base} to "
        f"{'LSN ' + format_lsn(reached) if reached is not None else 'the end of the archive'}"
    )
    scratch = Path(tempfile.mkdtemp(prefix="ava-walg-drill-", dir=base))
    try:
        with restored_instance(
            scratch / "data",
            backup=backup.name,
            target=RecoveryTarget(lsn=None if reached is None else format_lsn(reached)),
            report=report,
            user=_admin_user(target),
            keep_data=False,
            path_reader=path_reader,
        ) as instance:
            summary = _verify(
                instance.url(target.database), reached, database_for_url=database_for_url
            )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return summary


def run_drill(
    target: PgTarget,
    backup: Backup,
    previous: DrillRecord | None,
    report: Report,
    now: Callable[[], datetime],
    *,
    path_reader: Callable[[], Path | None],
    database_for_url: Callable[[str], Database],
) -> DrillRecord:
    """Run one drill against `backup`; every failure becomes a record with `ok=False`."""
    started = time.monotonic()
    reached: int | None = None
    try:
        facts = read_source_facts(target, backup)
        reached = target_lsn(backup, facts)
        detail = _restore_and_verify(
            target,
            backup,
            facts,
            reached,
            report,
            path_reader=path_reader,
            database_for_url=database_for_url,
        )
        ok = True
    except _EXPECTED_FAILURES as exc:
        ok, detail = False, str(exc)
    except Exception as exc:  # any other failure is still the drill's, never a crash of the tick
        logger.error(f"[walg] recovery drill failed: {traceback.format_exc()}")
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    finished = now()
    return DrillRecord(
        finished_at=finished,
        ok=ok,
        backup=backup.name,
        target_lsn=None if reached is None else format_lsn(reached),
        seconds=round(time.monotonic() - started, 1),
        detail=detail[:_DETAIL_CHARS],
        last_ok_at=finished if ok else (None if previous is None else previous.last_ok_at),
    )
