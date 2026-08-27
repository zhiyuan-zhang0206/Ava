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


def test_run_background_tracks_explicit_ttl(db_conn: psycopg.Connection, _agent_row: int) -> None:
    handle = ava.shell.run_background("sleep 60", name="test-bg-ttl", keep=True, ttl=120)
    try:
        assert _deadline(db_conn, _agent_row, handle.session_id) is not None
    finally:
        with contextlib.suppress(ValueError, RuntimeError):
            ava.shell.sessions.kill(handle.session_id)


@pytest.mark.parametrize("ttl", [0.0, -1.0, float("inf"), float("nan")])
def test_sessions_new_rejects_invalid_ttl(ttl: float) -> None:
    with pytest.raises(ValueError, match="ttl"):
        ava.shell.sessions.new("test-invalid", ttl=ttl)


@pytest.mark.parametrize("ttl", [0.0, -1.0, float("inf"), float("nan")])
def test_run_background_rejects_invalid_ttl(ttl: float) -> None:
    with pytest.raises(ValueError, match="ttl"):
        ava.shell.run_background("true", name="test-invalid", ttl=ttl)
