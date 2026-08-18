"""Daily local Postgres backup, driven by the watchdog tick on gateway hosts.

One `pg_dump --format=custom` per day into `$AVA_HOME/backups/db/`,
keeping the newest BACKUP_KEEP dumps. The watchdog runs
`maybe_run_daily_backup` every tick on gateway-capable hosts (registered like
a healthcheck, so `ava start --disable-service pg-backup` disables it); the
function is a no-op until the first tick at/after BACKUP_HOUR local time with
no dump for the current day — a host that was down at 03:00 catches up on its
next tick instead of skipping the day.

Local dumps guard against bad migrations / accidental deletes / DB
corruption — not against the host's disk dying. Storage interface:
`run_backup` is `dump -> (future: upload) -> prune`; an off-site backend
(R2 / GCS) slots in as an upload step between the two — tracked in
future/infra/pg-backup.md.

Only files matching this module's `<dbname>-YYYYMMDD-HHMMSS.dump` naming are
managed (counted for due-ness, pruned); a hand-made dump parked in the same
directory is never touched.

Restore (replaces the live DB's contents):
    pg_restore --clean --if-exists -d "<db_url>" <dump-file>
"""

from __future__ import annotations

import logging
import re
import subprocess
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

from shared.config import settings
from shared.db import direct_db_url
from shared.pg_tools import pg_tool

_log = logging.getLogger(__name__)

BACKUP_HOUR = 3  # local time; the first tick at/after this hour runs the day's backup
BACKUP_KEEP = 7  # a week of daily dumps: a bad migration found a day later must not have overwritten the last good copy
# Generous ceiling for one dump (the DB is far smaller); see the comment at the
# subprocess.run call for why an unbounded pg_dump is not acceptable here.
# 60 min (was 30): the post-checkpoint-exclusion dump is ~5 min, this is pure
# headroom against a stall — see `_EXCLUDE_TABLES` for why the dump shrank.
_DUMP_TIMEOUT_S = 60 * 60
# LangGraph runtime tables excluded from the daily dump. checkpoint_blobs alone
# is ~18 GB and grows unboundedly (no retention — every agent's full history),
# which pushed the full dump past the old 30-min timeout into a kill-retry
# loop (dump killed -> partial swept -> retry -> killed; the old dump never
# pruned, disk stuck ~90%). The checkpoints are RUNTIME execution state, not
# the system of record: the conversation stream lives in the `events` table
# (the unified read path since the W9 cutover), which IS dumped, and checkpoints
# are rebuildable from it (scripts/preview/rebuild-checkpoints.py proved the
# reconstruction; the restore reference in recover-a-cluster documents it).
# A restore therefore loses in-flight graph state (pending interrupts etc.),
# not the conversation history.
_EXCLUDE_TABLES = ("checkpoint_blobs", "checkpoints", "checkpoint_writes")

_NAME_RE = re.compile(r"^(?P<db>.+)-(?P<ts>\d{8}-\d{6})\.dump$")
_TS_FORMAT = "%Y%m%d-%H%M%S"


def backup_dir() -> Path:
    # `<home>/backups/db`: the home itself already scopes the cluster (path-only
    # identity), so the dump dir needs no per-cluster token. Pre-cutover dumps
    # under `backups/<cluster-name>` are left in place (at most BACKUP_KEEP of
    # them); rotation continues in the new dir.
    return Path(settings.general.ava_home).expanduser() / "backups" / "db"


def _managed_dumps(directory: Path) -> list[tuple[datetime, Path]]:
    """This module's dumps in `directory`, oldest first."""
    dumps: list[tuple[datetime, Path]] = []
    for path in directory.glob("*.dump"):
        m = _NAME_RE.match(path.name)
        if m:
            dumps.append((datetime.strptime(m["ts"], _TS_FORMAT).astimezone(), path))
    return sorted(dumps)


def is_due(now: datetime) -> bool:
    if now.hour < BACKUP_HOUR:
        return False
    dumps = _managed_dumps(backup_dir())
    return not dumps or dumps[-1][0].date() < now.date()


def _prune(directory: Path) -> list[Path]:
    """Delete managed dumps beyond the newest BACKUP_KEEP; return what was removed."""
    removed: list[Path] = []
    for _ts, path in _managed_dumps(directory)[:-BACKUP_KEEP]:
        path.unlink()
        removed.append(path)
    return removed


def run_backup(now: datetime | None = None, *, db_url: str | None = None) -> Path:
    """Dump the cluster DB into backup_dir() and prune; return the dump path.

    The dump is written to a `.partial` name and renamed only on pg_dump
    success, so a crash mid-dump never leaves a file that the prune/due logic
    (or a human restoring) would mistake for a complete backup.
    """
    now = now if now is not None else datetime.now().astimezone()
    # direct_db_url() (the admin-plane direct URL, derived from the
    # registry record): pg_dump needs a real Postgres session (it holds a consistent
    # snapshot across many statements); running it through a transaction pooler is
    # meaningless and breaks. Admin plane bypasses PgBouncer.
    db_url = db_url if db_url is not None else direct_db_url()
    directory = backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    # A run interrupted mid-dump (e.g. the process tree killed during a rollout)
    # leaves `.partial` files behind. The name never matches `_NAME_RE`, so the
    # due/prune logic ignores them and they pile up. Sweep them before writing a
    # new dump; a fresh partial from THIS run is created after the sweep.
    for stale in directory.glob("*.partial"):
        with suppress(OSError):
            stale.unlink()
    dbname = conninfo_to_dict(db_url)["dbname"]
    target = directory / f"{dbname}-{now.strftime(_TS_FORMAT)}.dump"
    partial = target.with_name(target.name + ".partial")
    cmd = [
        str(pg_tool("pg_dump")),
        "--format=custom",
        "--file",
        str(partial),
        "--dbname",
        db_url,
    ]
    for table in _EXCLUDE_TABLES:
        cmd += ["--exclude-table", table]
    try:
        # The timeout is load-bearing: the watchdog tick awaits this check, so
        # a pg_dump hung on a lock or stalled I/O would otherwise stall every
        # subsequent healthcheck forever. subprocess.run kills the child on
        # expiry and TimeoutExpired propagates as a failing check.
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, check=False, timeout=_DUMP_TIMEOUT_S
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pg_dump exited {proc.returncode}: {proc.stderr.strip()[:500]}")
        partial.rename(target)
    finally:
        # suppress: a cleanup unlink failure must not mask the dump/rename error.
        with suppress(OSError):
            partial.unlink(missing_ok=True)
    removed = _prune(directory)
    _log.info(
        "[backup] wrote %s (%.1f MiB), pruned %d",
        target,
        target.stat().st_size / 2**20,
        len(removed),
    )
    return target


def maybe_run_daily_backup() -> None:
    """Watchdog tick entry: run the day's backup if due, else no-op.

    Raises on pg_dump failure — the watchdog tick logs it like any failing
    check and retries next tick (is_due stays true until a dump succeeds).
    """
    now = datetime.now().astimezone()
    if is_due(now):
        run_backup(now)
