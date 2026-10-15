import json
from collections.abc import Mapping
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.messages.modifier import RemoveMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.hooks.compact import (
    COMPACT_MAX_ATTEMPTS,
    CompactionFailedError,
    auto_compact_for_llm,
    compose_summary_message,
)
from agent.hooks import Hook
from agent.state import AgentState, CompactState
from base.agents.context import AvaContext
from base.clock import Clock
from base.config import settings
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices, ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.context_budget import ContextBudget
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import EMPTY


def read_agent_field(field: str):
    return getattr(settings.agent, field)


def pinned_slices(pins: Mapping[str, Any] | None = None) -> AgentSlices:
    return AgentSlices.resolve(pins, default_reader=defaults())


def compact_tail(update: Any) -> list[AnyMessage]:
    msgs = update["messages"]
    assert len(msgs) == 1, f"expected the sentinel alone, got {len(msgs)} messages"
    assert isinstance(msgs[0], RemoveMessage)
    assert msgs[0].id == REMOVE_ALL_MESSAGES
    return update["context_reset"].tail


def patch_compact_config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    auto_compact_tokens: int = 800_000,
    compact_reminder_tokens: int = 600_000,
) -> None:
    budget = ContextBudget(
        max_context_tokens=1_000_000,
        soft_compact_tokens=compact_reminder_tokens,
        hard_compact_tokens=auto_compact_tokens,
    )

    def fixed_budget(
        _model: str, _overrides: ModelOverrides, *, catalog: ModelCatalog
    ) -> ContextBudget:
        return budget

    monkeypatch.setattr("agent.graph.hooks.compact.resolve_context_budget", fixed_budget)


def make_fake_llm(summary_text: str = "fake summary", *, response: AIMessage | None = None) -> Any:
    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(
        return_value=response if response is not None else AIMessage(content=summary_text)
    )
    return llm


def compaction_ainvoke(llm: Any) -> AsyncMock:
    return llm.bind_tools.return_value.ainvoke


LONG_SUMMARY = "## Requests\nfollow the template. " * 60


def _fake_llm_seq(*summaries: str) -> Any:
    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(
        side_effect=[AIMessage(content=s) for s in summaries]
    )
    return llm


def runtime_with_llm(llm: Any, database_gate: ProcessDbGate) -> Runtime[AvaContext]:
    ctx = AvaContext(
        ops_pool=None,
        llm=llm,
        event_publisher=MagicMock(),
        agent=pinned_slices(),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
    )
    return Runtime(context=ctx)


def fake_config() -> RunnableConfig:
    return {"configurable": {"thread_id": "1"}}


def _over_threshold_state() -> AgentState:
    return AgentState(
        messages=[
            SystemMessage(content="<sys>"),
            *(HumanMessage(content="x" * 1000) for _ in range(5)),
        ],
        halted=False,
    )


async def test_auto_compact_triggers_on_real_input_tokens_not_chars(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
):
    patch_compact_config(monkeypatch, auto_compact_tokens=200_000)
    state = AgentState(
        messages=[
            SystemMessage(content="<sys>"),
            HumanMessage(content="hi"),
            AIMessage(
                content="ok",
                usage_metadata={
                    "input_tokens": 300_000,
                    "output_tokens": 5,
                    "total_tokens": 300_005,
                },
            ),
            HumanMessage(content="more"),
        ],
        halted=False,
    )
    result = await auto_compact_for_llm(
        state,
        runtime_with_llm(make_fake_llm(LONG_SUMMARY), database_gate=database_gate),
        fake_config(),
    )
    assert result is not None


async def test_auto_compact_skips_when_input_tokens_below_ceiling_despite_chars(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
):
    patch_compact_config(monkeypatch, auto_compact_tokens=200_000)
    state = AgentState(
        messages=[
            SystemMessage(content="<sys>"),
            *(HumanMessage(content="x" * 100_000) for _ in range(20)),
            AIMessage(
                content="ok",
                usage_metadata={"input_tokens": 50_000, "output_tokens": 5, "total_tokens": 50_005},
            ),
        ],
        halted=False,
    )
    result = await auto_compact_for_llm(
        state, runtime_with_llm(make_fake_llm(), database_gate=database_gate), fake_config()
    )
    assert result is None


async def test_auto_compact_falls_back_to_chars_before_first_call(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
):
    patch_compact_config(monkeypatch, auto_compact_tokens=100)
    state = AgentState(
        messages=[
            SystemMessage(content="<sys>"),
            HumanMessage(content="x" * 4000),
        ],
        halted=False,
    )
    result = await auto_compact_for_llm(
        state,
        runtime_with_llm(make_fake_llm(LONG_SUMMARY), database_gate=database_gate),
        fake_config(),
    )
    assert result is not None


