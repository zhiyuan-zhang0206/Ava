"""WAL archiving health: one sentence when it is broken, None when it is fine.

Called by the cluster health probe (`ava health-probe`, every few minutes). Every
condition is a state, not a threshold, and the text of each failure is fixed: the
probe's episode logic keys on the full message, so a number or a timestamp in it
would start a new episode at every run and the alert would never escalate.

- the Postgres that is running must carry the archive settings the code launches
  it with (a retained postmaster keeps its old launch arguments, so "archiving was
  switched on" is not true until the next `ava stop` + `ava start`);
- the archiver must not be failing right now (`last_failed_time` newer than
  `last_archived_time`);
- WAL that is complete but not yet archived must not have been waiting longer than
  the RPO objective. "Waiting" is read from the `.ready` marker files in
  `archive_status/`, the archiver's own work queue, so a database that is merely idle
  (nothing complete, nothing to ship) never looks late. This is the one signal that
  also catches an archive command that hangs, which neither succeeds nor fails;
- the encryption key file must still be the key whose fingerprint this home pinned.

Listing the `.ready` markers needs superuser (or `pg_monitor`): the application
login the health probe normally uses is refused (`permission denied for function
pg_ls_archive_statusdir`), so this probe dials the home's owner-only admin socket
like the other operator-side readers. Nothing here writes.

The probe never raises: anything it cannot read is its own fixed failure.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg

from base.db import pg_admin
from base.log import logger
from services.gateway_side.walg import config as walg_config
from services.gateway_side.walg.archive import RPO_OBJECTIVE_S, ExpectedArchive, expected_archive

_CONNECT_TIMEOUT_S = 5
_STATEMENT_TIMEOUT = "10s"

CONFIG_UNUSABLE = "WAL archiving: the WAL-G configuration is unusable"
KEY_CHANGED = "WAL archiving: the encryption key is not the key this home pinned"
SETTINGS_DIFFER = (
    "WAL archiving: Postgres is not running with the configured archive settings "
    "(ava stop, then ava start)"
)
ARCHIVER_FAILING = "WAL archiving: the archiver is failing"
ARCHIVE_BEHIND = "WAL archiving: complete WAL has been waiting for archive beyond the RPO objective"
UNREADABLE = "WAL archiving: the archiver state is unreadable"


@dataclass(frozen=True)
class ArchiverState:
    """What Postgres reports about its archiver, read-only."""

    archive_mode: str
    archive_command: str
    archive_timeout_s: int
    failing_now: bool
    archived_count: int
    failed_count: int
    oldest_pending_age_s: float | None  # age of the oldest `.ready` marker; None = none pending


@contextmanager
def admin_connection() -> Generator[psycopg.Connection[Any]]:
    """The home's administrator over its owner-only socket, bound to its postmaster."""
    authority = pg_admin.local_owner_authority()
    with pg_admin.connect(
        authority.admin_url,
        expected_data_dir=authority.data_dir,
        autocommit=True,
        connect_timeout=_CONNECT_TIMEOUT_S,
    ) as conn:
        conn.execute(f"SET statement_timeout = '{_STATEMENT_TIMEOUT}'")
        yield conn


def read_archiver_state(conn: psycopg.Connection[Any]) -> ArchiverState:
    settings_row = conn.execute(
        "SELECT current_setting('archive_mode'), current_setting('archive_command'), "
        "(SELECT setting::int FROM pg_settings WHERE name = 'archive_timeout')"
    ).fetchone()
    archiver_row = conn.execute(
        # last_archived_time is NULL until the first success; last_failed_time is NULL
        # until the first failure. A failure counts only while it is the newer event.
        "SELECT coalesce(last_failed_time > coalesce(last_archived_time, '-infinity'), false), "
        "archived_count, failed_count FROM pg_stat_archiver"
    ).fetchone()
    pending_row = conn.execute(
        "SELECT extract(epoch FROM now() - min(modification))::float8 "
        "FROM pg_ls_archive_statusdir() WHERE name LIKE '%.ready'"
    ).fetchone()
    if settings_row is None or archiver_row is None or pending_row is None:
        raise RuntimeError("Postgres returned no archiver state")
    return ArchiverState(
        archive_mode=str(settings_row[0]),
        archive_command=str(settings_row[1]),
        archive_timeout_s=int(settings_row[2]),
        failing_now=bool(archiver_row[0]),
        archived_count=int(archiver_row[1]),
        failed_count=int(archiver_row[2]),
        oldest_pending_age_s=None if pending_row[0] is None else float(pending_row[0]),
    )


def settings_differ(state: ArchiverState, expected: ExpectedArchive) -> bool:
    return (state.archive_mode, state.archive_timeout_s, state.archive_command) != (
        expected.mode,
        expected.timeout_s,
        expected.command,
    )


def judge(state: ArchiverState, expected: ExpectedArchive) -> str | None:
    """The first broken condition, or None. Later conditions presuppose earlier ones."""
    if settings_differ(state, expected):
        return SETTINGS_DIFFER
    if state.failing_now:
        return ARCHIVER_FAILING
    if state.oldest_pending_age_s is not None and state.oldest_pending_age_s > RPO_OBJECTIVE_S:
        return ARCHIVE_BEHIND
    return None


def configuration_failure() -> str | None:
    """A broken configuration or a swapped key, from the files alone."""
    path = walg_config.configured_path()
    if path is None:
        return None
    try:
        config = walg_config.read_config(path)
        key_problem = walg_config.pin_problem(config)
    except walg_config.WalgConfigError as exc:
        logger.warning(f"[walg] probe: {exc}")
        return CONFIG_UNUSABLE
    if key_problem is not None:
        logger.warning(f"[walg] probe: {key_problem}")
        return KEY_CHANGED
    return None


def failure() -> str | None:
    """The first thing wrong with WAL archiving, or None (also None while it is off)."""
    expected = expected_archive()
    if expected is None:
        return None
    configuration = configuration_failure()
    if configuration is not None:
        return configuration
    try:
        with admin_connection() as conn:
            state = read_archiver_state(conn)
    except Exception as exc:  # the probe must never raise; the cause is logged
        logger.warning(f"[walg] probe: archiver state unreadable: {type(exc).__name__}: {exc}")
        return UNREADABLE
    return judge(state, expected)
