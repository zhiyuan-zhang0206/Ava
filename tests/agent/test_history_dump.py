"""Pre-compact history dump tests (`agent/history_dump.py`).

Covers the four contracts of the feature:
- config gating: `settings.agent.history_dump_enabled` off (default) → no file,
  no note; on → dump written + note injected;
- dump content completeness + replayability: one JSONL line per message, each
  line a LangChain BaseMessage `model_dump(mode="json")` that round-trips
  through `langchain_core.load.loads` with type / content / kwargs intact;
- rotation: only the newest `history_dump_keep` dumps survive;
- note injection position: the note rides the parked `context_reset.tail`
  AFTER the summary — never the live `messages` channel, and never between an
  AIMessage and its ToolMessage (the DeepSeek anthropic endpoint rejects that
  shape with a 400). Both compact paths (auto hook + claim node) are covered.
"""

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_from_dict,
)
from langchain_core.messages.modifier import RemoveMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent import history_dump
from agent.graph import claim_node
from agent.graph._context import AvaContext
from agent.hooks.compact import auto_compact_before_llm, compose_summary_message
from agent.messages import NoteTag, system_note_message
from agent.state import AgentState
from shared.config import settings
from shared.lm.context_budget import ContextBudget
from tests.conftest import spawn_agent

# ── helpers ──


def _patch_dump_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    *,
    enabled: bool = True,
    keep: int = 5,
) -> Any:
    """Enable the dump and redirect the workspace to a tmp dir, so tests never
    write into the real per-agent workspace. Returns the fake workspace root."""
    monkeypatch.setattr(settings.agent, "history_dump_enabled", enabled)
    monkeypatch.setattr(settings.agent, "history_dump_keep", keep)
    ws = tmp_path / "workspace"

    def _fake_workspace_dir(_aid: int) -> Any:
        return ws

    monkeypatch.setattr("agent.history_dump.workspace_dir", _fake_workspace_dir)
    return ws


class _FakeClock:
    """Controllable `datetime` replacement for `agent.history_dump` — lets a
    test write several dumps with distinct, ordered timestamps without sleeping."""

    _counter = 0

    @classmethod
    def now(cls, _tz: Any | None = None):
        cls._counter += 1
        return datetime(2026, 8, 13, 12, 0, 0, cls._counter, tzinfo=UTC)


def _patch_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent.history_dump.datetime", _FakeClock)


def _sample_messages() -> list[AnyMessage]:
    """A representative conversation: system prompt, inbound, tool round-trip,
    an AI reply, and a framework system note — every shape the dump must keep."""
    return [
        SystemMessage(content="<sys prompt>"),
        HumanMessage(
            content="User hello",
            additional_kwargs={"ava_msg_type": "inbound", "ava_source": "user"},
        ),
        AIMessage(
            content="",
            tool_calls=[{"name": "execute_code", "args": {"code": "x"}, "id": "c1"}],
            usage_metadata={"input_tokens": 5, "output_tokens": 1, "total_tokens": 6},
        ),
        ToolMessage(
            content="ran ok",
            tool_call_id="c1",
            additional_kwargs={"ava_msg_type": "exec_output", "ava_exit_code": 0},
        ),
        AIMessage(content="done"),
        system_note_message(
            content="a framework note",
            tag=NoteTag.COMPACT_REMINDER,
            created_at=datetime.now(UTC),
        ),
    ]


def _compact_tail(update: Any) -> list[AnyMessage]:
    """Same transport assertion as tests/agent/test_compact.py: the window is
    cleared (REMOVE_ALL sentinel alone) and what follows the head is parked in
    `context_reset.tail`. Returns the tail."""
    msgs = update["messages"]
    assert len(msgs) == 1
    assert isinstance(msgs[0], RemoveMessage)
    assert msgs[0].id == REMOVE_ALL_MESSAGES
    return update["context_reset"].tail


