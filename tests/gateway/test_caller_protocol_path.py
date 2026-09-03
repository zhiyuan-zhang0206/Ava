"""Real CLI -> authenticated HTTP -> ownership transaction -> hosted claim proof.

The only simulated rollout fact is an explicitly completed old-writer barrier.
Ownership is obtained using the actual hosted admission helper, never invented
by a mocked lookup. Production admission still advertises protocol zero.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, LiteralString
from unittest.mock import Mock
from uuid import uuid4

import httpx2
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.graph._chat_inbound import build_chat_inbound
from agent.hosted_ownership import admit_hosted_runtime, settle_hosted_runtime
from cli.commands.agents import cmd_agents_send
from gateway.app import app
from shared.config import settings
from shared.db import create_agent
from shared.runtime_incarnation import RuntimeIncarnation
from shared.turn_identity import bind_turn_identity

_SOURCE = "external_agent:codex:run-42"
_CALLER = {"kind": "external_agent", "subject": "codex", "instance": "run-42"}


async def _admit(db: psycopg.Connection, pool: AsyncConnectionPool) -> RuntimeIncarnation:
    agent_id = create_agent(db)
    db.execute(
        "INSERT INTO agents_meta (id, status, machine) VALUES (%s, 'idling', 'host-test') "
        "ON CONFLICT (id) DO UPDATE SET status = 'idling', machine = 'host-test'",
        (agent_id,),
    )
    db.commit()
    incarnation = await admit_hosted_runtime(
        pool, agent_id, "host-test", uuid4(), expected_from="idling"
    )
    assert incarnation is not None
    assert await settle_hosted_runtime(pool, incarnation)
    return incarnation


def _after_proven_old_writer_barrier(
    db: psycopg.Connection, incarnation: RuntimeIncarnation
) -> None:
    """CI-only activation precondition, not a production switch or API.

    This isolated test has no old executables or unconditional writers. Its
    only consumer below is this version's actual hosted claim/envelope code.
    Production must independently prove the corresponding fleet barrier.
    """
    row = db.execute(
        "UPDATE agents_meta SET runtime_protocol_version = 1 "
        "WHERE id = %s AND runtime_generation = %s AND runtime_owner = %s "
        "AND runtime_kind = 'hosted' AND lease_expires_at > clock_timestamp() RETURNING id",
        (incarnation.agent_id, incarnation.generation, incarnation.owner),
    ).fetchone()
    assert row == (incarnation.agent_id,)
    db.commit()


async def test_profile_through_auth_gate_and_real_hosted_claim(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incarnation = await _admit(db_conn, aops_pool)
    _after_proven_old_writer_barrier(db_conn, incarnation)
    secret = "caller-path-test-secret"  # noqa: S105 — isolated test credential
    monkeypatch.setattr(settings.data_plane, "cluster_secret", secret)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setenv("AVA_CALLER_IDENTITY", json.dumps(_CALLER))
    monkeypatch.setenv("AVA_AGENT_ID", "999")
    monkeypatch.setattr("shared.machine.gateway_api_base", Mock(return_value="http://testserver"))
    monkeypatch.setattr(
        "shared.machine.gateway_auth_headers",
        Mock(return_value={"Authorization": f"Bearer {secret}"}),
    )
    with TestClient(app) as client:

        def post(url: str, **kwargs: Any) -> httpx2.Response:
            return client.post(url, **kwargs)

        monkeypatch.setattr("shared.http_dial.post", post)
        assert cmd_agents_send(incarnation.agent_id, "caller path proof", None) == 0
        # Unauthenticated traffic cannot reach the now-capable target either.
        assert (
            client.post(
                f"/api/agents/{incarnation.agent_id}/messages",
                json={"source": _SOURCE, "content": "not authorized"},
            ).status_code
            == 401
        )
    with bind_turn_identity(incarnation.agent_id, incarnation=incarnation):
        claimed = await claim_inbound_batch(aops_pool, incarnation.agent_id)
        assert len(claimed) == 1
        item = claimed[0]
        assert item.source == _SOURCE
        assert item.payload == {"caller_identity": _CALLER}
        message = build_chat_inbound(item)
    content = message.model_dump()["content"]
    assert isinstance(content, str)
    assert "External agent" in content and "codex" in content
    assert "asserted" in content
    assert "User [" not in content and "[system]" not in content


@pytest.mark.parametrize(
    "invalid", ["legacy", "missing_owner", "missing_generation", "expired", "terminated"]
)
async def test_unready_target_rejects_before_insert(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    invalid: str,
) -> None:
    incarnation = await _admit(db_conn, aops_pool)
    if invalid != "legacy":
        _after_proven_old_writer_barrier(db_conn, incarnation)
    mutations: dict[str, LiteralString] = {
        "legacy": "runtime_protocol_version = 0",
        "missing_owner": "runtime_owner = NULL",
        "missing_generation": "runtime_generation = NULL",
        "expired": "lease_expires_at = clock_timestamp() - interval '1 second'",
        "terminated": "status = 'terminated'",
    }
    from psycopg import sql

    db_conn.execute(
        sql.SQL("UPDATE agents_meta SET {} WHERE id = %s").format(sql.SQL(mutations[invalid])),
        (incarnation.agent_id,),
    )
    db_conn.commit()
    with TestClient(app) as client:
        response = client.post(
            f"/api/agents/{incarnation.agent_id}/messages",
            json={"content": "must not insert", "source": _SOURCE},
        )
    assert response.status_code == 422, response.text
    assert "target runtime protocol" in response.text
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id = %s", (incarnation.agent_id,)
    ).fetchone() == (0,)


async def test_gate_holds_owner_lock_until_transaction_ends(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    from shared.caller_protocol import require_caller_protocol

    incarnation = await _admit(db_conn, aops_pool)
    _after_proven_old_writer_barrier(db_conn, incarnation)
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as other:
        with db_conn.transaction():
            assert require_caller_protocol(db_conn, incarnation.agent_id, _SOURCE) == incarnation
            other.execute("SET lock_timeout = '50ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                other.execute(
                    "UPDATE agents_meta SET runtime_owner = %s WHERE id = %s",
                    (uuid4(), incarnation.agent_id),
                )
        # A gate without a surrounding INSERT transaction would release the lock
        # immediately on an autocommit connection and admit stale ownership.
        with pytest.raises(RuntimeError, match="INSERT transaction"):
            require_caller_protocol(other, incarnation.agent_id, _SOURCE)


async def test_lease_expiring_while_waiting_for_unchanged_row_lock_is_rejected(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    from shared.caller_protocol import CallerProtocolUnavailableError, require_caller_protocol

    incarnation = await _admit(db_conn, aops_pool)
    _after_proven_old_writer_barrier(db_conn, incarnation)
    db_conn.execute(
        "UPDATE agents_meta SET lease_expires_at = clock_timestamp() + interval '1 second' "
        "WHERE id = %s",
        (incarnation.agent_id,),
    )
    db_conn.commit()
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as waiter:

        def gate() -> None:
            with waiter.transaction():
                require_caller_protocol(waiter, incarnation.agent_id, _SOURCE)

        with ThreadPoolExecutor(max_workers=1) as executor:
            with db_conn.transaction():
                db_conn.execute(
                    "SELECT id FROM agents_meta WHERE id = %s FOR UPDATE", (incarnation.agent_id,)
                )
                future = executor.submit(gate)
                for _ in range(200):
                    blocked = db_conn.execute(
                        "SELECT cardinality(pg_blocking_pids(%s)) > 0", (waiter.info.backend_pid,)
                    ).fetchone()
                    expired = db_conn.execute(
                        "SELECT lease_expires_at <= clock_timestamp() FROM agents_meta WHERE id = %s",
                        (incarnation.agent_id,),
                    ).fetchone()
                    if blocked == (True,) and expired == (True,):
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("gate did not block through lease expiry")
            # The locker commits without changing the tuple: the gate must
            # re-sample the clock, not rely on PostgreSQL update rechecks.
            with pytest.raises(CallerProtocolUnavailableError):
                future.result(timeout=5)
