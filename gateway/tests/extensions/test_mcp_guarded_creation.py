"""Guarded MCP creation uses the existing immutable birth transaction."""

from concurrent.futures import ThreadPoolExecutor
from typing import Any, LiteralString
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from gateway.agents import router
from gateway.app import app
from gateway.tests.extensions.test_mcp_endpoint import (
    _ACCEPT,
    _create_token,
    _tool_call,
    _tool_result,
)
from gateway.tests.extensions.test_mcp_endpoint import (
    _enable_endpoint as _enable_endpoint,
)

TOOL = "spawn_agent_guarded_v1"
ARGS = {"prompt": "one original goal", "idempotency_key": "original"}


def test_response_loss_replays_one_original_birth(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    create = router.create_and_launch_agent

    async def lose_response(*args: Any, **kwargs: Any) -> Any:
        await create(*args, **kwargs)
        raise RuntimeError("committed MCP response lost")

    monkeypatch.setattr(router, "create_and_launch_agent", lose_response)
    with TestClient(app) as client:
        token = _create_token(client)
        lost = _tool_call(client, token, TOOL, ARGS)
        assert lost["result"].get("isError") is True
        original = db_conn.execute("SELECT id FROM agents").fetchone()
        assert original is not None
        monkeypatch.setattr(router, "create_and_launch_agent", create)
        replay = _tool_result(_tool_call(client, token, TOOL, ARGS))
    assert replay["id"] == original[0]
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM agent_creation_snapshots").fetchone() == (1,)


@pytest.mark.parametrize("mutation", ["admitted", "terminated", "deleted", "placed", "rotated"])
def test_historical_replay_never_wakes_successor_work(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    statements: dict[str, LiteralString] = {
        "admitted": "UPDATE agents_meta SET last_admission_at=now(), last_admission_outcome='admitted' WHERE id=%s",
        "terminated": "UPDATE agents_meta SET status='terminated' WHERE id=%s",
        "deleted": "DELETE FROM agents_meta WHERE id=%s",
        "placed": "UPDATE agents_meta SET machine='later-placement' WHERE id=%s",
    }
    with TestClient(app) as client:
        token = _create_token(client)
        original = _tool_result(_tool_call(client, token, TOOL, ARGS))
        agent_id = original["id"]
        if mutation == "rotated":
            db_conn.execute(
                "UPDATE agents_meta SET last_launch_attempt_id=%s WHERE id=%s", (uuid4(), agent_id)
            )
        else:
            db_conn.execute(sql.SQL(statements[mutation]), (agent_id,))
        db_conn.commit()

        def no_preflight(*args: object, **kwargs: object) -> None:
            raise AssertionError("replay cannot repeat mutable preflight")

        async def no_launch(*args: object, **kwargs: object) -> None:
            raise AssertionError("historical birth cannot wake successor work")

        monkeypatch.setattr(router, "spawn_prechecks_blocking", no_preflight)
        monkeypatch.setattr(router, "forward_spawn_to_remote", no_launch)
        replay = _tool_result(_tool_call(client, token, TOOL, ARGS))
    assert replay["id"] == agent_id
    assert replay["accepted"] is True
    assert replay["execution_observed"] is False
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM agent_creation_snapshots").fetchone() == (1,)


def test_concurrent_retries_accept_one_agent_and_prompt(db_conn: psycopg.Connection) -> None:
    with TestClient(app) as client:
        token = _create_token(client)

        def submit(index: int) -> int:
            receipt = _tool_result(_tool_call(client, token, TOOL, ARGS, req_id=index + 10))
            return int(receipt["id"])

        with ThreadPoolExecutor(max_workers=3) as executor:
            ids = list(executor.map(submit, range(3)))
    assert len(set(ids)) == 1
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM agent_creation_snapshots").fetchone() == (1,)


@pytest.mark.parametrize(
    "args",
    [{"prompt": "goal"}, {**ARGS, "idempotency_key": ""}, {**ARGS, "idempotency_key": "x" * 129}],
)
def test_missing_or_invalid_key_is_rejected_before_birth(
    db_conn: psycopg.Connection, args: dict[str, str]
) -> None:
    with TestClient(app) as client:
        token = _create_token(client)
        response = _tool_call(client, token, TOOL, args)
    assert response["result"].get("isError") is True
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (0,)


def test_keys_are_scoped_to_tool_and_verified_client(db_conn: psycopg.Connection) -> None:
    with TestClient(app) as client:
        first = _create_token(client, name="first")
        second = _create_token(client, name="second")
        guarded = _tool_result(_tool_call(client, first, TOOL, ARGS))
        replay = _tool_result(_tool_call(client, first, TOOL, ARGS))
        assert replay["id"] == guarded["id"]
        changed = _tool_call(client, first, TOOL, {**ARGS, "prompt": "different intent"})
        assert changed["result"].get("isError") is True
        other = _tool_result(_tool_call(client, second, TOOL, ARGS))
        legacy = _tool_result(_tool_call(client, first, "spawn_agent", ARGS))
    assert len({guarded["id"], other["id"], legacy["id"]}) == 3
    assert db_conn.execute("SELECT count(*) FROM agent_creation_snapshots").fetchone() == (2,)


def test_read_scope_cannot_create_or_recover_a_birth(db_conn: psycopg.Connection) -> None:
    with TestClient(app) as client:
        token = _create_token(client, scope="read")
        response = _tool_call(client, token, TOOL, ARGS)
    assert response["result"].get("isError") is True
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (0,)


@pytest.mark.parametrize(
    "field", ["source", "instance", "caller_identity", "auth_principal", "client_id"]
)
def test_identity_arguments_cannot_override_verified_client(
    db_conn: psycopg.Connection, field: str
) -> None:
    with TestClient(app) as client:
        token = _create_token(client)
        response = _tool_call(client, token, TOOL, {**ARGS, field: "pretend-client"})
    assert "error" in response
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (0,)


def test_missing_snapshot_fails_closed_without_repeating_preflight(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as client:
        token = _create_token(client)
        original = _tool_result(_tool_call(client, token, TOOL, ARGS))
        db_conn.execute("DELETE FROM agent_creation_snapshots")
        db_conn.commit()

        def no_preflight(*args: object, **kwargs: object) -> None:
            raise AssertionError("missing snapshot must not authorize another birth")

        monkeypatch.setattr(router, "spawn_prechecks_blocking", no_preflight)
        response = _tool_call(client, token, TOOL, ARGS)
    assert response["result"].get("isError") is True
    assert "snapshot is unavailable" in _tool_result(response)
    assert db_conn.execute("SELECT id FROM agents").fetchall() == [(original["id"],)]
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)


def test_revoked_credential_cannot_recover_original_birth(db_conn: psycopg.Connection) -> None:
    with TestClient(app) as client:
        credential = client.post(
            "/api/mcp/clients", json={"name": "revoked", "scope": "write"}
        ).json()
        original = _tool_result(_tool_call(client, credential["token"], TOOL, ARGS))
        assert client.post(f"/api/mcp/clients/{credential['id']}/revoke").status_code == 200
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": TOOL, "arguments": ARGS},
            },
            headers={"Accept": _ACCEPT, "Authorization": f"Bearer {credential['token']}"},
        )
    assert response.status_code == 401
    assert db_conn.execute("SELECT id FROM agents").fetchall() == [(original["id"],)]
    assert db_conn.execute("SELECT count(*) FROM agent_creation_snapshots").fetchone() == (1,)
