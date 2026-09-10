"""Explicit TTL tracking for persistent shell sessions."""

import contextlib
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

import ava
from shared.platform import IS_WINDOWS

pytestmark = [
    pytest.mark.skipif(IS_WINDOWS, reason="PTY supervisor is POSIX-only"),
    pytest.mark.usefixtures("_pty_sessions_env", "_isolated_agent"),
]


def _deadline(conn: psycopg.Connection, agent_id: int, session_id: int) -> datetime | None:
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT expires_at FROM agent_shell_ttls WHERE agent_id = %s AND session_id = %s",
            (agent_id, session_id),
        )
        row = cur.fetchone()
    return row[0] if row is not None else None


def _facts(conn: psycopg.Connection, agent_id: int, session_id: int) -> tuple[int, object]:
    """The row's display facts: (renewals, last_renewed_at)."""
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT renewals, last_renewed_at FROM agent_shell_ttls "
            "WHERE agent_id = %s AND session_id = %s",
            (agent_id, session_id),
        )
        row = cur.fetchone()
    assert row is not None, "agent_shell_ttls row missing"
    return row[0], row[1]


def _renewal_rows(
    conn: psycopg.Connection, agent_id: int, session_id: int
) -> list[tuple[float, datetime, datetime]]:
    """The audit trail for one session, oldest first."""
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT requested_ttl_seconds, prev_expires_at, new_expires_at "
            "FROM agent_shell_ttl_renewals WHERE agent_id = %s AND session_id = %s "
            "ORDER BY id",
            (agent_id, session_id),
        )
        return [(float(r[0]), r[1], r[2]) for r in cur.fetchall()]


def test_sessions_new_tracks_explicit_ttl(db_conn: psycopg.Connection, _agent_row: int) -> None:
    before = datetime.now(UTC)
    session_id = ava.shell.sessions.new("test-ttl", ttl=120)
    try:
        expires_at = _deadline(db_conn, _agent_row, session_id)
        assert expires_at is not None
        assert (
            before + timedelta(seconds=119)
            <= expires_at
            <= datetime.now(UTC) + timedelta(seconds=121)
        )
    finally:
        with contextlib.suppress(ValueError, RuntimeError):
            ava.shell.sessions.kill(session_id)


def test_sessions_new_requires_ttl() -> None:
    """TTL is mandatory (user ruling 2026-08-27): omitting it is a TypeError."""
    with pytest.raises(TypeError):
        ava.shell.sessions.new("test-no-ttl")  # type: ignore[call-arg]


def test_create_session_requires_ttl() -> None:
    """The internal creation boundary is mandatory too (task #2614): every
    creation path — shells and watchers — must carry a deadline."""
    with pytest.raises(TypeError):
        ava.shell.sessions._create_session("test-no-ttl")  # type: ignore[call-arg]


def test_run_background_tracks_explicit_ttl(db_conn: psycopg.Connection, _agent_row: int) -> None:
    handle = ava.shell.run_background("sleep 60", name="test-bg-ttl", keep=True, ttl=120)
    try:
        assert _deadline(db_conn, _agent_row, handle.session_id) is not None
    finally:
        with contextlib.suppress(ValueError, RuntimeError):
            ava.shell.sessions.kill(handle.session_id)


@pytest.mark.parametrize("ttl", [0.0, -1.0, float("inf"), float("nan"), 86400.1, 1e9])
def test_sessions_new_rejects_invalid_ttl(ttl: float) -> None:
    with pytest.raises(ValueError, match="ttl"):
        ava.shell.sessions.new("test-invalid", ttl=ttl)


@pytest.mark.parametrize("ttl", [0.0, -1.0, float("inf"), float("nan"), 86400.1, 1e9])
def test_run_background_rejects_invalid_ttl(ttl: float) -> None:
    with pytest.raises(ValueError, match="ttl"):
        ava.shell.run_background("true", name="test-invalid", ttl=ttl)


def test_validate_ttl_accepts_exactly_max() -> None:
    """The cap is inclusive: exactly 86400s (24h) is allowed (ruling 2026-09-01)."""
    from ava.shell.sessions import _validate_ttl

    assert _validate_ttl(86400.0) == 86400.0
    with pytest.raises(ValueError, match="ttl"):
        _validate_ttl(86400.001)


# ─────────────── sessions.renew — explicit TTL renewal (task #2647) ───────────────


def test_renew_extends_deadline_from_now(db_conn: psycopg.Connection, _agent_row: int) -> None:
    """Deadline = now + ttl, never stacked on the old deadline; the audit row
    carries the before/after pair and the main row's counter ticks."""
    session_id = ava.shell.sessions.new("test-renew", ttl=120)
    try:
        prev = _deadline(db_conn, _agent_row, session_id)
        assert prev is not None
        before = datetime.now(UTC)
        new_deadline = ava.shell.sessions.renew(session_id, ttl=120)
        assert (
            before + timedelta(seconds=119)
            <= new_deadline
            <= datetime.now(UTC) + timedelta(seconds=121)
        )
        after = _deadline(db_conn, _agent_row, session_id)
        assert after is not None
        assert after == new_deadline
        assert after > prev  # from now, not stacked: deadline moved forward
        renewals, last_renewed = _facts(db_conn, _agent_row, session_id)
        assert renewals == 1
        assert last_renewed is not None
        rows = _renewal_rows(db_conn, _agent_row, session_id)
        assert len(rows) == 1
        requested, prev_exp, new_exp = rows[0]
        assert requested == 120
        assert prev_exp == prev
        assert new_exp == new_deadline
    finally:
        with contextlib.suppress(ValueError, RuntimeError):
            ava.shell.sessions.kill(session_id)


