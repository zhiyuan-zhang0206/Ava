"""GET /api/agents/{id}/messages contract tests.

This is the data-layer endpoint: raw state.messages as LangChain
BaseMessage `model_dump()` dicts, for programmatic consumers (ops scripts /
other agents / evals). The frontend-facing rendered view (TimelineItem) is
GET .../timeline and is covered separately in test_timeline.py.

Coverage:
  - 404 for a nonexistent agent (programmatic endpoint — a missing agent is a
    caller error, same agent-existence precondition as the timeline GET)
  - empty history for a new agent (no checkpoint -> empty list, msg_count 0)
  - full read returns every message as a raw dict with type/content fields,
    including a multi-block content list round-tripping unflattened
  - limit + before windowing boundaries (tail window, page-back, before
    without limit, clamping an out-of-range cursor)
  - a checkpoint store read failure surfaces as 503 (no disguising it as an
    empty history)
"""

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from shared.db import create_agent


@pytest.fixture
def test_client(db_conn: psycopg.Connection):
    """TestClient + lifespan. db_conn's TRUNCATE runs before lifespan — the
    DB pool reuses the same test database (settings.data_plane.db_url is swapped by the
    conftest)."""
    with TestClient(app) as client:
        yield client


def _put_checkpoint(agent_id: int, messages: list) -> None:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    """Write channel_values.messages = `messages` directly via
    PostgresSaver.put, bypassing the graph — the endpoint only cares about
    reading back what is in the checkpoint."""
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


def test_404_for_unknown_agent(test_client: TestClient) -> None:
    resp = test_client.get("/api/agents/99999/messages")
    assert resp.status_code == 404


def test_empty_for_new_agent(db_conn: psycopg.Connection, test_client: TestClient) -> None:
    """New agent has no checkpoint -> empty list, msg_count 0, start_index 0."""
    tid = create_agent(db_conn)
    resp = test_client.get(f"/api/agents/{tid}/messages")
    assert resp.status_code == 200
    assert resp.json() == {"messages": [], "msg_count": 0, "start_index": 0}


