"""Real HTTP retry receipts, frozen identities and repeatable native wake fences."""

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID, uuid4

import httpx2 as httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from base.config import settings
from gateway.agents import launch_retry
from gateway.agents import router as birth_router
from gateway.app import app
from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
    set_machine_identity: Callable[..., None],
    db_conn: psycopg.Connection[Any],
) -> Iterator[TestClient]:
    set_machine_identity(role="agent-runner", name="local-test")
    db_conn.execute("INSERT INTO machines(name) VALUES ('local-test') ON CONFLICT DO NOTHING")
    db_conn.commit()
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "retry-test-secret")

    async def born(_db: object, _target: str, body: LaunchAgentRequest) -> SpawnedAgent:
        return SpawnedAgent(id=body.agent_id)

    async def reconcile(*_args: object, **_kwargs: object) -> dict[str, bool]:
        return {"wake_published": False}

    monkeypatch.setattr(birth_router, "forward_spawn_to_remote", born)
    monkeypatch.setattr(launch_retry.rpc, "dispatch_to_machine", reconcile)
    with TestClient(app, headers={"Authorization": "Bearer retry-test-secret"}) as value:
        yield value


def birth(client: TestClient) -> tuple[int, str]:
    result = client.post(
        "/api/agents", json={"machine": "local-test", "prompt": "once", "prompt_source": "user"}
    )
    assert result.status_code == 201, result.text
    agent_id = result.json()["id"]
    prior = client.get(f"/api/agents/{agent_id}").json()["last_launch_attempt_id"]
    assert UUID(prior)
    return agent_id, prior


def submit(client: TestClient, agent_id: int, prior: str, key: str = "operation") -> httpx.Response:
    return client.post(
        f"/api/keyed/v1/agents/{agent_id}/retry-launch",
        json={"expected_prior_attempt_id": prior},
        headers={"Idempotency-Key": key, "Idempotency-Scope": "principal-v1"},
    )


def test_lost_response_concurrency_and_deliberate_new_attempt(
    client: TestClient, db_conn: psycopg.Connection[Any]
) -> None:
    agent_id, prior = birth(client)

    def retry(_index: int) -> httpx.Response:
        return submit(client, agent_id, prior)

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(retry, range(4)))
    assert all(r.status_code == 200 for r in responses)
    first = responses[0].json()
    assert all(r.json() == first for r in responses)
    attempt = first["launch_attempt_id"]
    assert attempt != prior
    assert db_conn.execute("SELECT count(*) FROM agent_launch_retry_receipts").fetchone() == (1,)
    assert submit(client, agent_id, attempt).status_code == 409
    assert submit(client, agent_id, prior, "deliberate-new").status_code == 409
    next_response = submit(client, agent_id, attempt, "deliberate-new")
    assert next_response.status_code == 200
    assert next_response.json()["launch_attempt_id"] != attempt
    assert submit(client, agent_id, prior).json() == first
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent_id,)
    ).fetchone() == (1,)


