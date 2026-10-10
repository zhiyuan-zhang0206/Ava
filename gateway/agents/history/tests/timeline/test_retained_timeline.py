"""Explicit retained history remains readable without trusting the live head."""

from __future__ import annotations

import secrets

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, BaseMessage

from base.agents.history.checkpoint import CheckpointReadError
from base.cluster.auth import bearer_header
from base.config import settings
from base.db import create_agent
from gateway.agents.history import timeline
from gateway.agents.history.tests.test_timeline import test_client as test_client
from gateway.app import app


def test_retained_read_requires_auth_and_serializes_for_authenticated_caller(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = secrets.token_urlsafe(24)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", secret)
    tid = create_agent(db_conn)
    with TestClient(app) as client:
        rejected = client.get(f"/api/agents/{tid}/timeline/retained")
        assert rejected.status_code == 401
        accepted = client.get(f"/api/agents/{tid}/timeline/retained", headers=bearer_header(secret))
    assert accepted.status_code == 200
    assert accepted.json() == {"boundary_checkpoint_id": None, "items": [], "has_more": False}


def test_retained_history_can_start_and_page_without_reading_broken_live_head(
    db_conn: psycopg.Connection, test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = create_agent(db_conn)
    monkeypatch.setattr(settings.gateway, "timeline_compact_history", -1)
    boundaries = ["retained-newer", "retained-older"]
    reads: list[str] = []

    def index(_db: object, _id: int, *, limit: int | None = None) -> list[str]:
        return boundaries

    def segment(_db: object, _id: int, checkpoint_id: str) -> list[BaseMessage]:
        reads.append(checkpoint_id)
        return [AIMessage(content=f"retained {i}", id=f"r{i}") for i in range(3)]

    def broken_live(*_args: object) -> list[BaseMessage]:
        raise CheckpointReadError("live parent is missing")

    monkeypatch.setattr(timeline, "list_compact_boundary_checkpoint_ids", index)
    monkeypatch.setattr(timeline, "load_checkpoint_messages_segment", segment)
    monkeypatch.setattr(timeline, "load_checkpoint_messages", broken_live)
    monkeypatch.setattr(timeline, "load_checkpoint_message_count", broken_live)
    live = test_client.get(f"/api/agents/{tid}/timeline")
    assert live.status_code == 503
    first = test_client.get(f"/api/agents/{tid}/timeline/retained", params={"limit": 1})
    assert first.status_code == 200
    body = first.json()
    assert body["boundary_checkpoint_id"] == boundaries[0]
    assert "msg_count" not in body
    assert [item["item_id"] for item in body["items"]] == ["s1.retained-newer.2.0"]
    assert body["has_more"] is True
    second = test_client.get(
        f"/api/agents/{tid}/timeline/retained",
        params={"limit": 1, "before": body["items"][0]["item_id"]},
    )
    assert second.status_code == 200
    assert [item["item_id"] for item in second.json()["items"]] == ["s1.retained-newer.1.0"]
    assert reads == ["retained-newer", "retained-newer"]
    explicit = test_client.get(
        f"/api/agents/{tid}/timeline/retained", params={"checkpoint_id": boundaries[1], "limit": 1}
    )
    assert explicit.status_code == 200
    assert explicit.json()["boundary_checkpoint_id"] == boundaries[1]
    assert [item["item_id"] for item in explicit.json()["items"]] == ["s2.retained-older.2.0"]


@pytest.mark.parametrize(
    ("params", "status"),
    [({"checkpoint_id": "not-retained"}, 404), ({"before": "1.0"}, 400)],
)
def test_retained_entry_never_guesses_a_boundary_or_current_cursor(
    db_conn: psycopg.Connection,
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, str],
    status: int,
) -> None:
    tid = create_agent(db_conn)
    monkeypatch.setattr(settings.gateway, "timeline_compact_history", -1)
    response = test_client.get(f"/api/agents/{tid}/timeline/retained", params=params)
    assert response.status_code == status


def test_retained_empty_agent_remains_successful_empty(
    db_conn: psycopg.Connection, test_client: TestClient
) -> None:
    tid = create_agent(db_conn)
    response = test_client.get(f"/api/agents/{tid}/timeline/retained")
    assert response.status_code == 200
    assert response.json() == {"boundary_checkpoint_id": None, "items": [], "has_more": False}
