"""SDK ↔ Gateway integration tests.

These tests were originally in agent/graph/claim/tests/test_core.py and tests/components/ava/test_user.py,
requiring the Gateway process to be running — local `pytest tests/` would not pass.
After migrating to integration/, they use FastAPI TestClient for in-process communication.
"""

from uuid import uuid4

import httpx
import psycopg
import pytest

import ava
from base.agents import InvalidModelConfig
from base.config import settings
from tests.fixtures.pin_agent import pin_agent

pytestmark = pytest.mark.usefixtures("sdk_model_owner")


@pytest.fixture(autouse=True)
def _authenticated_sdk(monkeypatch: pytest.MonkeyPatch, gateway_client: httpx.Client) -> None:
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "sdk-core-secret")
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    gateway_client.headers["Authorization"] = "Bearer sdk-core-secret"


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
    aid = ava.agents.spawn(idempotency_key=str(uuid4()))
    pin_agent(aid)
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
    def test_rejects_incompatible_effort_without_overlay_or_inbound(
        self, db_conn: psycopg.Connection, gateway_client
    ) -> None:
        aid = ava.agents.spawn(
            config_overlay={"llm_model": "deepseek-flash"}, idempotency_key=str(uuid4())
        )
        pin_agent(aid)
        with pytest.raises(InvalidModelConfig, match="unsupported reasoning effort"):
            ava.self.restart(config_overlay={"reasoning_effort": "low"})
        assert db_conn.execute(
            "SELECT config_overlay FROM agents_meta WHERE id=%s", (aid,)
        ).fetchone() == ({"llm_model": "deepseek-flash"},)
        assert not _inbound_rows(db_conn, aid)

    def test_model_and_effort_change_commit_with_the_restart(
        self, db_conn: psycopg.Connection, gateway_client
    ) -> None:
        aid = ava.agents.spawn(
            config_overlay={"llm_model": "gpt-5.6-sol", "reasoning_effort": "low"},
            idempotency_key=str(uuid4()),
        )
        pin_agent(aid)
        overlay: dict[str, object] = {"llm_model": "deepseek-flash", "reasoning_effort": "max"}
        with pytest.raises(ava.self.AgentRestart):
            ava.self.restart(config_overlay=overlay)
        assert db_conn.execute(
            "SELECT config_overlay FROM agents_meta WHERE id=%s", (aid,)
        ).fetchone() == (overlay,)
        assert _inbound_rows(db_conn, aid) == [("", "restart", "self")]

    def test_restart_inserts_inbound(
        self, db_conn: psycopg.Connection, gateway_client, monkeypatch: pytest.MonkeyPatch
    ):
        """restart → Gateway writes restart inbound."""
        aid = _spawn_self(monkeypatch)
        with pytest.raises(ava.self.AgentRestart):
            ava.self.restart()
        rows = _inbound_rows(db_conn, aid)
        assert any(r[1] == "restart" and r[2] == "self" for r in rows)
