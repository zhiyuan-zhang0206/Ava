from uuid import uuid4

import psycopg
import pytest

import ava
from ava.tests.fixtures.pin_agent import pin_agent
from base.api_contracts.agent_contract import InvalidModelConfig

pytestmark = pytest.mark.usefixtures("sdk_model_owner")


def _inbound_rows(conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT content, kind, source FROM inbound_messages WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]


def _spawn_self(monkeypatch: pytest.MonkeyPatch) -> int:
    aid = ava.agents.spawn(idempotency_key=str(uuid4()))
    pin_agent(aid)
    return aid


class TestSelfTerminate:
    def test_terminate_inserts_inbound(
        self, db_conn: psycopg.Connection, gateway_client, monkeypatch: pytest.MonkeyPatch
    ):
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
        aid = _spawn_self(monkeypatch)
        with pytest.raises(ava.self.AgentRestart):
            ava.self.restart()
        rows = _inbound_rows(db_conn, aid)
        assert any(r[1] == "restart" and r[2] == "self" for r in rows)
