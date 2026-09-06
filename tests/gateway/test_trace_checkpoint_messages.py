"""GET /api/agents/{id}/traces/{trace_id}/messages contract tests.

The trace-v2 on-demand content path: spans are metadata-only, so clicking an
LM span resolves the turn's full messages (system prompt included) from the
checkpoints table by trace id.

Coverage:
  - 404 for a nonexistent agent
  - pruned=true + empty messages when the trace id matches no checkpoint
    (compact/checkpoint trim dropped it, or the link predates the feature)
  - full messages returned when the checkpoint carries the trace_id
  - checkpoint read failure surfaces as 503
"""

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from shared.db import create_agent


@pytest.fixture
def test_client(db_conn: psycopg.Connection):
    with TestClient(app) as client:
        yield client


def _put_checkpoint_with_trace(agent_id: int, messages: list, trace_id: str) -> str:
    """Write one checkpoint whose metadata carries trace_id; return its id."""
    from typing import cast

    from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint
    from langgraph.checkpoint.postgres import PostgresSaver

    from shared.config import settings

    ckpt = empty_checkpoint()
    ckpt["channel_values"] = {"messages": messages}
    ckpt["channel_versions"] = {"messages": "1", "__start__": "1"}
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        saver.setup()
        saved = saver.put(
            config={"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
            checkpoint=ckpt,
            metadata=cast(
                CheckpointMetadata,
                {"trace_id": trace_id, "source": "loop", "step": 1, "parents": {}},
            ),
            new_versions={"messages": "1"},
        )
    cfg = saved.get("configurable") or {}
    return str(cfg["checkpoint_id"])


def test_404_for_unknown_agent(test_client: TestClient) -> None:
    resp = test_client.get(f"/api/agents/99999/traces/{'a' * 32}/messages")
    assert resp.status_code == 404


def test_pruned_when_no_checkpoint_carries_trace(
    db_conn: psycopg.Connection, test_client: TestClient
) -> None:
    """A trace id no checkpoint carries -> pruned=true, checkpoint_id=None,
    empty messages — the "\u5df2\u88c1\u526a" shape (content gone, span metadata remains)."""
    tid = create_agent(db_conn)
    resp = test_client.get(f"/api/agents/{tid}/traces/{'f' * 32}/messages")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pruned"] is True
    assert body["checkpoint_id"] is None
    assert body["messages"] == []
    assert body["trace_id"] == "f" * 32
    assert body["agent_id"] == tid


def test_returns_full_messages_for_trace(
    db_conn: psycopg.Connection, test_client: TestClient
) -> None:
    """The checkpoint carrying the trace id resolves to its full messages —
    system prompt included (raw BaseMessage model_dump() shape)."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    tid = create_agent(db_conn)
    trace_id = "ab" * 16
    msgs = [
        SystemMessage(content="You are Ava, an agent..."),
        HumanMessage(content="hello"),
        AIMessage(content="hi!"),
    ]
    ckpt_id = _put_checkpoint_with_trace(tid, msgs, trace_id)

    resp = test_client.get(f"/api/agents/{tid}/traces/{trace_id}/messages")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pruned"] is False
    assert body["checkpoint_id"] == ckpt_id
    assert [m["type"] for m in body["messages"]] == ["system", "human", "ai"]
    assert body["messages"][0]["content"] == "You are Ava, an agent..."
    assert body["messages"][2]["content"] == "hi!"


def test_503_on_checkpoint_read_failure(
    db_conn: psycopg.Connection, test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store read failure is not disguised as pruned — 503, caller retries."""
    from gateway.routers import agents_state as agents_mod
    from shared import checkpoint as checkpoint_mod

    tid = create_agent(db_conn)

    def _boom(*_a, **_kw):
        raise checkpoint_mod.CheckpointReadError("boom")

    monkeypatch.setattr(agents_mod, "load_checkpoint_messages_by_trace", _boom)  # pyright: ignore[reportUnknownArgumentType]
    resp = test_client.get(f"/api/agents/{tid}/traces/{'c' * 32}/messages")
    assert resp.status_code == 503