def test_replay_survives_mutable_config_admission_and_target_deletion(
    client: TestClient, db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id, prior = birth(client)
    accepted = submit(client, agent_id, prior).json()
    monkeypatch.setattr(settings.lm, "llm_model", "changed-after-acceptance")
    snapshot = db_conn.execute(
        "SELECT config_overlay, birth_config FROM agent_launch_retry_receipts"
    ).fetchone()
    db_conn.execute(
        "UPDATE agents_meta SET config_overlay='{\"llm_model\":\"changed\"}', last_admission_at=now(), last_admission_outcome='admitted' WHERE id=%s",
        (agent_id,),
    )
    db_conn.commit()
    assert submit(client, agent_id, prior).json() == accepted
    assert (
        db_conn.execute(
            "SELECT config_overlay, birth_config FROM agent_launch_retry_receipts"
        ).fetchone()
        == snapshot
    )
    db_conn.execute("DELETE FROM agents_meta WHERE id=%s", (agent_id,))
    db_conn.commit()
    assert submit(client, agent_id, prior).json() == accepted
    assert submit(client, agent_id, accepted["launch_attempt_id"], "fresh").status_code == 404


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Idempotency-Key": ""},
        {"Idempotency-Key": "x"},
        {"Idempotency-Key": "x", "Idempotency-Scope": "unknown"},
        {"Idempotency-Key": "x" * 129, "Idempotency-Scope": "principal-v1"},
    ],
)
def test_invalid_guard_has_no_receipt_or_pointer_effect(
    client: TestClient, db_conn: psycopg.Connection[Any], headers: dict[str, str]
) -> None:
    agent_id, prior = birth(client)
    response = client.post(
        f"/api/keyed/v1/agents/{agent_id}/retry-launch",
        json={"expected_prior_attempt_id": prior},
        headers=headers,
    )
    assert response.status_code == 400
    assert db_conn.execute(
        "SELECT last_launch_attempt_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (UUID(prior),)
    assert db_conn.execute("SELECT count(*) FROM agent_launch_retry_receipts").fetchone() == (0,)


def test_bad_body_and_revoked_auth_do_not_execute(
    client: TestClient, db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id, prior = birth(client)
    response = client.post(
        f"/api/keyed/v1/agents/{agent_id}/retry-launch",
        json={"expected_prior_attempt_id": "wrong"},
        headers={"Idempotency-Key": "op", "Idempotency-Scope": "principal-v1"},
    )
    assert response.status_code == 422
    accepted = submit(client, agent_id, prior).json()
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "rotated")
    assert submit(client, agent_id, prior).status_code == 401
    assert db_conn.execute(
        "SELECT last_launch_attempt_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (UUID(accepted["launch_attempt_id"]),)


def test_unsupported_old_runner_has_no_fallback_and_keeps_acceptance(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    async def old(_db: object, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        raise launch_retry.rpc.ClusterOpFailed({"error": "unknown kind"})

    monkeypatch.setattr(launch_retry.rpc, "dispatch_to_machine", old)
    agent_id, prior = birth(client)
    first = submit(client, agent_id, prior)
    assert first.status_code == 200, first.text
    assert submit(client, agent_id, prior).json() == first.json()
    assert [c["kind"] for c in calls] == ["launch-reconcile-v1", "launch-reconcile-v1"]


@pytest.mark.parametrize("changed", ["admitted", "terminated", "new-attempt", "machine", "deleted"])
def test_native_repeat_rejects_superseded_target(
    client: TestClient,
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    from ops.lifecycle import launch_reconcile
    from ops.rpc_schemas.launch_retry import LaunchReconcileRequest

    agent_id, prior = birth(client)
    accepted = submit(client, agent_id, prior).json()
    wakes: list[tuple[object, ...]] = []

    def wake(*args: object) -> bool:
        wakes.append(args)
        return True

    monkeypatch.setattr(launch_reconcile, "publish_inbound_wake", wake)
    payload = LaunchReconcileRequest(launch_attempt_id=accepted["launch_attempt_id"])
    assert launch_reconcile._reconcile(
        app.state.db, app.state.bus, payload, app.state.db_pool
    ).wake_published
    assert launch_reconcile._reconcile(
        app.state.db, app.state.bus, payload, app.state.db_pool
    ).wake_published
    if changed == "admitted":
        db_conn.execute(
            "UPDATE agents_meta SET last_admission_at=now(),last_admission_outcome='admitted' WHERE id=%s",
            (agent_id,),
        )
    elif changed == "terminated":
        db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (agent_id,))
    elif changed == "new-attempt":
        submit(client, agent_id, accepted["launch_attempt_id"], "next")
    elif changed == "machine":
        db_conn.execute("UPDATE agents_meta SET machine='other' WHERE id=%s", (agent_id,))
    else:
        db_conn.execute("DELETE FROM agents_meta WHERE id=%s", (agent_id,))
    db_conn.commit()
    assert not launch_reconcile._reconcile(
        app.state.db, app.state.bus, payload, app.state.db_pool
    ).wake_published
    assert len(wakes) == 2
    count = db_conn.execute("SELECT count(*) FROM agent_launch_retry_receipts").fetchone()
    assert count is not None and count[0] >= 1


def test_native_crash_before_wake_and_result_ack_loss_preserve_attempt(
    client: TestClient, db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ops.lifecycle import launch_reconcile
    from ops.rpc_schemas.launch_retry import LaunchReconcileRequest

    agent_id, prior = birth(client)
    accepted = submit(client, agent_id, prior).json()
    payload = LaunchReconcileRequest(launch_attempt_id=accepted["launch_attempt_id"])

    def crash(*_args: object) -> bool:
        raise RuntimeError("native crash before publication")

    monkeypatch.setattr(launch_reconcile, "publish_inbound_wake", crash)
    with pytest.raises(RuntimeError, match="native crash"):
        launch_reconcile._reconcile(app.state.db, app.state.bus, payload, app.state.db_pool)
    wakes: list[tuple[object, ...]] = []

    def wake(*args: object) -> bool:
        wakes.append(args)
        return True

    monkeypatch.setattr(launch_reconcile, "publish_inbound_wake", wake)
    # Discard the successful result just as a lost RPC ACK; repeat the same
    # natural wake without introducing a second queue item or rotating identity.
    launch_reconcile._reconcile(app.state.db, app.state.bus, payload, app.state.db_pool)
    launch_reconcile._reconcile(app.state.db, app.state.bus, payload, app.state.db_pool)
    assert len(wakes) == 2
    assert db_conn.execute(
        "SELECT last_launch_attempt_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (payload.launch_attempt_id,)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent_id,)
    ).fetchone() == (1,)
    assert db_conn.execute(
        "SELECT count(*) FROM api_idempotency WHERE method='ops'"
    ).fetchone() == (0,)


def test_reconcile_holds_row_fence_until_publication(
    client: TestClient, db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Event

    from ops.lifecycle import launch_reconcile
    from ops.rpc_schemas.launch_retry import LaunchReconcileRequest

    agent_id, prior = birth(client)
    accepted = submit(client, agent_id, prior).json()
    payload = LaunchReconcileRequest(launch_attempt_id=accepted["launch_attempt_id"])
    publishing, release = Event(), Event()

    def wake(*_args: object) -> bool:
        publishing.set()
        assert release.wait(5)
        return True

    monkeypatch.setattr(launch_reconcile, "publish_inbound_wake", wake)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            launch_reconcile._reconcile, app.state.db, app.state.bus, payload, app.state.db_pool
        )
        try:
            assert publishing.wait(5)
            db_conn.execute("SET LOCAL lock_timeout='100ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                db_conn.execute(
                    "UPDATE agents_meta SET status='terminated' WHERE id=%s", (agent_id,)
                )
            db_conn.rollback()
        finally:
            release.set()
        assert pending.result(timeout=5).wake_published


def test_unverified_principal_guard_when_auth_disabled(
    client: TestClient, db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id, prior = birth(client)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", False)
    assert submit(client, agent_id, prior).status_code == 400
    assert db_conn.execute("SELECT count(*) FROM agent_launch_retry_receipts").fetchone() == (0,)


def test_old_http_router_has_no_guarded_retry_endpoint(db_conn: psycopg.Connection[Any]) -> None:
    from fastapi import FastAPI

    old = FastAPI()
    old.include_router(birth_router.router)
    with TestClient(old) as older:
        response = older.post(
            "/api/keyed/v1/agents/42/retry-launch",
            json={"expected_prior_attempt_id": str(uuid4())},
            headers={"Idempotency-Key": "op", "Idempotency-Scope": "principal-v1"},
        )
    assert response.status_code == 404
    assert db_conn.execute("SELECT count(*) FROM agent_launch_retry_receipts").fetchone() == (0,)


def test_receipt_insert_and_pointer_rotation_roll_back_together(
    client: TestClient, db_conn: psycopg.Connection[Any]
) -> None:
    agent_id, prior = birth(client)
    db_conn.execute("""
        CREATE FUNCTION pg_temp.reject_retry_rotation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.last_launch_attempt_id IS DISTINCT FROM OLD.last_launch_attempt_id THEN
                RAISE EXCEPTION 'injected pointer write failure';
            END IF;
            RETURN NEW;
        END $$
    """)
    db_conn.execute(
        "CREATE TRIGGER reject_retry_rotation BEFORE UPDATE ON agents_meta FOR EACH ROW EXECUTE FUNCTION pg_temp.reject_retry_rotation()"
    )
    db_conn.commit()
    try:
        with pytest.raises(psycopg.errors.RaiseException, match="injected pointer"):
            submit(client, agent_id, prior)
    finally:
        db_conn.execute("DROP TRIGGER reject_retry_rotation ON agents_meta")
        db_conn.commit()
    assert db_conn.execute("SELECT count(*) FROM agent_launch_retry_receipts").fetchone() == (0,)
    assert db_conn.execute(
        "SELECT last_launch_attempt_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (UUID(prior),)
    assert submit(client, agent_id, prior).status_code == 200


def test_distinct_concurrent_intents_cannot_reuse_the_same_observation(
    client: TestClient, db_conn: psycopg.Connection[Any]
) -> None:
    agent_id, prior = birth(client)

    def submit_intent(index: int) -> httpx.Response:
        return submit(client, agent_id, prior, f"intent-{index}")

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(submit_intent, range(4)))
    assert sorted(result.status_code for result in results) == [200, 409, 409, 409]
    assert db_conn.execute("SELECT count(*) FROM agent_launch_retry_receipts").fetchone() == (1,)
