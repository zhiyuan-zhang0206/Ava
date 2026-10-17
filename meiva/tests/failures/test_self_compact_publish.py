from __future__ import annotations

import psycopg
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

import meiva
from base.agents.lifecycle import SystemHalt
from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.events.live.tests.fakes import patch_sync_redis
from base.lm.catalog import ModelCatalog
from base.telemetry import audit_events
from meiva.tests.fixtures.pin_agent import pin_agent


class _BoomSyncClient:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.publish_calls = 0

    def publish(self, _channel: str, _payload: str, *, auth_retry: bool) -> int:
        assert auth_retry is False
        self.publish_calls += 1
        raise self._exc

    def close(self) -> None:
        pass


def test_compact_survives_publish_failure(
    redis_url: str,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    pin_agent(
        spawn_agent(
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
            machine=machine_identity,
        )
    )

    client = _BoomSyncClient(RedisConnectionError("down"))
    patch_sync_redis(monkeypatch, lambda: client)

    with pytest.raises(SystemHalt):
        meiva.self.compact("Requests: (none)\nProgress: done\n")
    assert client.publish_calls == 1

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT kind FROM inbound_messages WHERE agent_id = %s ORDER BY id DESC LIMIT 1",
            (meiva.self.AGENT_ID,),
        )
        row = cur.fetchone()
    assert row is not None and row[0] == "compact_summary"


def test_compact_records_its_audit_fact_with_the_summary_inbound(
    redis_url: str,
    db_conn: psycopg.Connection,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    pin_agent(agent_id)

    with pytest.raises(SystemHalt):
        meiva.self.compact("Requests: (none)\nProgress: done\n")

    rows = db_conn.execute(
        "SELECT source, attributes->>'compact_kind' FROM audit_events "
        "WHERE agent_id = %s AND event_name = 'compact'",
        (agent_id,),
    ).fetchall()
    assert rows == [("self", "summary")]


def test_compact_whose_audit_fact_cannot_be_recorded_commits_no_summary(
    redis_url: str,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    pin_agent(agent_id)

    def refuse(_conn: object, _event: object) -> None:
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(audit_events, "record_audit", refuse)

    with pytest.raises(RuntimeError, match="audit write failed"):
        meiva.self.compact("Requests: (none)\nProgress: done\n")

    count = db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id = %s AND kind = 'compact_summary'",
        (agent_id,),
    ).fetchone()
    assert count == (0,)
