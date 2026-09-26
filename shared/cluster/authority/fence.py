"""Close old writers: revoke, then prove closure by census, never by signal.

``revoke`` records the unrevoked generation as revoking and runs the NOLOGIN
sweep. ``prove_closure`` terminates surviving sessions with the positive-
timeout ``pg_terminate_backend(pid, timeout)`` and re-censuses; only an empty
census counts. A ``true`` return is a sent signal and proves nothing, a
``false`` return is recorded, and a session still present after the bounded
rounds is a hold with evidence. Any prepared transaction holds: it is never
rolled back automatically.

The census covers the named roles, every session whose role can no longer log
in, and every session whose role was dropped underneath it (PostgreSQL lets
``DROP ROLE`` succeed while its sessions live on). A backend that passed
authentication just before its role lost LOGIN may appear after an empty
census. For a generation login that is harmless: its only capability is group
membership, which the sweep revokes in the same transaction. A legacy owner
session racing the cutover's demotion is excluded by the cutover's own
precondition (no application root running), not by this census.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from shared.cluster.authority.catalog import Conn, memberships, require_admin, role_facts
from shared.cluster.authority.ledger import begin_revoke, mark_closed
from shared.cluster.authority.model import (
    CatalogRefusedError,
    ClosureEvidence,
    ClosureRefusedError,
    OperationAuthority,
    PreparedTransaction,
    Revoked,
    SurvivingSession,
)
from shared.cluster.authority.roles import SweepResult, fenced_roles, sweep

TERMINATE_TIMEOUT_MS = 5000
CLOSURE_ROUNDS = 3

_CENSUS = """
SELECT a.pid, a.usename, a.datname, a.state, a.xact_start::text, a.wait_event
FROM pg_stat_activity a
LEFT JOIN pg_roles r ON r.oid = a.usesysid
WHERE a.pid <> pg_backend_pid()
  AND a.usesysid IS NOT NULL
  AND (a.usename = ANY(%s) OR r.oid IS NULL OR NOT r.rolcanlogin)
ORDER BY a.pid
"""


def _census(conn: Conn, roles: tuple[str, ...]) -> tuple[SurvivingSession, ...]:
    conn.execute("SELECT pg_stat_clear_snapshot()")
    rows = conn.execute(_CENSUS, (list(roles),)).fetchall()
    return tuple(SurvivingSession(*row) for row in rows)


def _terminate(conn: Conn, pid: int, timeout_ms: int) -> bool:
    row = conn.execute("SELECT pg_terminate_backend(%s, %s)", (pid, timeout_ms)).fetchone()
    return row is not None and row[0] is True


def _prepared(conn: Conn) -> tuple[PreparedTransaction, ...]:
    rows = conn.execute("SELECT gid, owner, database FROM pg_prepared_xacts ORDER BY gid")
    return tuple(PreparedTransaction(*row) for row in rows)


def _require_fenced(conn: Conn, roles: tuple[str, ...]) -> None:
    """Closure is only meaningful for roles that can no longer log in or inherit."""
    violations = [
        f"{name} can still log in"
        for name, fact in role_facts(conn, roles).items()
        if fact.login or fact.password is not None
    ]
    violations += [
        f"{row.member} is still a member of {row.role}"
        for row in memberships(conn, roles)
        if row.member in roles
    ]
    if violations:
        raise CatalogRefusedError(tuple(violations))


def prove_closure(
    conn: Conn,
    roles: Iterable[str],
    *,
    rounds: int = CLOSURE_ROUNDS,
    timeout_ms: int = TERMINATE_TIMEOUT_MS,
) -> ClosureEvidence:
    """Terminate and re-census until no stale session survives; then require
    zero prepared transactions.

    Each named role that exists must already be NOLOGIN without a password and
    a member of nothing. Raises ``ClosureRefusedError`` with the surviving sessions,
    the unconfirmed signals (``false`` returns) or the prepared transactions.
    """
    if rounds < 1 or timeout_ms <= 0:
        raise ValueError("closure needs at least one round and a positive timeout")
    require_admin(conn)
    names = tuple(sorted(set(roles)))
    _require_fenced(conn, names)
    terminated = attempt = 0
    unconfirmed: list[int] = []
    for attempt in range(rounds + 1):
        survivors = _census(conn, names)
        if not survivors:
            break
        if attempt == rounds:
            raise ClosureRefusedError(
                "sessions survived termination",
                survivors=survivors,
                unconfirmed_signals=tuple(unconfirmed),
            )
        for session in survivors:
            if _terminate(conn, session.pid, timeout_ms):
                terminated += 1
            else:
                unconfirmed.append(session.pid)
    prepared = _prepared(conn)
    if prepared:
        raise ClosureRefusedError("prepared transactions exist", prepared=prepared)
    return ClosureEvidence(roles=names, terminated=terminated, rounds=attempt)


def revoke(conn: Conn, home: Path, authority: OperationAuthority) -> SweepResult:
    """Record the unrevoked generation as revoking, then sweep it (retry-safe)."""
    require_admin(conn)
    begin_revoke(home, authority)
    return sweep(conn, home)


def close_revoked(
    conn: Conn,
    home: Path,
    authority: OperationAuthority,
    *,
    rounds: int = CLOSURE_ROUNDS,
    timeout_ms: int = TERMINATE_TIMEOUT_MS,
) -> tuple[ClosureEvidence, tuple[Revoked, ...]]:
    """Prove closure over the home's fenced roles and record it in the ledger.

    The caller has stopped the owned pooler first: a pooler holds backend
    sessions as the old logins and a reload does not revoke them.
    """
    evidence = prove_closure(conn, fenced_roles(conn, home), rounds=rounds, timeout_ms=timeout_ms)
    return evidence, mark_closed(home, authority, evidence)