def _patch_compact_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin compact thresholds so any message exceeds the force ceiling."""
    budget = ContextBudget(
        max_context_tokens=1_000_000,
        soft_compact_tokens=1,
        hard_compact_tokens=1,
    )
    monkeypatch.setattr(
        "agent.hooks.compact.resolve_context_budget",  # pyright: ignore[reportUnknownArgumentType]
        lambda _model: budget,  # pyright: ignore[reportUnknownArgumentType]
    )


def _fake_llm(summary_text: str) -> Any:
    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(return_value=AIMessage(content=summary_text))
    return llm


_LONG_SUMMARY = "## Requests\nfollow the template. " * 60


def _runtime_with_llm(llm: Any) -> Runtime[AvaContext]:
    return Runtime(context=AvaContext(ops_pool=None, llm=llm, event_publisher=MagicMock()))


def _fake_config() -> RunnableConfig:
    return {"configurable": {"thread_id": "1"}}


def _over_threshold_state() -> AgentState:
    return AgentState(
        messages=[
            SystemMessage(content="<sys>"),
            *(HumanMessage(content="x" * 1000) for _ in range(5)),
        ],
        halted=False,
    )


def _make_runtime(
    *,
    ops_pool: AsyncConnectionPool | None = None,
    llm: Any | None = None,
) -> Runtime[AvaContext]:
    ctx = AvaContext(
        ops_pool=ops_pool,
        llm=llm if llm is not None else _fake_llm("synthetic summary"),
        event_publisher=MagicMock(),
    )
    return Runtime(context=ctx)


def _insert_inbound_kind(
    db: psycopg.Connection, tid: int, content: str, kind: str, source: str = "system"
) -> int:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (tid, content, kind, source),
        )
        new_id = cur.fetchone()[0]  # type: ignore[index]
    db.commit()
    return new_id


def _config(tid: int) -> RunnableConfig:
    return {"configurable": {"thread_id": str(tid)}}


# ── dump_history: config gating ──


def test_dump_disabled_by_default_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Any):
    """Config off (the default) → dump_history returns None and creates nothing."""
    ws = _patch_dump_enabled(monkeypatch, tmp_path, enabled=False)
    assert history_dump.dump_history(_sample_messages(), 1) is None
    assert not (ws / "compact_dumps").exists()


def test_dump_enabled_writes_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Any):
    """Config on → the dump is written under <workspace>/compact_dumps/ and the
    path is returned."""
    ws = _patch_dump_enabled(monkeypatch, tmp_path)
    path = history_dump.dump_history(_sample_messages(), 1)
    assert path is not None
    assert path.parent == ws / "compact_dumps"
    assert path.name.endswith(".jsonl")
    assert path.exists()


# ── dump_history: content completeness + replayability ──


def test_dump_is_replayable_jsonl(monkeypatch: pytest.MonkeyPatch, tmp_path: Any):
    """One JSON line per message, each line a raw BaseMessage model_dump that
    round-trips through langchain load — type, content, and ava_* kwargs all
    survive, SystemMessage and ToolMessages included (full-history snapshot)."""
    _patch_dump_enabled(monkeypatch, tmp_path)
    messages = _sample_messages()
    path = history_dump.dump_history(messages, 1)
    assert path is not None

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(messages)

    replayed: list[BaseMessage] = []
    for line in lines:
        raw = json.loads(line)
        assert isinstance(raw, dict)
        # The documented replay recipe: wrap the raw fields in the langchain
        # type/data envelope and let messages_from_dict rebuild the message.
        replayed.extend(
            messages_from_dict(
                [
                    {
                        "type": raw["type"],
                        "data": {k: v for k, v in raw.items() if k != "type"},
                    }
                ]
            )
        )

    assert [type(m).__name__ for m in replayed] == [type(m).__name__ for m in messages]
    for original, restored in zip(messages, replayed, strict=True):
        assert restored.content == original.content  # pyright: ignore[reportUnknownMemberType]
        assert restored.additional_kwargs == original.additional_kwargs  # pyright: ignore[reportUnknownMemberType]
    # The tool round-trip survived: tool_call ids match their ToolMessage.
    tool_msg = replayed[3]
    assert isinstance(tool_msg, ToolMessage)
    assert tool_msg.tool_call_id == "c1"


def test_dump_rotates_to_newest_keep(monkeypatch: pytest.MonkeyPatch, tmp_path: Any):
    """After each write only the newest `history_dump_keep` dumps remain — disk
    growth is bounded (each dump is a full conversation snapshot)."""
    _patch_clock(monkeypatch)
    _patch_dump_enabled(monkeypatch, tmp_path, keep=2)
    for _ in range(4):
        history_dump.dump_history(_sample_messages(), 1)
    dump_dir = tmp_path / "workspace" / "compact_dumps"
    dumps = sorted(p.name for p in dump_dir.glob("*.jsonl"))
    assert len(dumps) == 2
    assert dumps == sorted(dumps)  # lexicographic == chronological


def test_dump_failure_is_best_effort(monkeypatch: pytest.MonkeyPatch, tmp_path: Any):
    """A write failure must never raise — the dump is forensics, the compaction
    is the critical path. Returns None and the caller injects no note."""
    _patch_dump_enabled(monkeypatch, tmp_path)

    def _boom(aid: int) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr("agent.history_dump.workspace_dir", _boom)
    assert history_dump.dump_history(_sample_messages(), 1) is None


# ── auto-compact path: note injection position ──


async def test_auto_compact_injects_dump_note_after_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
):
    """Enabled → the auto-compact hook dumps the pre-compact history and parks
    the note in the fresh-context tail AFTER the summary. The live `messages`
    update carries only the REMOVE_ALL sentinel — the note is never in the
    pre-compact channel and never between an AIMessage and its ToolMessage."""
    ws = _patch_dump_enabled(monkeypatch, tmp_path)
    _patch_compact_config(monkeypatch)
    state = _over_threshold_state()

    result = await auto_compact_before_llm(
        state, _runtime_with_llm(_fake_llm(_LONG_SUMMARY)), _fake_config()
    )
    assert result is not None

    tail = _compact_tail(result)
    assert len(tail) == 2
    assert tail[0].content == compose_summary_message(_LONG_SUMMARY)  # pyright: ignore[reportUnknownMemberType]
    note = tail[1]
    assert note.additional_kwargs["ava_msg_type"] == "system_note"  # pyright: ignore[reportUnknownMemberType]
    assert note.additional_kwargs["ava_note_tag"] == "history_dump"  # pyright: ignore[reportUnknownMemberType]
    # The note names the real, existing dump.
    (dump_path,) = (ws / "compact_dumps").glob("*.jsonl")
    assert dump_path.name in note.content  # pyright: ignore[reportUnknownMemberType]
    assert dump_path.exists()


async def test_auto_compact_no_note_when_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Any):
    """Config off (default) → the auto-compact transition is exactly today's:
    tail = the summary alone, no note (and no dump file)."""
    _patch_dump_enabled(monkeypatch, tmp_path, enabled=False)
    _patch_compact_config(monkeypatch)
    state = _over_threshold_state()

    result = await auto_compact_before_llm(
        state, _runtime_with_llm(_fake_llm(_LONG_SUMMARY)), _fake_config()
    )
    assert result is not None
    tail = _compact_tail(result)
    assert [m.content for m in tail] == [compose_summary_message(_LONG_SUMMARY)]  # pyright: ignore[reportUnknownMemberType]


async def test_auto_compact_proceeds_when_dump_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
):
    """A dump failure degrades to today's behavior: compaction still applies,
    no note is injected."""
    _patch_dump_enabled(monkeypatch, tmp_path)
    _patch_compact_config(monkeypatch)

    def _boom(aid: int) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr("agent.history_dump.workspace_dir", _boom)
    state = _over_threshold_state()

    result = await auto_compact_before_llm(
        state, _runtime_with_llm(_fake_llm(_LONG_SUMMARY)), _fake_config()
    )
    assert result is not None
    tail = _compact_tail(result)
    assert [m.content for m in tail] == [compose_summary_message(_LONG_SUMMARY)]  # pyright: ignore[reportUnknownMemberType]


# ── claim path (agent-/user-triggered compact): note injection position ──


async def test_claim_compact_summary_parks_dump_note_after_summary(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
):
    """Enabled → the claim-node compact path (agent-written summary) dumps the
    pre-compact state.messages and parks the note after the summary in the
    fresh-context tail. The messages update is the REMOVE_ALL sentinel alone:
    no note, no AIMessage — nothing that could sit between a tool_use and its
    tool_result on the wire."""
    _patch_dump_enabled(monkeypatch, tmp_path)
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "agent-written summary", "compact_summary")
    state = AgentState(
        messages=[
            SystemMessage(content="<sys>"),
            *(HumanMessage(content=f"old-{i}") for i in range(8)),
        ],
        halted=False,
    )

    cmd = await claim_node(
        state,
        _make_runtime(
            ops_pool=aops_pool,
            llm=_fake_llm("LLM should not be called"),
        ),
        _config(tid),
    )

    tail = _compact_tail(cmd.update)
    assert tail[0].content == compose_summary_message("agent-written summary")  # pyright: ignore[reportUnknownMemberType]
    assert len(tail) == 2  # summary + dump note, nothing else
    note = tail[-1]
    assert note.additional_kwargs["ava_msg_type"] == "system_note"  # pyright: ignore[reportUnknownMemberType]
    assert note.additional_kwargs["ava_note_tag"] == "history_dump"  # pyright: ignore[reportUnknownMemberType]
    # Fresh-context laydown (init_context): [SystemMessage, *notes, *tail] —
    # the note's neighbors are the summary and the next LLM output, never a
    # tool_use/tool_result pair.
    for m in tail:
        assert not (isinstance(m, AIMessage) and m.tool_calls)


async def test_claim_compact_summary_no_note_when_disabled(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
):
    """Config off → claim-path compact tail is exactly today's (summary only)."""
    _patch_dump_enabled(monkeypatch, tmp_path, enabled=False)
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "agent-written summary", "compact_summary")
    state = AgentState(
        messages=[SystemMessage(content="<sys>"), HumanMessage(content="old")],
        halted=False,
    )

    cmd = await claim_node(
        state,
        _make_runtime(
            ops_pool=aops_pool,
            llm=_fake_llm("LLM should not be called"),
        ),
        _config(tid),
    )

    tail = _compact_tail(cmd.update)
    assert [m.content for m in tail] == [  # pyright: ignore[reportUnknownMemberType]
        compose_summary_message("agent-written summary")
    ]
