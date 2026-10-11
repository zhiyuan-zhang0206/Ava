"""Sending as the user versus as a declared external agent, from the CLI to the agent's context.

`user` reaches the agent as an ordinary user message. `external_agent:<subject>[:<instance>]`
reaches it as a message from that external agent: the agent reads who sent it, and the
timeline row carries the source so the UI can show the sender. Both pass the same gateway
credential check; neither declaration grants anything.
"""

from __future__ import annotations

import contextlib
from collections.abc import Generator
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import httpx2
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.graph.claim._chat_inbound import build_chat_inbound
from agent.ownership.hosted import admit_hosted_runtime, settle_hosted_runtime
from base.agents.messages.envelope import EnvelopeReadInputs
from base.clock import Clock
from base.cluster.machine import MachineIdentity
from base.config import settings
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from cli.commands.agents.control import cmd_agents_send
from gateway.app import app
from tests.fixtures.gateway_config import gateway_test_client
from tests.fixtures.unit.identity import machine_identity as machine_identity

_ENVELOPE_INPUTS = EnvelopeReadInputs(Clock.from_settings, lambda: False)
_SECRET = "external-agent-source-secret"  # noqa: S105 -- isolated test credential
_EXTERNAL = "external_agent:claude_code:run-7"


async def _admit(
    db: psycopg.Connection, pool: AsyncConnectionPool, database: Database
) -> RuntimeIncarnation:
    """A real hosted admission: the target is a live agent whose claim path reads the inbound."""
    agent_id = create_agent(db)
    db.execute(
        "INSERT INTO agents_meta (id, status, machine) VALUES (%s, 'idling', 'host-test') "
        "ON CONFLICT (id) DO UPDATE SET status = 'idling', machine = 'host-test'",
        (agent_id,),
    )
    db.commit()
    incarnation = await admit_hosted_runtime(
        pool, agent_id, "host-test", uuid4(), expected_from="idling", db=database
    )
    assert incarnation is not None
    assert await settle_hosted_runtime(
        pool, incarnation, bus=EventBus.from_settings(), resources=None
    )
    return incarnation


@contextlib.contextmanager
def authenticated_gateway(
    monkeypatch: pytest.MonkeyPatch, *, machine: MachineIdentity
) -> Generator[TestClient]:
    """The CLI's `send` posts through a real gateway app with the cluster credential on."""
    monkeypatch.setattr(settings.data_plane, "cluster_secret", _SECRET)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(
        "base.cluster.machine.gateway_api_base", Mock(return_value="http://testserver")
    )
    monkeypatch.setattr(
        "base.cluster.machine.gateway_auth_headers",
        Mock(return_value={"Authorization": f"Bearer {_SECRET}"}),
    )
    with gateway_test_client(app, machine=machine) as client:

        def post(url: str, **kwargs: Any) -> httpx2.Response:
            return client.post(url, **kwargs)

        monkeypatch.setattr("base.host.net.http_dial.post", post)
        yield client


async def _claimed_text(
    pool: AsyncConnectionPool, incarnation: RuntimeIncarnation
) -> tuple[str, str, dict[str, object] | None]:
    claimed = await claim_inbound_batch(
        pool, incarnation.agent_id, incarnation=incarnation, work=None
    )
    assert len(claimed) == 1
    item = claimed[0]
    message, _ = build_chat_inbound(
        item,
        read_security_enabled=lambda: settings.agent.security_scan_enabled,
        envelope_inputs=_ENVELOPE_INPUTS,
    )
    content = message.model_dump()["content"]
    assert isinstance(content, str)
    return item.source, content, item.payload


async def test_cli_send_as_user_reaches_the_agent_as_a_plain_user_message(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    incarnation = await _admit(db_conn, aops_pool, database)
    with authenticated_gateway(monkeypatch, machine=machine_identity):
        assert cmd_agents_send(incarnation.agent_id, "run the checks", "user") == 0
    source, content, payload = await _claimed_text(aops_pool, incarnation)
    assert source == "user"
    assert payload is None
    # The user envelope has no sender label: the message reads exactly as typed.
    assert content == "run the checks"


async def test_cli_send_as_external_agent_reaches_the_agent_as_that_agent(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    incarnation = await _admit(db_conn, aops_pool, database)
    with authenticated_gateway(monkeypatch, machine=machine_identity):
        assert cmd_agents_send(incarnation.agent_id, "I finished the refactor", _EXTERNAL) == 0
    source, content, payload = await _claimed_text(aops_pool, incarnation)
    assert source == _EXTERNAL
    assert payload == {
        "caller_identity": {"kind": "external_agent", "subject": "claude_code", "instance": "run-7"}
    }
    assert content == (
        "External agent (claude_code / run-7; asserted provenance):\n\nI finished the refactor"
    )


async def test_external_agent_message_is_listed_with_its_source_for_the_timeline(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """The UI shows the sender from the inbound's `source`, so the row must carry it."""
    incarnation = await _admit(db_conn, aops_pool, database)
    with authenticated_gateway(monkeypatch, machine=machine_identity):
        assert cmd_agents_send(incarnation.agent_id, "hello", _EXTERNAL) == 0
    row = db_conn.execute(
        "SELECT source, kind, source_verified_by, source_transport FROM inbound_messages "
        "WHERE agent_id = %s",
        (incarnation.agent_id,),
    ).fetchone()
    assert row == ("external_agent:claude_code:run-7", "chat", "cluster_bearer", "http")


@pytest.mark.parametrize(
    "source",
    [
        "claude_code",
        "external_agent:",
        "external_agent:a:b:c",
        "external_agent:bad name",
        "unknown:legacy",
    ],
)
def test_gateway_rejects_a_malformed_declaration_before_any_insert(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    *,
    machine_identity: MachineIdentity,
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (id, status, lease_expires_at) "
        "VALUES (%s, 'idling', now() + interval '5 minutes')",
        (agent_id,),
    )
    db_conn.commit()
    with authenticated_gateway(monkeypatch, machine=machine_identity) as client:
        response = client.post(
            f"/api/agents/{agent_id}/messages",
            headers={"Authorization": f"Bearer {_SECRET}"},
            json={"content": "hello", "source": source},
        )
    assert response.status_code == 422, response.text
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id = %s", (agent_id,)
    ).fetchone() == (0,)


async def test_an_unauthenticated_external_agent_declaration_is_refused(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """Declaring an identity is not a credential: the gateway check still applies."""
    incarnation = await _admit(db_conn, aops_pool, database)
    with authenticated_gateway(monkeypatch, machine=machine_identity) as client:
        response = client.post(
            f"/api/agents/{incarnation.agent_id}/messages",
            json={"content": "hello", "source": _EXTERNAL},
        )
    assert response.status_code == 401
