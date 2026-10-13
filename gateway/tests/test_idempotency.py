"""Keyed chat delivery and its reconcile endpoint (R3 door ① server side).

The endpoint POST /api/agents/{id}/messages declares
Idempotency.AT_LEAST_ONCE_WITH_KEY: the caller gives one logical message a
stable key. The inbound INSERT owns that key in its transaction, and a same-key
retry resolves the durable row instead of inserting again — even when the
gateway died after COMMIT and before returning its response.

Tests run against the real app + test DB (TestClient + lifespan).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from unittest.mock import patch

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.api_contracts.agent_contract import AgentStatus
from base.api_contracts.idempotency import keyed_headers
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from gateway.app import app


@pytest.fixture
def client(gateway_data_plane: None) -> Iterator[TestClient]:
    # A real lifespan boots its own delivered environment; restore that root's
    # projection as well as closing its HTTP/database resources.
    with patch.dict(os.environ), TestClient(app) as c:
        yield c


@pytest.fixture
def agent_id(db_conn: psycopg.Connection) -> int:
    """A minimal agents row (agents + agents_meta) for message delivery."""
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES ('idem-test') RETURNING id")
        row = cur.fetchone()
        assert row is not None
        aid = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, status) VALUES (%s, 'running')",
            (aid,),
        )
    db_conn.commit()
    return int(aid)


def _count_inbounds(conn: psycopg.Connection, agent_id: int, content: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM inbound_messages WHERE agent_id = %s AND content = %s",
            (agent_id, content),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def test_reconcile_endpoint_finds_the_durable_inbound(client: TestClient, agent_id: int) -> None:
    """A browser whose POST timed out can resolve the unknown outcome by key."""
    sent = client.post(
        f"/api/agents/{agent_id}/messages",
        json={"content": "reconcile me", "source": "user"},
        headers=keyed_headers("key-reconcile"),
    )
    assert sent.status_code == 201, sent.text

    receipt = client.post(
        f"/api/agents/{agent_id}/messages/reconcile",
        json={"content": "reconcile me", "source": "user"},
        headers=keyed_headers("key-reconcile"),
    )

    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["inbound_id"] == sent.json()["inbound_id"]


def test_reconcile_does_not_repeat_mutable_multimodal_validation(
    client: TestClient,
    agent_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A receipt survives model/upload changes after the original commit."""
    from base.agents.upload_delivery.paths import agent_upload_dir
    from base.lm import factory

    upload_dir = agent_upload_dir(agent_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    image = upload_dir / "gone.png"
    image.write_bytes(b"test image")

    def supports_vision(_model: str, *, catalog: ModelCatalog) -> bool:
        return True

    monkeypatch.setattr(factory, "model_supports_vision", supports_vision)

    body = {
        "content": [
            {
                "type": "image_url",
                "image_url": {"url": f"/api/agents/{agent_id}/uploads/gone.png"},
            }
        ],
        "source": "user",
    }
    sent = client.post(
        f"/api/agents/{agent_id}/messages",
        json=body,
        headers=keyed_headers("key-multimodal-reconcile"),
    )
    assert sent.status_code == 201, sent.text
    image.unlink()

    def _mutable_gate_must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("same-key receipt lookup re-ran current model/upload validation")

    monkeypatch.setattr(factory, "model_supports_vision", _mutable_gate_must_not_run)
    retried = client.post(
        f"/api/agents/{agent_id}/messages",
        json=body,
        headers=keyed_headers("key-multimodal-reconcile"),
    )
    assert retried.status_code == 201, retried.text
    assert retried.json()["inbound_id"] == sent.json()["inbound_id"]

    receipt = client.post(
        f"/api/agents/{agent_id}/messages/reconcile",
        json=body,
        headers=keyed_headers("key-multimodal-reconcile"),
    )
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["inbound_id"] == sent.json()["inbound_id"]


def test_reconcile_heals_crash_after_commit_before_resurrect(
    client: TestClient,
    db_conn: psycopg.Connection,
    agent_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost first response heals one pending chat and its terminated owner."""
    from ops import lifecycle as ops_lifecycle

    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
    db_conn.commit()
    calls = 0

    async def _crash_then_heal(_db: object, _bus: EventBus, aid: int, **kw: object) -> AgentStatus:
        nonlocal calls
        calls += 1
        assert aid == agent_id
        assert kw["trigger_inbound_kind"] == "chat"
        if calls == 1:
            raise RuntimeError("gateway died after commit")
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = 'idling' WHERE id = %s AND status = 'terminated'",
                (aid,),
            )
            if cur.rowcount == 1:
                cur.execute(
                    "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                    "VALUES (%s, '', 'resurrect', 'system')",
                    (aid,),
                )
        db_conn.commit()
        return AgentStatus.IDLING

    monkeypatch.setattr(ops_lifecycle, "resurrect_if_terminated", _crash_then_heal)
    with pytest.raises(RuntimeError, match="gateway died after commit"):
        client.post(
            f"/api/agents/{agent_id}/messages",
            json={"content": "survive crash", "source": "user"},
            headers=keyed_headers("key-crash-heal"),
        )

    receipt = client.post(
        f"/api/agents/{agent_id}/messages/reconcile",
        json={"content": "survive crash", "source": "user"},
        headers=keyed_headers("key-crash-heal"),
    )
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["status"] == "idling"
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT kind, count(*) FROM inbound_messages WHERE agent_id = %s "
            "GROUP BY kind ORDER BY kind",
            (agent_id,),
        )
        assert cur.fetchall() == [("chat", 1), ("resurrect", 1)]


def _inject_delivery_failure(patch: pytest.MonkeyPatch, step: str) -> None:
    from ops import lifecycle
    from ops.agents import delivery
    from ops.cluster import rpc

    async def fail_async(*_args: object, **_kwargs: object) -> None:
        raise AttributeError("injected postcommit bug")

    def fail_sync(*_args: object, **_kwargs: object) -> None:
        raise AttributeError("injected postcommit bug")

    targets = {
        "wake": (delivery, "publish_inbound_wake", fail_sync),
        "live": (lifecycle, "publish_inbound_arrived", fail_async),
        "resurrection": (lifecycle, "resurrect_if_terminated", fail_async),
        "resurrection_rpc": (rpc, "dispatch_to_machine", fail_async),
    }
    target, name, failure = targets[step]
    patch.setattr(target, name, failure)


@pytest.mark.parametrize("step", ["wake", "live", "resurrection", "resurrection_rpc"])
def test_postcommit_error_response_preserves_receipt_and_same_key_recovery(
    client: TestClient,
    db_conn: psycopg.Connection,
    agent_id: int,
    monkeypatch: pytest.MonkeyPatch,
    step: str,
) -> None:
    """Unknown tail failures fail this request, preserve its row and permit keyed healing."""
    from ops import lifecycle

    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (agent_id,))
    db_conn.commit()

    path = f"/api/agents/{agent_id}/messages"
    key = f"postcommit-{step}"
    body = {"content": "one logical message", "source": "user"}
    with monkeypatch.context() as patch:
        _inject_delivery_failure(patch, step)
        # The fixture already owns the real app lifespan. This client only
        # observes the HTTP error body instead of re-raising server exceptions.
        observer = TestClient(app, raise_server_exceptions=False)
        try:
            failed = observer.post(path, json=body, headers=keyed_headers(key))
            assert failed.status_code == 500, failed.text
            error = failed.json()
            assert error["committed"] is True
            assert error["idempotency_key"] == key
            assert error["retryable"] is False
            assert _count_inbounds(db_conn, agent_id, body["content"]) == 1
            # Another request is still served by the same app after this bug.
            healthy = observer.get(f"/api/agents/{agent_id}")
            assert healthy.status_code == 200, healthy.text
        finally:
            observer.close()

    async def heal_once(_db: object, _bus: object, aid: int, **_kwargs: object) -> AgentStatus:
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status='idling' WHERE id=%s AND status='terminated'",
                (aid,),
            )
            if cur.rowcount == 1:
                cur.execute(
                    "INSERT INTO inbound_messages (agent_id,content,kind,source) "
                    "VALUES (%s,'','resurrect','system')",
                    (aid,),
                )
        db_conn.commit()
        return AgentStatus.IDLING

    monkeypatch.setattr(lifecycle, "resurrect_if_terminated", heal_once)
    healed = client.post(path + "/reconcile", json=body, headers=keyed_headers(key))
    assert healed.status_code == 200, healed.text
    assert healed.json()["inbound_id"] == error["inbound_id"]
    repeated = client.post(path, json=body, headers=keyed_headers(key))
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["inbound_id"] == error["inbound_id"]
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT kind,count(*) FROM inbound_messages WHERE agent_id=%s GROUP BY kind ORDER BY kind",
            (agent_id,),
        )
        assert cur.fetchall() == [("chat", 1), ("resurrect", 1)]


def test_large_message_response_waits_for_live_publish(
    client: TestClient,
    db_conn: psycopg.Connection,
    agent_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The near-limit HTTP message commits before its awaited live publish returns."""
    from ops import lifecycle

    content = "x" * (1024 * 1024 - 1)
    published: list[int] = []
    real_publish = lifecycle.publish_inbound_arrived

    async def publish(
        bus: EventBus, aid: int, inbound_id: int, kind: str, source: str, text: str
    ) -> None:
        assert text == content
        assert _count_inbounds(db_conn, agent_id, content) == 1
        await real_publish(bus, aid, inbound_id, kind, source, text)
        published.append(inbound_id)

    monkeypatch.setattr(lifecycle, "publish_inbound_arrived", publish)
    response = client.post(
        f"/api/agents/{agent_id}/messages",
        json={"content": content, "source": "user"},
        headers=keyed_headers("large-awaited-live"),
    )
    assert response.status_code == 201, response.text
    assert published == [response.json()["inbound_id"]]
