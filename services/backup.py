"""Daily local Postgres backup, driven by the watchdog tick on gateway hosts.

One `pg_dump --format=custom` per day into `$AVA_HOME/backups/db/`,
keeping the newest BACKUP_KEEP dumps. The watchdog runs
`maybe_run_daily_backup` every tick on gateway-capable hosts (registered like
a healthcheck, so `ava start --disable-service pg-backup` disables it); the
function is a no-op until the first tick at/after BACKUP_HOUR CLUSTER time with
no dump for the current cluster day — a host that was down at 03:00 catches up
on its next tick instead of skipping the day.

Two clocks, deliberately different ones:

- **When to run** is the cluster wall clock (`AVA_TIMEZONE`, cluster-pinned):
  "03:00" means the same instant for every gateway in the fleet, and it does
  not move when a machine does. Reading the host's OS timezone instead used to
  skip backups outright — carry a laptop from Asia/Shanghai to US/Pacific and
  the newest dump's local date sits ahead of the new local date, so `is_due`
  returned False (silently, with no error to notice) until the calendar caught
  up.
- **What a dump is called** is UTC, stamped `Z`: `<db>-YYYYMMDDTHHMMSSZ.dump`.
  Prune deletes the oldest dumps by this stamp, so the ordering must be a
  total order over real instants. A local-time name is not: in the DST fall-back
  hour the same wall clock names two instants an hour apart, and re-parsing it
  as naive-then-local made "which backup do we delete" depend on which offset
  the parse happened to pick.

Local dumps guard against bad migrations / accidental deletes / DB
corruption — not against the host's disk dying. Storage interface:
`run_backup` is `dump -> (future: upload) -> prune`; an off-site backend
(R2 / GCS) slots in as an upload step between the two — tracked in
future/infra/pg-backup.md.

Only files matching this module's naming are managed (counted for due-ness,
pruned); a hand-made dump parked in the same directory is never touched. Dumps
written before the UTC naming (`<dbname>-YYYYMMDD-HHMMSS.dump`, host wall
clock) stay managed so they still prune out instead of stranding a week of
files: their stamp is read in cluster time, which is the closest well-defined
reading of a name that never recorded an offset.

Restore (replaces the live DB's contents):
    pg_restore --clean --if-exists -d "<db_url>" <dump-file>
"""

from __future__ import annotations

import logging
import re
import subprocess
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from psycopg.conninfo import conninfo_to_dict

from shared.config import settings
from shared.db import direct_db_url
from shared.pg_tools import pg_tool
from shared.private_storage import ensure_private_dir, ensure_private_file

_log = logging.getLogger(__name__)

BACKUP_HOUR = 3  # cluster time; the first tick at/after this hour runs the day's backup
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
# are rebuildable from it (a one-off script proved the reconstruction
# pre-cutover; the restore reference in recover-a-cluster documents the
# procedure).
# A restore therefore loses in-flight graph state (pending interrupts etc.),
# not the conversation history.
_EXCLUDE_TABLES = ("checkpoint_blobs", "checkpoints", "checkpoint_writes")

_NAME_RE = re.compile(r"^(?P<db>.+)-(?P<ts>\d{8}T\d{6}Z|\d{8}-\d{6})\.dump$")
_TS_FORMAT = "%Y%m%dT%H%M%SZ"  # UTC, offset-bearing by construction
_LEGACY_TS_FORMAT = "%Y%m%d-%H%M%S"  # pre-cutover names: wall clock, no offset


def _cluster_tz() -> ZoneInfo:
    """The cluster wall clock every scheduling decision here is made in."""
    return ZoneInfo(settings.general.timezone)


def _require_aware(now: datetime) -> datetime:
    """A naive datetime is rejected rather than read as host-local: silently
    adopting the host's timezone is the exact failure this module was carrying."""
    if now.tzinfo is None:
        raise ValueError(f"backup needs a TZ-aware datetime, got naive {now!r}")
    return now


def _parse_stamp(stamp: str) -> datetime:
    """A managed dump's filename stamp as an aware UTC instant."""
    if stamp.endswith("Z"):
        return datetime.strptime(stamp, _TS_FORMAT).replace(tzinfo=UTC)
    # Legacy name: no offset was ever recorded, so read it in cluster time.
    # `.replace(tzinfo=...)` (fold=0) keeps the reading deterministic through
    # the DST fall-back hour, where `.astimezone()` on a naive value used to
    # pick an offset out of the host's clock.
    legacy = datetime.strptime(stamp, _LEGACY_TS_FORMAT).replace(tzinfo=_cluster_tz())
    return legacy.astimezone(UTC)


def backup_dir() -> Path:
    # `<home>/backups/db`: the home itself already scopes the cluster (path-only
    # identity), so the dump dir needs no per-cluster token. Pre-cutover dumps
    # under `backups/<cluster-name>` are left in place (at most BACKUP_KEEP of
    # them); rotation continues in the new dir.
    return Path(settings.general.ava_home).expanduser() / "backups" / "db"


def _managed_dumps(directory: Path) -> list[tuple[datetime, Path]]:
    """This module's dumps in `directory`, oldest first, keyed by UTC instant."""
    dumps: list[tuple[datetime, Path]] = []
    for path in directory.glob("*.dump"):
        m = _NAME_RE.match(path.name)
        if m:
            dumps.append((_parse_stamp(m["ts"]), path))
    return sorted(dumps)


def is_due(now: datetime) -> bool:
    """True once the cluster clock has passed BACKUP_HOUR with no dump for the
    current cluster day. `now` must be TZ-aware."""
    local_now = _require_aware(now).astimezone(_cluster_tz())
    if local_now.hour < BACKUP_HOUR:
        return False
    dumps = _managed_dumps(backup_dir())
    tz = _cluster_tz()
    return not dumps or dumps[-1][0].astimezone(tz).date() < local_now.date()


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
    now = _require_aware(now) if now is not None else datetime.now(UTC)
    # direct_db_url() (the admin-plane direct URL, derived from the
    # registry record): pg_dump needs a real Postgres session (it holds a consistent
    # snapshot across many statements); running it through a transaction pooler is
    # meaningless and breaks. Admin plane bypasses PgBouncer.
    db_url = db_url if db_url is not None else direct_db_url()
    directory = ensure_private_dir(backup_dir())
    # A run interrupted mid-dump (e.g. the process tree killed during a rollout)
    # leaves `.partial` files behind. The name never matches `_NAME_RE`, so the
    # due/prune logic ignores them and they pile up. Sweep them before writing a
    # new dump; a fresh partial from THIS run is created after the sweep.
    for stale in directory.glob("*.partial"):
        with suppress(OSError):
            stale.unlink()
    dbname = conninfo_to_dict(db_url)["dbname"]
    target = directory / f"{dbname}-{now.astimezone(UTC).strftime(_TS_FORMAT)}.dump"
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
        ensure_private_file(target)
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
    now = datetime.now(UTC)
    if is_due(now):
        run_backup(now)
