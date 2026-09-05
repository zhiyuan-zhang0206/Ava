"""Failure-event ingestion, delivery fallback, and deduplication."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx2
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool
from pydantic import SecretStr

from gateway.app import app
from gateway.routers import _delivery as delivery_router
from gateway.routers import work_failed as work_failed_router
from gateway.routers._delivery import ChatDelivery
from gateway.schemas.work_failed import WorkFailedIn, WorkFailedResult
from shared.agents import AgentStatus
from shared.config import settings

_WEBHOOK_TOKEN = "work-failed-webhook-token"  # noqa: S105 -- isolated test credential
_CLUSTER_SECRET = "work-failed-cluster-secret"  # noqa: S105 -- isolated test credential


def _seed_agent(
    conn: psycopg.Connection,
    *,
    status: AgentStatus,
    born_spawner: str = "user",
) -> int:
    row = conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    agent_id = int(row[0])
    lease = (
        datetime.now(UTC) + timedelta(minutes=5)
        if status in (AgentStatus.RUNNING, AgentStatus.IDLING)
        else None
    )
    conn.execute(
        "INSERT INTO agents_meta (id, spawner, born_spawner, status, lease_expires_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (agent_id, born_spawner, born_spawner, status.value, lease),
    )
    conn.commit()
    return agent_id


def _payload(agent_id: int, *, dedup_key: str) -> dict[str, object]:
    return {
        "repo": "Ava",
        "ref": "refs/heads/feature",
        "commit_sha": "a" * 40,
        "stage": "ci",
        "summary": "targeted tests failed",
        "dedup_key": dedup_key,
        "author_agent_id": agent_id,
    }


def _post(client: TestClient, payload: dict[str, object]) -> httpx2.Response:
    return client.post(
        "/api/work-failed",
        headers={"X-Alerts-Token": _WEBHOOK_TOKEN},
        json=payload,
    )


def _stored_failure_provenance(
    conn: psycopg.Connection,
    agent_id: int,
) -> tuple[str | None, str | None]:
    row = conn.execute(
        "SELECT source_verified_by, source_transport FROM inbound_messages "
        "WHERE agent_id = %s ORDER BY id DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    assert row is not None
    return row[0], row[1]


def _stored_event_delivery(
    conn: psycopg.Connection,
    dedup_key: str,
) -> tuple[int, str | None, str | None, bool]:
    row = conn.execute(
        "SELECT id, delivered_to, delivery_kind, delivered_at "
        "FROM work_failed_events WHERE dedup_key = %s",
        (dedup_key,),
    ).fetchone()
    assert row is not None
    return int(row[0]), row[1], row[2], row[3] is not None


def _seed_unfinished_failure(
    conn: psycopg.Connection,
    author_agent_id: int,
    *,
    dedup_key: str,
    age: timedelta,
    delivery_attempts: int = 0,
) -> tuple[int, WorkFailedIn]:
    body = WorkFailedIn.model_validate(_payload(author_agent_id, dedup_key=dedup_key))
    row = conn.execute(
        "INSERT INTO work_failed_events "
        "(repo, ref, commit_sha, stage, summary, author_agent_id, dedup_key, "
        "created_at, delivery_attempts) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, now() - %s::interval, %s) RETURNING id",
        (
            body.repo,
            body.ref,
            body.commit_sha,
            body.stage,
            body.summary,
            body.author_agent_id,
            body.dedup_key,
            f"{age.total_seconds()} seconds",
            delivery_attempts,
        ),
    ).fetchone()
    conn.commit()
    assert row is not None
    return int(row[0]), body


@pytest.fixture()
def failure_pool() -> Iterator[ConnectionPool]:
    import shared.db

    pool = shared.db.pool(max_size=4)
    yield pool
    pool.close()


@pytest.fixture(autouse=True)
def _webhook_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.alerts, "webhook_token", SecretStr(_WEBHOOK_TOKEN))


def test_alive_author_receives_failure_with_webhook_provenance(
    db_conn: psycopg.Connection,
) -> None:
    author = _seed_agent(db_conn, status=AgentStatus.IDLING)

    with TestClient(app) as client:
        response = _post(client, _payload(author, dedup_key="alive-author"))

    assert response.status_code == 200
    body = WorkFailedResult.model_validate(response.json())
    assert body.status == "delivered"
    assert body.delivered_to == f"agent:{author}"
    assert body.delivery_kind == "author"
    assert _stored_event_delivery(db_conn, "alive-author") == (
        body.event_id,
        f"agent:{author}",
        "author",
        True,
    )
    row = db_conn.execute(
        "SELECT content, source, source_verified_by, source_transport, content_hash "
        "FROM inbound_messages WHERE agent_id = %s AND kind = 'chat'",
        (author,),
    ).fetchone()
    assert row is not None
    assert row[1:4] == ("system", "webhook:work_failed", "http")
    assert row[4] == hashlib.sha256(row[0].encode()).hexdigest()


@pytest.mark.parametrize(
    ("field", "oversize"),
    [
        ("repo", "x" * 201),
        ("ref", "x" * 256),
        ("commit_sha", "x" * 65),
        ("summary", "x" * 2001),
        ("dedup_key", "x" * 256),
    ],
)
def test_work_failed_rejects_oversize_text_fields(
    db_conn: psycopg.Connection, field: str, oversize: str
) -> None:
    """Task #2531: unbounded webhook text could inject megabyte-scale content
    into agent chat / task descriptions. The schema bounds reject oversize
    payloads before any row or delivery effect."""
    author = _seed_agent(db_conn, status=AgentStatus.IDLING)

    with TestClient(app) as client:
        payload = _payload(author, dedup_key=f"oversize-{field}")
        payload[field] = oversize
        response = _post(client, payload)

    assert response.status_code == 422
    row = db_conn.execute(
        "SELECT COUNT(*) FROM work_failed_events WHERE dedup_key = %s",
        (f"oversize-{field}",),
    ).fetchone()
    assert row is not None and row[0] == 0


def test_cluster_bearer_is_accepted_and_recorded(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    author = _seed_agent(db_conn, status=AgentStatus.IDLING)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", _CLUSTER_SECRET)

    with TestClient(app) as client:
        response = client.post(
            "/api/work-failed",
            headers={"Authorization": f"Bearer {_CLUSTER_SECRET}"},
            json=_payload(author, dedup_key="cluster-bearer"),
        )

    assert response.status_code == 200
    assert _stored_failure_provenance(db_conn, author) == ("cluster_bearer", "http")


def test_terminated_author_successfully_resurrected_is_final_target(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    author = _seed_agent(db_conn, status=AgentStatus.TERMINATED)

    async def _resurrect(
        agent_id: int,
        **kwargs: object,
    ) -> AgentStatus:
        assert agent_id == author
        db_conn.execute(
            "UPDATE agents_meta SET status = 'idling', lease_expires_at = now() + interval '5 minutes' "
            "WHERE id = %s",
            (author,),
        )
        db_conn.commit()
        return AgentStatus.IDLING

    monkeypatch.setattr(delivery_router._ops, "resurrect_if_terminated", _resurrect)
    with TestClient(app) as client:
        response = _post(client, _payload(author, dedup_key="author-resurrected"))

    assert response.status_code == 200
    body = WorkFailedResult.model_validate(response.json())
    assert body.delivery_kind == "author_resurrected"
    assert body.delivered_to == f"agent:{author}"


def test_failed_resurrection_falls_back_to_nearest_live_delegator(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    grandparent = _seed_agent(db_conn, status=AgentStatus.RUNNING)
    parent = _seed_agent(
        db_conn,
        status=AgentStatus.IDLING,
        born_spawner=f"agent:{grandparent}",
    )
    author = _seed_agent(
        db_conn,
        status=AgentStatus.TERMINATED,
        born_spawner=f"agent:{parent}",
    )
    db_conn.execute(
        "UPDATE agents_meta SET spawner = %s WHERE id = %s",
        (f"agent:{grandparent}", author),
    )
    db_conn.commit()
    delivered: list[int] = []

    async def _delivery(_pool: object, agent_id: int, **kwargs: object) -> ChatDelivery:
        delivered.append(agent_id)
        return ChatDelivery(
            AgentStatus.TERMINATED if agent_id == author else AgentStatus.IDLING,
            len(delivered),
        )

    monkeypatch.setattr(work_failed_router, "deliver_chat_inbound", _delivery)
    with TestClient(app) as client:
        response = _post(client, _payload(author, dedup_key="delegator-fallback"))

    assert response.status_code == 200
    assert delivered == [author, parent]
    body = WorkFailedResult.model_validate(response.json())
    assert body.delivered_to == f"agent:{parent}"
    assert body.delivery_kind == "delegator"


def test_all_dead_creates_task_registry_alert(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = _seed_agent(db_conn, status=AgentStatus.TERMINATED)
    author = _seed_agent(
        db_conn,
        status=AgentStatus.TERMINATED,
        born_spawner=f"agent:{parent}",
    )
    db_conn.execute(
        "INSERT INTO agent_tasks (title, description, status, created_by, is_root) "
        "SELECT 'Root', 'root', 'ongoing', 'system', TRUE "
        "WHERE NOT EXISTS (SELECT 1 FROM agent_tasks WHERE is_root)"
    )
    db_conn.commit()

    async def _failed_delivery(*args: object, **kwargs: object) -> ChatDelivery:
        return ChatDelivery(AgentStatus.TERMINATED, 1)

    monkeypatch.setattr(work_failed_router, "deliver_chat_inbound", _failed_delivery)
    with TestClient(app) as client:
        response = _post(client, _payload(author, dedup_key="all-dead"))

    assert response.status_code == 200
    body = WorkFailedResult.model_validate(response.json())
    assert body.status == "task_alerted"
    assert body.delivery_kind == "task_alert"
    assert body.delivered_to is not None
    assert body.delivered_to.startswith("task:")
    assert _stored_event_delivery(db_conn, "all-dead") == (
        body.event_id,
        body.delivered_to,
        "task_alert",
        True,
    )
    task = db_conn.execute(
        "SELECT parent_id, title, description, created_by, owner FROM agent_tasks WHERE id = %s",
        (int(body.delivered_to.removeprefix("task:")),),
    ).fetchone()
    assert task is not None
    assert task[0] is not None
    assert task[2] == ("repo=Ava commit=" + "a" * 40 + " stage=ci\n\n" + "targeted tests failed")
    assert task[3:] == ("system", author)


def test_duplicate_dedup_key_never_delivers_twice(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    author = _seed_agent(db_conn, status=AgentStatus.IDLING)
    calls = 0

    async def _delivery(*args: object, **kwargs: object) -> ChatDelivery:
        nonlocal calls
        calls += 1
        return ChatDelivery(AgentStatus.IDLING, calls)

    monkeypatch.setattr(work_failed_router, "deliver_chat_inbound", _delivery)
    payload = _payload(author, dedup_key="one-logical-failure")
    with TestClient(app) as client:
        first = _post(client, payload)
        second = _post(client, payload)

    assert first.status_code == second.status_code == 200
    first_result = WorkFailedResult.model_validate(first.json())
    second_result = WorkFailedResult.model_validate(second.json())
    assert first_result.status == "delivered"
    assert second_result.status == "duplicate"
    assert second_result.event_id == first_result.event_id
    assert second_result.delivered_to == first_result.delivered_to
    assert second_result.delivery_kind == first_result.delivery_kind
    assert calls == 1
    assert db_conn.execute(
        "SELECT count(*) FROM work_failed_events WHERE dedup_key = 'one-logical-failure'"
    ).fetchone() == (1,)


async def test_reconcile_delivers_stale_unfinished_event(
    db_conn: psycopg.Connection,
    failure_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    author = _seed_agent(db_conn, status=AgentStatus.IDLING)
    event_id, _ = _seed_unfinished_failure(
        db_conn,
        author,
        dedup_key="stale-retry",
        age=timedelta(minutes=10),
    )

    async def _publish(*args: object, **kwargs: object) -> None:
        return None

    async def _keep_alive(*args: object, **kwargs: object) -> AgentStatus:
        return AgentStatus.IDLING

    monkeypatch.setattr(delivery_router._ops, "publish_inbound_arrived", _publish)
    monkeypatch.setattr(delivery_router._ops, "resurrect_if_terminated", _keep_alive)

    assert await work_failed_router.reconcile_stale_work_failures(failure_pool) == 1

    event = db_conn.execute(
        "SELECT delivered_to, delivery_kind, delivered_at IS NOT NULL, delivery_attempts "
        "FROM work_failed_events WHERE id = %s",
        (event_id,),
    ).fetchone()
    assert event == (f"agent:{author}", "author", True, 1)
    inbound = db_conn.execute(
        "SELECT source_verified_by, source_transport FROM inbound_messages "
        "WHERE client_message_id = %s",
        (f"work-failed:{event_id}:agent:{author}",),
    ).fetchone()
    assert inbound == (None, "reconcile")


async def test_reconcile_sends_attempts_over_limit_directly_to_task_alert(
    db_conn: psycopg.Connection,
    failure_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    author = _seed_agent(db_conn, status=AgentStatus.TERMINATED)
    event_id, _ = _seed_unfinished_failure(
        db_conn,
        author,
        dedup_key="retry-limit",
        age=timedelta(minutes=10),
        delivery_attempts=3,
    )
    db_conn.execute(
        "INSERT INTO agent_tasks (title, description, status, created_by, is_root) "
        "SELECT 'Root', 'root', 'ongoing', 'system', TRUE "
        "WHERE NOT EXISTS (SELECT 1 FROM agent_tasks WHERE is_root)"
    )
    db_conn.commit()

    async def _unexpected_delivery(*args: object, **kwargs: object) -> ChatDelivery:
        pytest.fail("an exhausted event must not retry agent delivery")

    monkeypatch.setattr(work_failed_router, "deliver_chat_inbound", _unexpected_delivery)

    assert await work_failed_router.reconcile_stale_work_failures(failure_pool) == 1
    event = db_conn.execute(
        "SELECT delivered_to, delivery_kind, delivered_at IS NOT NULL, delivery_attempts "
        "FROM work_failed_events WHERE id = %s",
        (event_id,),
    ).fetchone()
    assert event is not None
    assert event[0].startswith("task:")
    assert event[1:] == ("task_alert", True, 4)


async def test_reconcile_leaves_events_inside_grace_window_untouched(
    db_conn: psycopg.Connection,
    failure_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    author = _seed_agent(db_conn, status=AgentStatus.IDLING)
    event_id, _ = _seed_unfinished_failure(
        db_conn,
        author,
        dedup_key="young-event",
        age=timedelta(minutes=1),
    )

    async def _unexpected_delivery(*args: object, **kwargs: object) -> ChatDelivery:
        pytest.fail("an event inside the grace window must not be delivered")

    monkeypatch.setattr(work_failed_router, "deliver_chat_inbound", _unexpected_delivery)

    assert await work_failed_router.reconcile_stale_work_failures(failure_pool) == 0
    assert db_conn.execute(
        "SELECT delivered_at, delivery_attempts FROM work_failed_events WHERE id = %s",
        (event_id,),
    ).fetchone() == (None, 0)


async def test_concurrent_delivery_cas_deduplicates_the_agent_inbound(
    db_conn: psycopg.Connection,
    failure_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    author = _seed_agent(db_conn, status=AgentStatus.IDLING)
    event_id, body = _seed_unfinished_failure(
        db_conn,
        author,
        dedup_key="concurrent-delivery",
        age=timedelta(minutes=10),
    )

    async def _publish(*args: object, **kwargs: object) -> None:
        return None

    async def _keep_alive(*args: object, **kwargs: object) -> AgentStatus:
        await asyncio.sleep(0)
        return AgentStatus.IDLING

    monkeypatch.setattr(delivery_router._ops, "publish_inbound_arrived", _publish)
    monkeypatch.setattr(delivery_router._ops, "resurrect_if_terminated", _keep_alive)
    provenance = work_failed_router.InboundProvenance(
        source_verified_by="webhook:work_failed",
        source_transport="http",
    )

    results = await asyncio.gather(
        work_failed_router._deliver_failure(failure_pool, event_id, body, provenance),
        work_failed_router._deliver_failure(failure_pool, event_id, body, provenance),
    )

    assert sorted(result.status for result in results) == ["delivered", "duplicate"]
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE client_message_id = %s",
        (f"work-failed:{event_id}:agent:{author}",),
    ).fetchone() == (1,)
    assert db_conn.execute(
        "SELECT delivered_to, delivery_kind FROM work_failed_events WHERE id = %s",
        (event_id,),
    ).fetchone() == (f"agent:{author}", "author")


def test_work_failed_rejects_unverified_remote_caller(
    db_conn: psycopg.Connection,
) -> None:
    author = _seed_agent(db_conn, status=AgentStatus.IDLING)

    with TestClient(app) as client:
        response = client.post("/api/work-failed", json=_payload(author, dedup_key="no-auth"))

    assert response.status_code == 401
    assert db_conn.execute("SELECT count(*) FROM work_failed_events").fetchone() == (0,)
