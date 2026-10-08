"""Guarded native cancel HTTP never upgrades an unsupported observed tuple."""

from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from agent.tests.claim.test_inbound_ownership import _insert
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.config import settings
from base.native_process.turn_identity import bind_turn_identity
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.runtime import TurnOutcome
from services.agent_runner.agent_host.settlement import close_hosted_turn
from services.agent_runner.agent_host.tests.native_cancel.helpers import managed_work
from services.agent_runner.agent_host.tests.native_cancel.test_return_boundaries import (
    _blocked_host,
)


def _headers(patch: pytest.MonkeyPatch) -> dict[str, str]:
    secret = "native-cancel-offline-secret"  # noqa: S105 — isolated fixture credential
    patch.setattr(settings.data_plane, "cluster_secret", secret)
    patch.setattr(settings.gateway, "auth_middleware_enabled", True)
    return {
        "Authorization": f"Bearer {secret}",
        "Idempotency-Scope": "principal-v1",
        "Idempotency-Key": "native-original",
    }


async def test_original_acceptance_replay_precedes_current_owner_and_work(
    client: TestClient,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _inc, target = await managed_work(db_conn, aops_pool)
    headers = _headers(monkeypatch)
    url = f"/api/keyed/v1/agents/{target.agent_id}"
    observed = client.get(f"{url}/native-work", headers=headers)
    assert observed.status_code == 200 and observed.json() == target.model_dump(mode="json")
    first = client.post(f"{url}/cancel-work", json=observed.json(), headers=headers)
    assert first.status_code == 200, first.text
    changed = observed.json() | {"owner": str(uuid4())}
    assert client.post(f"{url}/cancel-work", json=changed, headers=headers).status_code == 409
    assert (
        client.post(
            f"{url}/cancel-work",
            json=observed.json(),
            headers=headers | {"Idempotency-Key": "second-key"},
        ).status_code
        == 409
    )
    db_conn.execute(
        "UPDATE agents_meta SET native_work_id=NULL,runtime_owner=%s WHERE id=%s",
        (uuid4(), target.agent_id),
    )
    db_conn.execute("DELETE FROM native_graph_work WHERE id=%s", (target.work_id,))
    db_conn.commit()
    replay = client.post(f"{url}/cancel-work", json=observed.json(), headers=headers)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert client.get(f"{url}/native-work", headers=headers).status_code == 409
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (target.agent_id,)
    ).fetchone() == (0,)


@pytest.mark.parametrize("protocol", [None, True, 1.0, 2])
async def test_raw_protocol_is_required_and_lossless(
    client: TestClient,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    protocol: object,
) -> None:
    _inc, target = await managed_work(db_conn, aops_pool)
    headers = _headers(monkeypatch)
    body = target.model_dump(mode="json")
    if protocol is None:
        del body["protocol"]
    else:
        body["protocol"] = protocol
    response = client.post(
        f"/api/keyed/v1/agents/{target.agent_id}/cancel-work", json=body, headers=headers
    )
    assert response.status_code == 422, response.text
    assert db_conn.execute(
        "SELECT count(*) FROM native_cancel_commands WHERE agent_id=%s", (target.agent_id,)
    ).fetchone() == (0,)


@pytest.mark.parametrize("missing", ["Idempotency-Key", "Idempotency-Scope"])
async def test_guarded_cancel_rejects_unscoped_or_unkeyed_request(
    client: TestClient,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    _inc, target = await managed_work(db_conn, aops_pool)
    headers = _headers(monkeypatch)
    del headers[missing]
    response = client.post(
        f"/api/keyed/v1/agents/{target.agent_id}/cancel-work",
        json=target.model_dump(mode="json"),
        headers=headers,
    )
    assert response.status_code == 400
    assert db_conn.execute(
        "SELECT count(*) FROM native_cancel_commands WHERE agent_id=%s", (target.agent_id,)
    ).fetchone() == (0,)


async def test_preparing_and_managed_null_do_not_advertise_capability(
    client: TestClient,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _inc, target = await managed_work(db_conn, aops_pool, active=False)
    headers = _headers(monkeypatch)
    url = f"/api/keyed/v1/agents/{target.agent_id}"
    assert client.get(f"{url}/native-work", headers=headers).status_code == 409
    db_conn.execute("UPDATE native_graph_work SET phase='active' WHERE id=%s", (target.work_id,))
    db_conn.execute(
        "UPDATE agents_meta SET incarnation_resources=NULL WHERE id=%s", (target.agent_id,)
    )
    db_conn.commit()
    assert client.get(f"{url}/native-work", headers=headers).status_code == 409
    assert (
        client.post(
            f"{url}/cancel-work", json=target.model_dump(mode="json"), headers=headers
        ).status_code
        == 409
    )


async def test_crashed_host_idle_active_work_is_not_new_cancel_eligible(
    client: TestClient,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incarnation, initial = await managed_work(db_conn, aops_pool)
    _insert(db_conn, initial.agent_id)

    async def unexpected(_state: object) -> None:
        raise RuntimeError("isolated unexpected graph failure")

    _graph, saver, host, context = await _blocked_host(aops_pool, unexpected)
    with (
        bind_turn_identity(initial.agent_id, incarnation=incarnation),
        pytest.raises(RuntimeError, match="isolated unexpected graph failure"),
    ):
        await host._invoke_until_done(initial.agent_id, context)
    await close_hosted_turn(
        aops_pool,
        aops_pool,
        host._db,
        host._bus,
        saver,
        incarnation,
        TurnOutcome(exited=False, crashed=True),
    )
    row = db_conn.execute(
        "SELECT m.status,w.phase,w.id FROM agents_meta m "
        "JOIN native_graph_work w ON w.id=m.native_work_id WHERE m.id=%s",
        (initial.agent_id,),
    ).fetchone()
    assert row is not None and row[:2] == ("idling", "active")
    target = NativeWorkTarget(
        work_id=row[2],
        agent_id=initial.agent_id,
        machine=initial.machine,
        generation=incarnation.generation,
        owner=incarnation.owner,
        protocol=1,
    )
    headers = _headers(monkeypatch)
    url = f"/api/keyed/v1/agents/{initial.agent_id}"
    assert client.get(f"{url}/native-work", headers=headers).status_code == 409
    assert (
        client.post(
            f"{url}/cancel-work", json=target.model_dump(mode="json"), headers=headers
        ).status_code
        == 409
    )
    assert db_conn.execute(
        "SELECT count(*) FROM native_cancel_commands WHERE agent_id=%s", (initial.agent_id,)
    ).fetchone() == (0,)


@pytest.mark.parametrize("raw_agent", ["0", "-1", str(2**63), str(10**40), "true", "false"])
def test_native_paths_reject_invalid_agent_before_owner_lookup(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, raw_agent: str
) -> None:
    headers = _headers(monkeypatch)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("invalid path reached SQL or command acceptance")

    monkeypatch.setattr("gateway.agents.lifecycle.observe_native_work", forbidden)
    monkeypatch.setattr("gateway.agents.lifecycle.accept_native_cancel", forbidden)
    target = NativeWorkTarget(
        work_id=uuid4(),
        agent_id=1,
        machine="isolated-boundary",
        generation=uuid4(),
        owner=uuid4(),
        protocol=1,
    )
    url = f"/api/keyed/v1/agents/{raw_agent}"
    assert client.get(f"{url}/native-work", headers=headers).status_code == 422
    assert (
        client.post(
            f"{url}/cancel-work", json=target.model_dump(mode="json"), headers=headers
        ).status_code
        == 422
    )