async def test_auto_compact_hook_returns_none_when_under_threshold(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
):
    patch_compact_config(monkeypatch, auto_compact_tokens=1_000_000)
    state = AgentState(messages=[HumanMessage(content="hi" * 100)], halted=False)
    result = await auto_compact_for_llm(
        state, runtime_with_llm(make_fake_llm(), database_gate=database_gate), fake_config()
    )
    assert result is None


async def test_auto_compact_hook_clears_history_and_parks_summary(
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
    database_gate: ProcessDbGate,
):
    patch_compact_config(monkeypatch, auto_compact_tokens=1)
    state = _over_threshold_state()

    fake_llm = make_fake_llm(LONG_SUMMARY)
    result = await auto_compact_for_llm(
        state, runtime_with_llm(fake_llm, database_gate=database_gate), fake_config()
    )

    assert result is not None
    compaction_ainvoke(fake_llm).assert_called_once()
    new_msgs = result["messages"]
    assert len(new_msgs) == 1
    assert isinstance(new_msgs[0], RemoveMessage)
    assert new_msgs[0].id == REMOVE_ALL_MESSAGES

    reset = result["context_reset"]
    assert [m.content for m in reset.tail] == [compose_summary_message(LONG_SUMMARY)]
    assert isinstance(reset.tail[0], HumanMessage)
    assert reset.resume == "claim"
    assert result["goto"] == "init_context"
    [monitoring] = [
        record
        for record in loguru_records
        if record["extra"].get("event") == "compaction_completed"
    ]
    assert monitoring["extra"] | {"msg": None} == {
        "agent_id": 1,
        "compact_kind": "auto",
        "reminder_shown": False,
        "compactions": 1,
        "history_chars": 5000,
        "summary_chars": len(LONG_SUMMARY),
        "summary_history_ratio": pytest.approx(len(LONG_SUMMARY) / 5000),
        "event": "compaction_completed",
        "msg": None,
    }


async def test_auto_compact_hook_skips_when_no_conversation(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
):
    patch_compact_config(monkeypatch, auto_compact_tokens=1)
    state = AgentState(messages=[SystemMessage(content="x" * 100)], halted=False)

    llm = make_fake_llm()
    result = await auto_compact_for_llm(
        state, runtime_with_llm(llm, database_gate=database_gate), fake_config()
    )
    assert result is None
    compaction_ainvoke(llm).assert_not_called()


async def test_auto_compact_hook_raises_when_summary_empty_every_attempt(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
):
    patch_compact_config(monkeypatch, auto_compact_tokens=1)
    llm = make_fake_llm(summary_text="")
    state = _over_threshold_state()

    with pytest.raises(CompactionFailedError, match="no usable summary across"):
        await auto_compact_for_llm(
            state, runtime_with_llm(llm, database_gate=database_gate), fake_config()
        )
    assert compaction_ainvoke(llm).call_count == COMPACT_MAX_ATTEMPTS


async def test_auto_compact_hook_raises_when_summary_short_every_attempt(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
):
    patch_compact_config(monkeypatch, auto_compact_tokens=1)
    llm = make_fake_llm("too short")
    state = _over_threshold_state()
    publisher = MagicMock()
    ctx = AvaContext(
        ops_pool=None,
        llm=llm,
        event_publisher=publisher,
        agent=pinned_slices(),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
    )

    with pytest.raises(CompactionFailedError, match="no usable summary across"):
        await auto_compact_for_llm(state, Runtime(context=ctx), fake_config())

    assert compaction_ainvoke(llm).call_count == COMPACT_MAX_ATTEMPTS
    events = [json.loads(c.args[0]) for c in publisher.emit.call_args_list]
    assert [e["role"] for e in events] == ["compact_started", "compact_finished"]
    assert events[1]["status"] == "failure"
    assert events[0]["compact_id"] == events[1]["compact_id"]


async def test_auto_compact_hook_retries_short_then_accepts_long(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
):
    patch_compact_config(monkeypatch, auto_compact_tokens=1)
    llm = _fake_llm_seq("too short", LONG_SUMMARY)
    state = _over_threshold_state()

    result = await auto_compact_for_llm(
        state, runtime_with_llm(llm, database_gate=database_gate), fake_config()
    )

    assert result is not None
    assert compaction_ainvoke(llm).call_count == 2
    assert result["context_reset"].tail[0].content == compose_summary_message(LONG_SUMMARY)