def test_renew_requires_ttl() -> None:
    """Renewal is an explicit action and ttl is mandatory (user ruling
    2026-09-08): omitting it is a TypeError."""
    with pytest.raises(TypeError):
        ava.shell.sessions.renew(1)  # type: ignore[call-arg]


@pytest.mark.parametrize("ttl", [0.0, -1.0, float("inf"), float("nan"), 86400.1, 1e9])
def test_renew_rejects_invalid_ttl(ttl: float) -> None:
    with pytest.raises(ValueError, match="ttl"):
        ava.shell.sessions.renew(1, ttl=ttl)


def test_renew_rejects_unknown_session() -> None:
    """A session id that is not this agent's (or not alive) is a ValueError,
    same rule as send/capture."""
    with pytest.raises(ValueError, match="session"):
        ava.shell.sessions.renew(999_999, ttl=120)


def test_renew_rejects_watcher_session(db_conn: psycopg.Connection, _agent_row: int) -> None:
    """A watcher's lifetime is governed by the watcher registry/timeout, not
    the shell TTL — renewing its row would pretend to extend nothing."""
    session_id = ava.shell.sessions.new("test-renew-watcher", ttl=120)
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_watchers (agent_id, session_id, kind, name) "
                "VALUES (%s, %s, 'cron', 'test-watcher')",
                (_agent_row, session_id),
            )
        db_conn.commit()
        with pytest.raises(ValueError, match="watcher"):
            ava.shell.sessions.renew(session_id, ttl=120)
        renewals, _ = _facts(db_conn, _agent_row, session_id)
        assert renewals == 0
        assert _renewal_rows(db_conn, _agent_row, session_id) == []
    finally:
        from shared.watcher_registry import delete_watcher

        delete_watcher(_agent_row, session_id)
        with contextlib.suppress(ValueError, RuntimeError):
            ava.shell.sessions.kill(session_id)


def test_renew_rejects_expired_row(db_conn: psycopg.Connection, _agent_row: int) -> None:
    """TTL expiry = immediate reclamation; there is no renewable expired state
    (user ruling 2026-09-08) — an already-passed deadline rejects the call."""
    session_id = ava.shell.sessions.new("test-renew-expired", ttl=120)
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_shell_ttls SET expires_at = now() - interval '1 second' "
                "WHERE agent_id = %s AND session_id = %s",
                (_agent_row, session_id),
            )
        db_conn.commit()
        with pytest.raises(ValueError, match="past its TTL"):
            ava.shell.sessions.renew(session_id, ttl=120)
        renewals, _ = _facts(db_conn, _agent_row, session_id)
        assert renewals == 0
        assert _renewal_rows(db_conn, _agent_row, session_id) == []
    finally:
        with contextlib.suppress(ValueError, RuntimeError):
            ava.shell.sessions.kill(session_id)


def test_renew_requires_ttl_row(db_conn: psycopg.Connection, _agent_row: int) -> None:
    """A pre-mandate session without a TTL row cannot be renewed — the reaper
    reclaims it via the launch-epoch fallback deadline."""
    session_id = ava.shell.sessions.new("test-renew-norow", ttl=120)
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM agent_shell_ttls WHERE agent_id = %s AND session_id = %s",
                (_agent_row, session_id),
            )
        db_conn.commit()
        with pytest.raises(RuntimeError, match="no TTL row"):
            ava.shell.sessions.renew(session_id, ttl=120)
    finally:
        with contextlib.suppress(ValueError, RuntimeError):
            ava.shell.sessions.kill(session_id)


def test_renew_audits_every_call(db_conn: psycopg.Connection, _agent_row: int) -> None:
    """Every renewal lands its own audit row; the chain's prev matches the
    prior new (before/after pair per call)."""
    session_id = ava.shell.sessions.new("test-renew-multi", ttl=120)
    try:
        first = ava.shell.sessions.renew(session_id, ttl=60)
        second = ava.shell.sessions.renew(session_id, ttl=90)
        renewals, last_renewed = _facts(db_conn, _agent_row, session_id)
        assert renewals == 2
        assert last_renewed is not None
        rows = _renewal_rows(db_conn, _agent_row, session_id)
        assert [r[0] for r in rows] == [60.0, 90.0]
        assert rows[0][2] == first
        assert rows[1][1] == first  # second call's prev = first call's new
        assert rows[1][2] == second
    finally:
        with contextlib.suppress(ValueError, RuntimeError):
            ava.shell.sessions.kill(session_id)


def test_renewal_guard_evaluates_at_statement_time_not_transaction_start(
    db_conn: psycopg.Connection, _agent_row: int
) -> None:
    """Issue #2053: a renewal whose transaction began before the deadline must
    still lose when its UPDATE executes after the deadline.

    The guard compares against clock_timestamp() (statement time, after the
    row lock), not now() (transaction start). With now() the guarded UPDATE
    would renew a row the reaper may already have claimed and killed.
    """
    import time

    from ava.shell import sessions

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at, created_at) "
            "VALUES (%s, %s, clock_timestamp() + interval '1 second', clock_timestamp()) "
            "ON CONFLICT (agent_id, session_id) DO UPDATE SET expires_at = EXCLUDED.expires_at",
            (_agent_row, 301),
        )
    db_conn.commit()
    with db_conn.cursor() as cur:
        # The transaction starts before the deadline passes.
        cur.execute("SET TRANSACTION READ WRITE")
        time.sleep(2.0)
        assert sessions._renewal_update(cur, _agent_row, 301, 600.0) is None
    db_conn.rollback()
    assert _facts(db_conn, _agent_row, 301) == (0, None)
