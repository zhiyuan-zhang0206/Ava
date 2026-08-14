"""SDK ↔ Gateway integration tests.

These tests were originally in tests/ava/test_core.py and tests/ava/test_user.py,
requiring the Gateway process to be running — local `pytest tests/` would not pass.
After migrating to integration/, they use FastAPI TestClient for in-process communication.
"""

import psycopg
import pytest

import ava


def _inbound_rows(conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str]]:
    """Return (content, kind, source) list — ordered by id."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT content, kind, source FROM inbound_messages WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]


def _spawn_self(monkeypatch: pytest.MonkeyPatch) -> int:
    """Spawn an agent via the gateway and make it THIS process's self.

    Returns the spawned agent_id and points `ava.self.AGENT_ID` at it, so a
    subsequent `ava.self.terminate()/restart()` writes an inbound for an
    agent_id that actually exists. The previous code assumed the spawned id
    equalled the conftest-fixed AGENT_ID (1); when that assumption did not hold
    the inbound insert hit `inbound_messages_agent_id_fkey` (a CI-only flake).
    `_launch_agent_process` is stubbed — the test does not start a real
    subprocess.
    """
    aid = ava.agents.spawn()
    monkeypatch.setattr(ava._boot, "_agent_id", aid)
    return aid


class TestSelfTerminate:
    def test_terminate_inserts_inbound(
        self, db_conn: psycopg.Connection, gateway_client, monkeypatch: pytest.MonkeyPatch
    ):
        """terminate → Gateway writes terminate inbound."""
        aid = _spawn_self(monkeypatch)
        with pytest.raises(ava.self.AgentTermination):
            ava.self.terminate()
        rows = _inbound_rows(db_conn, aid)
        assert any(r[1] == "terminate" and r[2] == "self" for r in rows)


class TestSelfRestart:
    def test_restart_inserts_inbound(
        self, db_conn: psycopg.Connection, gateway_client, monkeypatch: pytest.MonkeyPatch
    ):
        """restart → Gateway writes restart inbound."""
        aid = _spawn_self(monkeypatch)
        with pytest.raises(ava.self.AgentRestart):
            ava.self.restart()
        rows = _inbound_rows(db_conn, aid)
        assert any(r[1] == "restart" and r[2] == "self" for r in rows)