async def test_auto_compact_hook_emits_compact_done_on_success(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
):
    patch_compact_config(monkeypatch, auto_compact_tokens=1)
    publisher = MagicMock()
    llm = make_fake_llm(LONG_SUMMARY)
    ctx = AvaContext(
        ops_pool=None,
        llm=llm,
        event_publisher=publisher,
        agent=pinned_slices(),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
    )
    state = _over_threshold_state()

    result = await auto_compact_for_llm(state, Runtime(context=ctx), fake_config())

    assert result is not None
    events = [json.loads(c.args[0]) for c in publisher.emit.call_args_list]
    assert [e["role"] for e in events] == [
        "compact_started",
        "compact_done",
        "compact_finished",
    ]
    started, finished = events[0], events[2]
    assert started["mode"] == "auto"
    assert finished["status"] == "success"
    assert started["compact_id"] == finished["compact_id"]
    assert started["started_at"] and finished["finished_at"]
    tail = result["context_reset"].tail
    assert tail[0].additional_kwargs["ava_compact_id"] == started["compact_id"]


@pytest.fixture
def ava_compact_loaded() -> tuple[type[AgentState], Hook]:
    from agent.graph.hooks.compact import compact_reminder
    from agent.state import build_agent_state

    return build_agent_state(EMPTY), compact_reminder


async def test_compact_reminder_passthrough_none(
    ava_compact_loaded: tuple[type[AgentState], Hook],
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
):
    state_cls, wrap_fn = ava_compact_loaded

    patch_compact_config(monkeypatch, auto_compact_tokens=1_000_000)
    state = state_cls(
        messages=[HumanMessage(content="hi")],
        halted=False,
        compact=CompactState(version=0),
    )
    result = await wrap_fn(
        state, runtime_with_llm(make_fake_llm(), database_gate=database_gate), fake_config()
    )
    assert result is None


def _over_threshold_messages() -> list[AnyMessage]:
    return [
        SystemMessage(content="<sys>"),
        *(HumanMessage(content="x" * 1000) for _ in range(5)),
    ]


async def test_compact_reminder_zero_to_one(
    ava_compact_loaded: tuple[type[AgentState], object],
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
):
    state_cls, _ = ava_compact_loaded
    wrap_fn = auto_compact_for_llm

    patch_compact_config(monkeypatch, auto_compact_tokens=1)
    state = state_cls(
        messages=_over_threshold_messages(), halted=False, compact=CompactState(version=0)
    )
    result = await wrap_fn(
        state,
        runtime_with_llm(make_fake_llm(LONG_SUMMARY), database_gate=database_gate),
        fake_config(),
    )
    assert result is not None
    assert result["compact"].version == 1
    assert "messages" in result


async def test_compact_reminder_increments_from_existing(
    ava_compact_loaded: tuple[type[AgentState], object],
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
):
    state_cls, _ = ava_compact_loaded
    wrap_fn = auto_compact_for_llm

    patch_compact_config(monkeypatch, auto_compact_tokens=1)
    state = state_cls(
        messages=_over_threshold_messages(), halted=False, compact=CompactState(version=5)
    )
    result = await wrap_fn(
        state,
        runtime_with_llm(make_fake_llm(LONG_SUMMARY), database_gate=database_gate),
        fake_config(),
    )
    assert result is not None
    assert result["compact"].version == 6


def reminder_state(state_cls, *, version=0, shown=False, seen=0, messages=None):
    return state_cls(
        messages=_over_threshold_messages() if messages is None else messages,
        halted=False,
        compact=CompactState(version=version, reminder_shown=shown, reminder_seen_version=seen),
    )


COMPACT_SECTIONS = (
    "Requests",
    "Progress",
    "In flight",
    "Dead ends",
    "Pitfalls",
    "Verbatim tail",
)


def insert_compact_summary(db: psycopg.Connection, tid: int, content: str) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind) "
            "VALUES (%s, %s, 'compact_summary')",
            (tid, content),
        )
    db.commit()


def make_runtime(
    ops_pool: AsyncConnectionPool | None = None,
    llm: BaseChatModel | None = None,
    *,
    database_gate: ProcessDbGate,
) -> Runtime[AvaContext]:
    if ops_pool is None:
        ops_pool = AsyncMock()
    if llm is None:
        llm = AsyncMock()
    ctx = AvaContext(
        ops_pool=ops_pool,
        llm=llm,
        event_publisher=MagicMock(),
        agent=pinned_slices(),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
    )
    from langgraph.runtime import Runtime

    return Runtime(context=ctx)


def _config(tid: int) -> RunnableConfig:
    return {"configurable": {"thread_id": str(tid)}}
