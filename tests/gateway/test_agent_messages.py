"""GET /api/agents/{id}/messages contract tests.

This is the data-layer endpoint: raw state.messages as LangChain
BaseMessage `model_dump()` dicts, for programmatic consumers (ops scripts /
other agents / evals). The frontend-facing rendered view (TimelineItem) is
GET .../timeline and is covered separately in test_timeline.py.

Coverage:
  - 404 for a nonexistent agent (programmatic endpoint — a missing agent is a
    caller error, same agent-existence precondition as the timeline GET)
  - empty history for a new agent (no checkpoint -> empty list, msg_count 0,
    has_more false)
  - default + explicit windowing boundaries (tail window, page-back,
    before without limit, clamping an out-of-range cursor)
  - raw message dicts retain type/content fields, including a multi-block
    content list round-tripping unflattened
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


def _put_checkpoint(agent_id: int, messages: list) -> None:
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
    """New agent has no checkpoint -> empty list and no older page."""
    tid = create_agent(db_conn)
    resp = test_client.get(f"/api/agents/{tid}/messages")
    assert resp.status_code == 200
    assert resp.json() == {"messages": [], "msg_count": 0, "start_index": 0, "has_more": False}


def test_default_window_returns_raw_message_dicts(
    db_conn: psycopg.Connection, test_client: TestClient
) -> None:
    """A history shorter than the default window returns each raw BaseMessage
    model_dump() carrying type + content (+ tool_calls for the AIMessage).

    The AIMessage uses list-of-blocks content (thinking + text, the production
    ChatAnthropic shape) to verify the block list survives the full round-trip
    (checkpoint blob -> model_dump -> JSON) unflattened — the data endpoint
    returns the raw structure, not the timeline's per-block rendering."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    tid = create_agent(db_conn)
    ai_blocks: list[str | dict] = [
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
    assert body["has_more"] is False
    got = body["messages"]
    assert [m["type"] for m in got] == ["human", "ai", "tool"]
    assert got[0]["content"] == "run something"
    # The multi-block content list round-trips verbatim (not flattened into
    # separate items the way the timeline endpoint would render it).
    assert got[1]["content"] == ai_blocks
    # tool_calls survive the raw model_dump too.
    assert got[1]["tool_calls"][0]["args"]["code"] == "print(1)"
    assert got[2]["tool_call_id"] == "c0"


def test_default_window_returns_newest_hundred_with_older_page(
    db_conn: psycopg.Connection, test_client: TestClient
) -> None:
    """A no-parameter read is bounded to the newest 100 raw messages.

    Removing the default window or calculating `has_more` from the returned
    length rather than the absolute start position makes this fail.
    """
    from langchain_core.messages import HumanMessage

    tid = create_agent(db_conn)
    _put_checkpoint(tid, [HumanMessage(content=f"m{i}") for i in range(105)])

    resp = test_client.get(f"/api/agents/{tid}/messages")
    assert resp.status_code == 200
    body = resp.json()
    assert body["msg_count"] == 105
    assert body["start_index"] == 5
    assert body["has_more"] is True
    assert [m["content"] for m in body["messages"]] == [f"m{i}" for i in range(5, 105)]


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
    assert body["has_more"] is True
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
    assert body["has_more"] is True
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
    assert body["has_more"] is False
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
    assert body["has_more"] is True
    assert [m["content"] for m in body["messages"]] == ["m1", "m2"]


def test_before_without_limit_uses_default_window(
    db_conn: psycopg.Connection, test_client: TestClient
) -> None:
    """before without limit returns the newest 100 messages before the cursor."""
    from langchain_core.messages import HumanMessage

    tid = create_agent(db_conn)
    messages = [HumanMessage(content=f"m{i}") for i in range(205)]
    _put_checkpoint(tid, messages)

    resp = test_client.get(f"/api/agents/{tid}/messages?before=150")
    assert resp.status_code == 200
    body = resp.json()
    assert body["msg_count"] == 205
    assert body["start_index"] == 50
    assert body["has_more"] is True
    assert [m["content"] for m in body["messages"]] == [f"m{i}" for i in range(50, 150)]


def test_explicit_limit_paging_uses_start_index_as_next_before(
    db_conn: psycopg.Connection, test_client: TestClient
) -> None:
    """Explicit limit plus returned start_index walks the whole history oldest-first.

    Setting `has_more` from the end cursor or returning an inferred next cursor
    would either prematurely stop this traversal or duplicate a message.
    """
    from langchain_core.messages import HumanMessage

    tid = create_agent(db_conn)
    _put_checkpoint(tid, [HumanMessage(content=f"m{i}") for i in range(5)])

    newest = test_client.get(f"/api/agents/{tid}/messages?limit=2").json()
    middle = test_client.get(
        f"/api/agents/{tid}/messages?limit=2&before={newest['start_index']}"
    ).json()
    oldest = test_client.get(
        f"/api/agents/{tid}/messages?limit=2&before={middle['start_index']}"
    ).json()

    assert [page["has_more"] for page in [newest, middle, oldest]] == [True, True, False]
    assert [m["content"] for m in oldest["messages"] + middle["messages"] + newest["messages"]] == [
        "m0",
        "m1",
        "m2",
        "m3",
        "m4",
    ]


def test_limit_accepts_10000_and_rejects_10001(
    db_conn: psycopg.Connection, test_client: TestClient
) -> None:
    """The pre-existing inclusive 1..10000 public limit range remains intact."""
    from langchain_core.messages import HumanMessage

    tid = create_agent(db_conn)
    _put_checkpoint(tid, [HumanMessage(content="m0")])

    accepted = test_client.get(f"/api/agents/{tid}/messages?limit=10000")
    rejected = test_client.get(f"/api/agents/{tid}/messages?limit=10001")

    assert accepted.status_code == 200
    assert accepted.json()["messages"][0]["content"] == "m0"
    assert rejected.status_code == 422


def test_limit_zero_is_rejected_by_query_validation(test_client: TestClient) -> None:
    """The public page-size range starts at one, before agent lookup runs."""
    response = test_client.get("/api/agents/99999/messages?limit=0")

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["errors"][0]["loc"] == ["query", "limit"]


def test_before_zero_returns_empty_window(
    db_conn: psycopg.Connection, test_client: TestClient
) -> None:
    """The exclusive absolute cursor zero has no prefix to return."""
    from langchain_core.messages import HumanMessage

    tid = create_agent(db_conn)
    _put_checkpoint(tid, [HumanMessage(content="m0")])

    response = test_client.get(f"/api/agents/{tid}/messages?before=0")

    assert response.status_code == 200
    assert response.json() == {"messages": [], "msg_count": 1, "start_index": 0, "has_more": False}


def test_checkpoint_read_failure_returns_503(
    db_conn: psycopg.Connection,
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkpoint store read failure surfaces as 503 — the programmatic data
    endpoint does not disguise an IO failure as an empty history (contrast the
    timeline GET, which tolerates the same failure to an empty view + 200).

    Mirrors test_timeline.TestTimelineFailLoud: patch langgraph.checkpoint.postgres's
    PostgresSaver name (where load_checkpoint_messages' function-local import
    resolves it) so from_conn_string raises, exercising the CheckpointReadError -> 503 path.
    """
    from contextlib import contextmanager

    import langgraph.checkpoint.postgres as ckpt_mod

    tid = create_agent(db_conn)

    @contextmanager
    def fake_saver(_url):
        raise OSError("simulated DB connection lost")
        yield  # type: ignore[unreachable]

    class FakeSaver:
        from_conn_string = staticmethod(fake_saver)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(ckpt_mod, "PostgresSaver", FakeSaver)
    # raise_server_exceptions=False so FastAPI's error middleware turns the
    # HTTPException into a real HTTP 503 instead of re-raising into the test.
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(f"/api/agents/{tid}/messages")
    assert resp.status_code == 503, f"expected 503, got {resp.status_code}: {resp.text[:200]}"