def test_full_read_returns_raw_message_dicts(
    db_conn: psycopg.Connection, test_client: TestClient
) -> None:
    """No limit -> every message, each a raw BaseMessage model_dump() carrying
    type + content (+ tool_calls for the AIMessage).

    The AIMessage uses list-of-blocks content (thinking + text, the production
    ChatAnthropic shape) to verify the block list survives the full round-trip
    (checkpoint blob -> model_dump -> JSON) unflattened — the data endpoint
    returns the raw structure, not the timeline's per-block rendering."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    tid = create_agent(db_conn)
    ai_blocks: list[str | dict] = [  # pyright: ignore[reportMissingTypeArgument, reportUnknownVariableType]
        {"type": "thinking", "thinking": "hm"},
        {"type": "text", "text": "on it"},
    ]
    messages = [
        HumanMessage(content="run something"),
        AIMessage(
            content=ai_blocks,
            tool_calls=[{"name": "execute_code", "args": {"code": "print(1)"}, "id": "c0"}],
        ),
        ToolMessage(content="1\n", tool_call_id="c0"),
    ]
    _put_checkpoint(tid, messages)

    resp = test_client.get(f"/api/agents/{tid}/messages")
    assert resp.status_code == 200
    body = resp.json()
    assert body["msg_count"] == 3
    assert body["start_index"] == 0
    got = body["messages"]
    assert [m["type"] for m in got] == ["human", "ai", "tool"]
    assert got[0]["content"] == "run something"
    # The multi-block content list round-trips verbatim (not flattened into
    # separate items the way the timeline endpoint would render it).
    assert got[1]["content"] == ai_blocks
    # tool_calls survive the raw model_dump too.
    assert got[1]["tool_calls"][0]["args"]["code"] == "print(1)"
    assert got[2]["tool_call_id"] == "c0"


def test_limit_returns_newest_tail(db_conn: psycopg.Connection, test_client: TestClient) -> None:
    """limit (no before) -> the newest `limit` messages; start_index points at
    the first returned message's absolute position."""
    from langchain_core.messages import HumanMessage

    tid = create_agent(db_conn)
    messages = [HumanMessage(content=f"m{i}") for i in range(5)]
    _put_checkpoint(tid, messages)

    resp = test_client.get(f"/api/agents/{tid}/messages?limit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["msg_count"] == 5
    assert body["start_index"] == 3
    assert [m["content"] for m in body["messages"]] == ["m3", "m4"]


def test_limit_with_before_pages_back(db_conn: psycopg.Connection, test_client: TestClient) -> None:
    """before is an exclusive absolute-index cursor: window is the `limit`
    messages immediately older than state.messages[before]."""
    from langchain_core.messages import HumanMessage

    tid = create_agent(db_conn)
    messages = [HumanMessage(content=f"m{i}") for i in range(5)]
    _put_checkpoint(tid, messages)

    # before=3 exclusive, limit=2 -> indices 1,2
    resp = test_client.get(f"/api/agents/{tid}/messages?limit=2&before=3")
    assert resp.status_code == 200
    body = resp.json()
    assert body["msg_count"] == 5
    assert body["start_index"] == 1
    assert [m["content"] for m in body["messages"]] == ["m1", "m2"]


def test_before_reaching_start_clamps(db_conn: psycopg.Connection, test_client: TestClient) -> None:
    """before smaller than limit -> window starts at index 0, no underflow."""
    from langchain_core.messages import HumanMessage

    tid = create_agent(db_conn)
    messages = [HumanMessage(content=f"m{i}") for i in range(5)]
    _put_checkpoint(tid, messages)

    # before=2 exclusive, limit=10 -> indices 0,1 only
    resp = test_client.get(f"/api/agents/{tid}/messages?limit=10&before=2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["start_index"] == 0
    assert [m["content"] for m in body["messages"]] == ["m0", "m1"]


def test_before_out_of_range_clamps_to_count(
    db_conn: psycopg.Connection, test_client: TestClient
) -> None:
    """before past the end clamps to msg_count -> still a valid tail window."""
    from langchain_core.messages import HumanMessage

    tid = create_agent(db_conn)
    messages = [HumanMessage(content=f"m{i}") for i in range(3)]
    _put_checkpoint(tid, messages)

    resp = test_client.get(f"/api/agents/{tid}/messages?limit=2&before=999")
    assert resp.status_code == 200
    body = resp.json()
    assert body["msg_count"] == 3
    assert body["start_index"] == 1
    assert [m["content"] for m in body["messages"]] == ["m1", "m2"]


def test_before_without_limit_returns_prefix(
    db_conn: psycopg.Connection, test_client: TestClient
) -> None:
    """before (no limit) -> the whole prefix before the cursor:
    state.messages[0:before], start_index 0, msg_count still the full length."""
    from langchain_core.messages import HumanMessage

    tid = create_agent(db_conn)
    messages = [HumanMessage(content=f"m{i}") for i in range(5)]
    _put_checkpoint(tid, messages)

    resp = test_client.get(f"/api/agents/{tid}/messages?before=3")
    assert resp.status_code == 200
    body = resp.json()
    assert body["msg_count"] == 5
    assert body["start_index"] == 0
    assert [m["content"] for m in body["messages"]] == ["m0", "m1", "m2"]


def test_checkpoint_read_failure_returns_503(
    db_conn: psycopg.Connection,
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkpoint store read failure surfaces as 503 — the programmatic data
    endpoint does not disguise an IO failure as an empty history (contrast the
    timeline GET, which tolerates the same failure to an empty view + 200).

    Mirrors test_timeline.TestTimelineFailLoud: patch shared.checkpoint's
    PostgresSaver name (where load_checkpoint_messages resolves it) so
    from_conn_string raises, exercising the CheckpointReadError -> 503 path.
    """
    from contextlib import contextmanager

    import shared.checkpoint as ckpt_mod

    tid = create_agent(db_conn)

    @contextmanager
    def fake_saver(_url):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        raise OSError("simulated DB connection lost")
        yield  # type: ignore[unreachable]

    class FakeSaver:
        from_conn_string = staticmethod(fake_saver)  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]

    monkeypatch.setattr(ckpt_mod, "PostgresSaver", FakeSaver)
    # raise_server_exceptions=False so FastAPI's error middleware turns the
    # HTTPException into a real HTTP 503 instead of re-raising into the test.
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(f"/api/agents/{tid}/messages")
    assert resp.status_code == 503, f"expected 503, got {resp.status_code}: {resp.text[:200]}"
