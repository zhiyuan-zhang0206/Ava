"""Durable desired revisions and live PTY command provenance for convergence."""

import re
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, NamedTuple

from psycopg_pool import ConnectionPool

from base.cluster import session_name
from base.db.transaction import write_transaction
from base.sessions.pty import client

# Keep provenance of already-running sessions across the entrypoint migration.
_REVISION = re.compile(
    r"-m (?:services\.wake\.schedule_manager|gateway\.schedules)\.runner \d+ (\d+);"
)


class Desired(NamedTuple):
    enabled: bool
    status: str
    revision: int
    applied: int


@contextmanager
def claim_convergence(pool: ConnectionPool[Any], schedule_id: int) -> Generator[bool, None, None]:
    """One manager owns this schedule's process effects across independent processes.

    The advisory transaction survives inner desired/applied commits but is
    released by connection failure. Desired writes remain free to advance;
    their conditional revision writes prevent this owner acknowledging newer work.
    """
    with write_transaction(pool) as conn:
        row = conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended('ava.schedule-convergence:' || %s::text, 0))",
            (schedule_id,),
        ).fetchone()
        assert row is not None  # noqa: S101 — SELECT always returns one boolean
        yield bool(row[0])


def desired(pool: ConnectionPool[Any], schedule_id: int) -> Desired | None:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT enabled, status, desired_revision, applied_revision FROM schedules WHERE id = %s",
            (schedule_id,),
        ).fetchone()
    return None if row is None else Desired(*row)


def launched_revision(schedule_id: int) -> int | None:
    """Read provenance from the PTY owner; missing evidence never permits replacement.

    A legacy command belongs to revision zero. The allocation generation is a
    separate release identity, still checked by the manager's exact PTY cleanup.
    """
    name = session_name(f"schedule-{schedule_id}")
    infos = client.list_sessions(name, include_initial_command=True)
    info = next((item for item in infos if item.name == name), None)
    if info is None or info.initial_command is None:
        return None
    match = _REVISION.search(info.initial_command)
    return 0 if match is None else int(match.group(1))


def mark_applied(pool: ConnectionPool[Any], schedule_id: int, revision: int) -> None:
    with write_transaction(pool) as conn:
        conn.execute(
            "UPDATE schedules SET applied_revision = %s WHERE id = %s AND desired_revision = %s",
            (revision, schedule_id, revision),
        )
