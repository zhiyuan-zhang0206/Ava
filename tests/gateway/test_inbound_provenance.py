"""Server-owned inbound credential facts are recorded without enforcement."""

from __future__ import annotations

import hashlib

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from shared.chat_delivery import insert_chat_inbound_once
from shared.config import settings
from shared.db import create_agent, insert_inbound_message
from shared.inbound_provenance import InboundProvenance, source_assertion_match

_SECRET = "inbound-provenance-secret"  # noqa: S105 -- isolated test credential


def _seed_live_agent(conn: psycopg.Connection) -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, status, lease_expires_at) "
        "VALUES (%s, 'idling', now() + interval '5 minutes')",
        (agent_id,),
    )
    conn.commit()
    return agent_id


def _stored_provenance(conn: psycopg.Connection, agent_id: int) -> tuple[object, ...]:
    row = conn.execute(
        "SELECT content, source_verified_by, source_transport, content_hash, "
        "source_assertion_match FROM inbound_messages WHERE agent_id = %s ORDER BY id DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    assert row is not None
    return row


def test_cluster_bearer_message_records_http_provenance(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = _seed_live_agent(db_conn)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", _SECRET)

    with TestClient(app) as client:
        response = client.post(
            f"/api/agents/{agent_id}/messages",
            headers={"Authorization": f"Bearer {_SECRET}"},
            json={"content": "bearer message", "source": f"agent:{agent_id}"},
        )

    assert response.status_code == 201, response.text
    assert _stored_provenance(db_conn, agent_id) == (
        "bearer message",
        "cluster_bearer",
        "http",
        hashlib.sha256(b"bearer message").hexdigest(),
        None,
    )


def test_browser_message_records_user_session(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = _seed_live_agent(db_conn)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", _SECRET)

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"password": _SECRET})
        assert login.status_code == 200
        response = client.post(
            f"/api/agents/{agent_id}/messages",
            json={"content": "browser message", "source": "user"},
        )

    assert response.status_code == 201, response.text
    assert _stored_provenance(db_conn, agent_id)[1:] == (
        "user_session",
        "http",
        hashlib.sha256(b"browser message").hexdigest(),
        None,
    )


@pytest.mark.parametrize(
    ("source", "verified_by", "expected"),
    [
        ("agent:7", "agent_token:7", True),
        ("agent:7", "agent_token:8", False),
        ("agent:7", "cluster_bearer", None),
        ("user", "agent_token:7", None),
        ("agent:7", None, None),
    ],
)
def test_source_assertion_match_is_a_non_blocking_three_state_fact(
    source: str, verified_by: str | None, expected: bool | None
) -> None:
    provenance = InboundProvenance(
        source_verified_by=verified_by,
        source_transport="http",
    )

    assert source_assertion_match(source, provenance) is expected


@pytest.mark.parametrize(
    ("source", "verified_by", "expected"),
    [
        ("agent:7", "agent_token:7", True),
        ("agent:7", "agent_token:8", False),
        ("agent:7", "cluster_bearer", None),
    ],
)
def test_assertion_match_three_state_is_persisted_without_rejection(
    db_conn: psycopg.Connection,
    source: str,
    verified_by: str,
    expected: bool | None,
) -> None:
    agent_id = _seed_live_agent(db_conn)
    content = f"message from {source} via {verified_by}"

    receipt = insert_chat_inbound_once(
        db_conn,
        agent_id=agent_id,
        content=content,
        source=source,
        payload=None,
        client_message_id=f"assertion-{verified_by}",
        provenance=InboundProvenance(
            source_verified_by=verified_by,
            source_transport="http",
        ),
    )

    assert receipt.inserted is True
    assert _stored_provenance(db_conn, agent_id) == (
        content,
        verified_by,
        "http",
        hashlib.sha256(content.encode()).hexdigest(),
        expected,
    )


def test_provenance_change_does_not_reject_an_idempotent_retry(
    db_conn: psycopg.Connection,
) -> None:
    agent_id = _seed_live_agent(db_conn)
    first = insert_chat_inbound_once(
        db_conn,
        agent_id=agent_id,
        content="same logical message",
        source="agent:7",
        payload=None,
        client_message_id="credential-change",
        provenance=InboundProvenance(
            source_verified_by="agent_token:7",
            source_transport="http",
        ),
    )
    retried = insert_chat_inbound_once(
        db_conn,
        agent_id=agent_id,
        content="same logical message",
        source="agent:7",
        payload=None,
        client_message_id="credential-change",
        provenance=InboundProvenance(
            source_verified_by="agent_token:8",
            source_transport="ops",
        ),
    )

    assert retried.inbound_id == first.inbound_id
    assert retried.inserted is False
    assert _stored_provenance(db_conn, agent_id)[1:] == (
        "agent_token:7",
        "http",
        hashlib.sha256(b"same logical message").hexdigest(),
        True,
    )


def test_legacy_shared_writer_leaves_provenance_columns_null(
    db_conn: psycopg.Connection,
) -> None:
    agent_id = _seed_live_agent(db_conn)

    insert_inbound_message(db_conn, agent_id, "legacy inbound", source="user")

    assert _stored_provenance(db_conn, agent_id)[1:] == (None, None, None, None)
