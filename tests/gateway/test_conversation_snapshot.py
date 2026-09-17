"""GET /api/agents/{id}/conversation-snapshot contract tests.

One composed read for the selected agent's switch refresh (task #3900 batch
2): the head timeline window, token usage, and pending inbounds in a single
round trip. This route composes the standalone endpoints' own functions — the
tests here own the COMPOSITION (all three sections, right values) plus the
route's edge contract (nonexistent agent 404s through the timeline read,
matching GET .../timeline).
"""

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from shared.db import create_agent, insert_inbound_message


@pytest.fixture
def test_client(db_conn: psycopg.Connection):
    """TestClient + lifespan, same harness as the standalone endpoint tests."""
    with TestClient(app) as client:
        yield client


def _put_checkpoint(agent_id: int, messages: list) -> None:
    """Write channel_values.messages directly (same helper shape as
    test_token_usage.py): the composed reads only care about what is stored."""
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.postgres import PostgresSaver

    from shared.config import settings

    ckpt = empty_checkpoint()
    ckpt["channel_values"] = {"messages": messages}
    ckpt["channel_versions"] = {"messages": "1", "__start__": "1"}
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        saver.setup()
        saver.put(
            config={"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
            checkpoint=ckpt,
            metadata={"source": "input", "step": 1, "parents": {}},
            new_versions={"messages": "1"},
        )


def _usage_message(input_tokens: int, output_tokens: int, reasoning: int):
    from langchain_core.messages import AIMessage

    return AIMessage(
        content="hello",
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "output_token_details": {"reasoning": reasoning},
        },
    )


def test_snapshot_composes_all_three_sections(
    db_conn: psycopg.Connection, test_client: TestClient
) -> None:
    tid = create_agent(db_conn)
    _put_checkpoint(tid, [_usage_message(100, 20, 7)])
    pending_id = insert_inbound_message(db_conn, tid, "queued question", source="user")

    resp = test_client.get(f"/api/agents/{tid}/conversation-snapshot")
    assert resp.status_code == 200
    body = resp.json()

    assert set(body) == {"timeline", "token_usage", "pending"}

    # Timeline section: the head-window shape GET .../timeline serves.
    timeline = body["timeline"]
    assert set(timeline) >= {"items", "msg_count", "has_more"}

    # Token section: same values as the standalone endpoint (its scan
    # contract is covered in test_token_usage.py; here: the composition).
    assert body["token_usage"]["input_tokens"] == 100
    assert body["token_usage"]["output_tokens"] == 20
    assert body["token_usage"]["reasoning_tokens"] == 7
    assert body["token_usage"]["max_input_tokens"] > 0

    # Pending section: the same list GET .../pending serves.
    assert [p["id"] for p in body["pending"]] == [pending_id]
    assert [p["content"] for p in body["pending"]] == ["queued question"]


def test_snapshot_sections_match_the_standalone_endpoints(
    db_conn: psycopg.Connection, test_client: TestClient
) -> None:
    """No drift: each composed section equals the standalone endpoint's own
    response for the same state (read side by side)."""
    tid = create_agent(db_conn)
    _put_checkpoint(tid, [_usage_message(42, 2, 0)])
    insert_inbound_message(db_conn, tid, "queued", source="user")

    snap = test_client.get(f"/api/agents/{tid}/conversation-snapshot").json()
    assert snap["timeline"] == test_client.get(f"/api/agents/{tid}/timeline").json()
    assert snap["token_usage"] == test_client.get(f"/api/agents/{tid}/token-usage").json()
    assert snap["pending"] == test_client.get(f"/api/agents/{tid}/pending").json()


def test_snapshot_nonexistent_agent_404(
    db_conn: psycopg.Connection, test_client: TestClient
) -> None:
    resp = test_client.get("/api/agents/999999/conversation-snapshot")
    assert resp.status_code == 404
