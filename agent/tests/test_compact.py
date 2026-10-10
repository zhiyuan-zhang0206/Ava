"""Compact tool tests.

`agent/hooks/compact.py` exports:
- `generate_summary(messages, llm) -> summary`: pure function (called by claim node processing
  inbound kind compact_request; also called internally by auto-compact hook). Request =
  whole conversation reused byte-for-byte (original SystemMessage + all content + same
  bind_tools(execute_code)) + a final COMPACTION_INSTRUCTION — hits prefix cache.
  Returns summary text (response.text; block content extracts text blocks; thinking excluded).
  LLM returns empty text (tool_use-only blocks, empty string) → RuntimeError; conversation empty → ValueError.
- `auto_compact_for_llm` built-in before_llm hook — when over threshold runs generate_summary,
  replaces entire history with `[system, summary]` (leaves no raw tail). Short summary retries ≤
  COMPACT_MAX_ATTEMPTS, still short then fail-fast. On success emits CompactDone.

After compact, history = `[RemoveAll, SystemMessage, HumanMessage(header+summary)]` —
summary is complete memory; framework no longer appends any original messages (no tail).
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.modifier import RemoveMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime

from agent.hooks.compact import (
    COMPACT_MAX_ATTEMPTS,
    COMPACTION_INSTRUCTION,
    CompactionFailedError,
    auto_compact_for_llm,
    compose_summary_message,
    generate_summary,
)
from agent.llm import execute_code
from agent.state import AgentState, CompactState
from base.agents.context import AvaContext
from base.db import Database
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices, ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.context_budget import ContextBudget
from base.lm.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import EMPTY


def _compact_tail(update: Any) -> list[AnyMessage]:
    """Assert the transport every compaction now shares — the window is cleared
    and rebuilding the standing head is handed to `init_context` — and return the
    parked tail, which is what the compaction itself decided.

    A compaction no longer emits a replacement window: `messages` carries the
    REMOVE_ALL sentinel alone, and what belongs *behind* the head — the summary
    — rides in `context_reset`. A compact is a clean wipe: chats co-batched
    with the compact request are re-delivered as pending inbounds, never parked
    here.
    """
    msgs = update["messages"]
    assert len(msgs) == 1, f"expected the sentinel alone, got {len(msgs)} messages"
    assert isinstance(msgs[0], RemoveMessage)
    assert msgs[0].id == REMOVE_ALL_MESSAGES
    return update["context_reset"].tail


def _patch_compact_config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    auto_compact_tokens: int = 800_000,
    compact_reminder_tokens: int = 600_000,
) -> None:
    """Pin the compact thresholds regardless of model, by replacing
    `resolve_context_budget` in the compact module with a fixed budget. These
    tests use synthetic messages with no usage_metadata, so context occupancy is
    the chars/4 fallback and the kwargs are the absolute token thresholds the
    gate compares against — `auto_compact_tokens` = hard (force) ceiling,
    `compact_reminder_tokens` = soft (reminder) threshold (the kwarg names keep
    their historical meaning)."""
    budget = ContextBudget(
        max_context_tokens=1_000_000,
        soft_compact_tokens=compact_reminder_tokens,
        hard_compact_tokens=auto_compact_tokens,
    )

    def fixed_budget(
        _model: str, _overrides: ModelOverrides, *, catalog: ModelCatalog
    ) -> ContextBudget:
        return budget

    monkeypatch.setattr("agent.hooks.compact.resolve_context_budget", fixed_budget)


def _fake_llm(summary_text: str = "fake summary", *, response: AIMessage | None = None) -> Any:
    """Construct a mock LLM — bind_tools(...).ainvoke returns AIMessage(content=summary_text)
    (or explicitly passed response), matching generate_summary's call shape (same tool binding
    as main llm node)."""
    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(
        return_value=response if response is not None else AIMessage(content=summary_text)
    )
    return llm


def _compaction_ainvoke(llm: Any) -> AsyncMock:
    """The ainvoke mock that compaction actually uses on the fake LLM (after bind_tools)."""
    return llm.bind_tools.return_value.ainvoke


# A summary that clears COMPACT_MIN_SUMMARY_CHARS — the auto-compact hook retries
# anything shorter, so tests exercising the success path must return one this long.
_LONG_SUMMARY = "## Requests\nfollow the template. " * 60


def _fake_llm_seq(*summaries: str) -> Any:
    """A mock LLM whose successive compaction calls return each `summaries` text
    in turn (AIMessage content) — lets a test drive the auto-compact retry loop
    across attempts (e.g. short then long). Raising past the last entry surfaces
    an over-call as a test failure rather than reusing the final response."""
    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(
        side_effect=[AIMessage(content=s) for s in summaries]
    )
    return llm


def _runtime_with_llm(llm: Any) -> Runtime[AvaContext]:
    # These unit tests have no DB. ops_pool=None is the container-mode value:
    # the post-compact checkpoint trim treats it as a no-op (real-pool trimming is covered by
    # base/agents/history/tests/test_checkpoint_cleanup.py and the aops_pool compact tests below).
    ctx = AvaContext(
        ops_pool=None,
        llm=llm,
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(),
        db=Database.from_settings(),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
    )
    return Runtime(context=ctx)


def _fake_config() -> RunnableConfig:
    """Minimal config with agent_id=1 — hook's three-argument signature requires passing config."""
    return {"configurable": {"thread_id": "1"}}


# --- generate_summary tests ---


async def test_generate_summary_returns_summary():
    """generate_summary returns summary text (from LLM), no longer returns tail."""
    msgs: list[AnyMessage] = [HumanMessage(content=f"msg{i}") for i in range(8)]

    llm = _fake_llm(summary_text="a synthetic summary")
    summary = await generate_summary(
        msgs, llm, AgentSlices.resolve(), catalog=build_model_catalog()
    )

    assert summary == "a synthetic summary"


async def test_generate_summary_remembers_the_call_that_produced_it() -> None:
    """The summary carries the compaction call's provider input, model and instruction size --
    what the boundary checkpoint stores so the sealed segment's tail can be priced."""
    from agent.hooks.compact_anchor import closing_of
    from base.agents.history.closing_request import ClosingRequest

    msgs: list[AnyMessage] = [HumanMessage(content=f"msg{i}") for i in range(8)]
    response = AIMessage(
        content="a synthetic summary",
        usage_metadata={"input_tokens": 4321, "output_tokens": 9, "total_tokens": 4330},
        response_metadata={"model_name": "m1"},
    )
    summary = await generate_summary(
        msgs, _fake_llm(response=response), AgentSlices.resolve(), catalog=build_model_catalog()
    )

    closing = closing_of(summary)
    assert closing is not None
    assert (closing.input_tokens, closing.model) == (4321, "m1")
    assert closing.extra_tokens > 0  # the compaction instruction message
    assert closing == ClosingRequest(4321, closing.extra_tokens, "m1")
    assert summary == "a synthetic summary"  # still the plain text everywhere else

    bare = await generate_summary(
        msgs, _fake_llm("no usage"), AgentSlices.resolve(), catalog=build_model_catalog()
    )
    assert closing_of(bare) is None
    assert closing_of("an agent-written summary") is None


async def test_stamp_compact_boundary_writes_the_closing_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.hooks import compact
    from base.agents.history.closing_request import ClosingRequest

    seen: list[ClosingRequest | None] = []

    async def fake_mark(_pool: Any, _thread: str, *, closing: ClosingRequest | None = None) -> str:
        seen.append(closing)
        return "boundary"

    async def no_wait(*_args: Any) -> None:
        return None

    monkeypatch.setattr(compact, "mark_compact_boundary", fake_mark)
    monkeypatch.setattr(compact, "await_snapshot", no_wait)
    closing = ClosingRequest(100, 5, "m1")
    await compact.stamp_compact_boundary(MagicMock(), 1, None, closing=closing)
    await compact.stamp_compact_boundary(MagicMock(), 1)
    assert seen == [closing, None]


async def test_generate_summary_emits_agent_billing_span(
    model_catalog: ModelCatalog,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
) -> None:
    """A completed compaction call records its provider usage in the ledger.

    The regression this catches is removing billing emission from the compact
    path, which is the largest individual agent provider call.
    """
    from opentelemetry import trace as otel_trace

    from base.telemetry import tracing as tracing_mod

    class _Span:
        def __init__(self, name: str) -> None:
            self.name = name
            self.attributes: dict[str, Any] = {}

        def set_attribute(self, key: str, value: Any) -> None:
            self.attributes[key] = value

        def end(self) -> None:
            pass

    class _Tracer:
        def __init__(self) -> None:
            self.spans: list[_Span] = []

        def start_span(self, _name: str, *, start_time: int | None = None) -> _Span:
            span = _Span(_name)
            self.spans.append(span)
            return span

    tracer = _Tracer()

    # The summary's billing behavior requires a known provider; a bare test
    # process has no installed provider plugins and must declare that input.
    def vendor_of_model(_model: str, *, catalog: ModelCatalog) -> str:
        assert catalog is model_catalog
        return "deepseek"

    monkeypatch.setattr("base.lm.pricing.billing.vendor_of_model", vendor_of_model)
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr(tracing_mod, "is_initialized", lambda: True)
    monkeypatch.setattr(otel_trace, "get_tracer", lambda _name: tracer)  # pyright: ignore[reportUnknownArgumentType]
    response = AIMessage(
        content="a complete summary",
        usage_metadata={
            "input_tokens": 1_000,
            "output_tokens": 100,
            "total_tokens": 1_100,
            "input_token_details": {"cache_read": 800},
        },
    )
    llm = _fake_llm(response=response)
    llm.model_name = "deepseek-flash"
    slices = AgentSlices.resolve({"llm_model": "deepseek-flash"})

    assert (
        await generate_summary(
            [HumanMessage(content="conversation")], llm, slices, catalog=model_catalog
        )
        == "a complete summary"
    )

    assert len(tracer.spans) == 1
    span = tracer.spans[0]
    assert span.name == "ava.billing.call"
    assert span.attributes["ava.billing.model"] == "deepseek-flash"
    assert span.attributes["ava.billing.vendor"] == "deepseek"
    assert span.attributes["ava.billing.usage_kind"] == "agent"
    assert span.attributes["ava.billing.tokens_in"] == 1_000
    assert span.attributes["ava.billing.tokens_out"] == 100
    assert span.attributes["ava.billing.cache_read_tokens"] == 800
    [record] = [record for record in loguru_records if record["extra"].get("event") == "llm_usage"]
    assert record["extra"]["usage_kind"] == "agent"


async def test_generate_summary_includes_whole_conversation():
    """Request = [*whole conversation, HumanMessage(COMPACTION_INSTRUCTION)] — no more hold-out
    tail, model sees every message (including ToolMessage), instruction enters as appended message."""
    ai = AIMessage(
        content="", tool_calls=[{"name": "execute_code", "args": {"code": "x"}, "id": "c1"}]
    )
    convo: list[AnyMessage] = [
        HumanMessage(content="q"),
        ai,
        ToolMessage(content="out", tool_call_id="c1"),
        AIMessage(content="done"),
    ]

    llm = _fake_llm()
    await generate_summary(convo, llm, AgentSlices.resolve(), catalog=build_model_catalog())

    [call] = _compaction_ainvoke(llm).call_args_list
    [llm_input] = call.args
    assert llm_input[:-1] == convo  # whole conversation, no hold-out
    assert isinstance(llm_input[-1], HumanMessage)
    assert llm_input[-1].content.startswith(COMPACTION_INSTRUCTION)


async def test_generate_summary_reuses_conversation_prefix_for_cache():
    """compaction request = main conversation + a final instruction — original SystemMessage kept in place (not replaced with compaction-specific system prompt), bind_tools same as main llm node (tools field participates in prefix rendering); the whole conversation is the prefix of the previous main request, combined they hit the backend prefix cache."""
    sys_msg = SystemMessage(content="<real agent sys prompt>")
    content: list[AnyMessage] = [HumanMessage(content=f"m-{i}") for i in range(5)]

    llm = _fake_llm()
    await generate_summary(
        [sys_msg, *content], llm, AgentSlices.resolve(), catalog=build_model_catalog()
    )

    llm.bind_tools.assert_called_once_with([execute_code])
    [call] = _compaction_ainvoke(llm).call_args_list
    [llm_input] = call.args
    assert llm_input[0] is sys_msg  # original object in-place — same byte-for-byte prefix
    assert llm_input[:-1] == [sys_msg, *content]
    assert llm_input[-1].content.startswith(COMPACTION_INSTRUCTION)


async def test_generate_summary_raises_on_empty_llm_text():
    """LLM returns empty text (e.g., defying instruction only gives tool_call) → RuntimeError —
    empty summary cannot be used to replace history."""
    msgs: list[AnyMessage] = [HumanMessage(content=f"m{i}") for i in range(3)]

    with pytest.raises(RuntimeError, match="no text"):
        await generate_summary(
            msgs, _fake_llm(summary_text=""), AgentSlices.resolve(), catalog=build_model_catalog()
        )


async def test_generate_summary_extracts_text_from_block_content():
    """Production provider (thinking enabled) returns block content [thinking, text] —
    summary must be the text block content, not the whole list's repr."""
    msgs: list[AnyMessage] = [HumanMessage(content=f"m{i}") for i in range(3)]
    block_response = AIMessage(
        content=[
            {"type": "thinking", "thinking": "internal reasoning"},
            {"type": "text", "text": "the real summary"},
        ]
    )

    llm = _fake_llm(response=block_response)
    summary = await generate_summary(
        msgs, llm, AgentSlices.resolve(), catalog=build_model_catalog()
    )
    assert summary == "the real summary"


async def test_generate_summary_raises_on_tool_use_only_block_content():
    """Defying instruction only returns tool_use block (no text block) → response.text is empty →
    RuntimeError — cannot let block list repr become summary."""
    msgs: list[AnyMessage] = [HumanMessage(content=f"m{i}") for i in range(3)]
    tool_only = AIMessage(
        content=[{"type": "tool_use", "id": "c1", "name": "execute_code", "input": {"code": "x"}}]
    )

    with pytest.raises(RuntimeError, match="no text"):
        await generate_summary(
            msgs,
            _fake_llm(response=tool_only),
            AgentSlices.resolve(),
            catalog=build_model_catalog(),
        )


async def test_generate_summary_raises_on_empty_conversation():
    """Only SystemMessage (no conversation) → ValueError — nothing to summarize."""
    with pytest.raises(ValueError, match="empty"):
        await generate_summary(
            [SystemMessage(content="<sys>")],
            _fake_llm(),
            AgentSlices.resolve(),
            catalog=build_model_catalog(),
        )


# --- auto_compact_for_llm hook tests ---


def _over_threshold_state() -> AgentState:
    return AgentState(
        messages=[
            SystemMessage(content="<sys>"),
            *(HumanMessage(content="x" * 1000) for _ in range(5)),
        ],
        halted=False,
    )


async def test_auto_compact_triggers_on_real_input_tokens_not_chars(
    monkeypatch: pytest.MonkeyPatch,
):
    """Option Y: occupancy is the last AIMessage's real input_tokens, not chars/4.
    A short conversation (tiny chars/4) whose last LLM call measured a large
    input_tokens still forces compaction — the trigger reads the provider truth."""
    _patch_compact_config(monkeypatch, auto_compact_tokens=200_000)
    state = AgentState(
        messages=[
            SystemMessage(content="<sys>"),
            HumanMessage(content="hi"),  # a few chars: chars/4 is far under 200K
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
        state, _runtime_with_llm(_fake_llm(_LONG_SUMMARY)), _fake_config()
    )
    assert result is not None  # 300K measured input_tokens > 200K ceiling -> compact


async def test_auto_compact_skips_when_input_tokens_below_ceiling_despite_chars(
    monkeypatch: pytest.MonkeyPatch,
):
    """The inverse: a huge chars/4 footprint but a small measured input_tokens
    does NOT force compaction — chars/4 no longer drives the gate once a real
    measurement exists (the last AIMessage's usage wins)."""
    _patch_compact_config(monkeypatch, auto_compact_tokens=200_000)
    state = AgentState(
        messages=[
            SystemMessage(content="<sys>"),
            *(HumanMessage(content="x" * 100_000) for _ in range(20)),  # chars/4 ~ 500K
            AIMessage(
                content="ok",
                usage_metadata={"input_tokens": 50_000, "output_tokens": 5, "total_tokens": 50_005},
            ),
        ],
        halted=False,
    )
    result = await auto_compact_for_llm(state, _runtime_with_llm(_fake_llm()), _fake_config())
    assert result is None  # 50K measured < 200K ceiling, though chars/4 is far over


async def test_auto_compact_falls_back_to_chars_before_first_call(monkeypatch: pytest.MonkeyPatch):
    """Before any LLM call completes (no AIMessage with usage), occupancy falls
    back to the chars/4 estimate so an oversized first inbound still triggers."""
    _patch_compact_config(monkeypatch, auto_compact_tokens=100)
    state = AgentState(
        messages=[
            SystemMessage(content="<sys>"),
            HumanMessage(content="x" * 4000),
        ],  # chars/4 = 1000
        halted=False,
    )
    result = await auto_compact_for_llm(
        state, _runtime_with_llm(_fake_llm(_LONG_SUMMARY)), _fake_config()
    )
    assert result is not None  # 1000 chars/4 estimate > 100 ceiling -> compact


async def test_auto_compact_hook_returns_none_when_under_threshold(monkeypatch: pytest.MonkeyPatch):
    """token estimate ≤ threshold → hook returns None, no-op pass-through to llm."""
    _patch_compact_config(monkeypatch, auto_compact_tokens=1_000_000)
    state = AgentState(messages=[HumanMessage(content="hi" * 100)], halted=False)
    result = await auto_compact_for_llm(state, _runtime_with_llm(_fake_llm()), _fake_config())
    assert result is None


async def test_auto_compact_hook_clears_history_and_parks_summary(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
):
    """Over threshold → the hook empties the window and hands the rebuild to
    `init_context`: messages is the REMOVE_ALL sentinel alone, the summary is
    parked as the tail to lay down behind the standing head, and the turn is
    routed through that node before resuming at the LLM. The summary is the
    complete replacement memory — no raw tail survives."""
    _patch_compact_config(
        monkeypatch, auto_compact_tokens=1
    )  # deliberately lower so that any message exceeds
    state = _over_threshold_state()

    fake_llm = _fake_llm(_LONG_SUMMARY)
    result = await auto_compact_for_llm(state, _runtime_with_llm(fake_llm), _fake_config())

    assert result is not None
    _compaction_ainvoke(fake_llm).assert_called_once()
    new_msgs = result["messages"]
    assert len(new_msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(new_msgs[0], RemoveMessage)
    assert new_msgs[0].id == REMOVE_ALL_MESSAGES  # pyright: ignore[reportUnknownMemberType]

    reset = result["context_reset"]
    assert [m.content for m in reset.tail] == [compose_summary_message(_LONG_SUMMARY)]  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(reset.tail[0], HumanMessage)  # pyright: ignore[reportUnknownMemberType]
    assert reset.resume == "claim"  # pyright: ignore[reportUnknownMemberType]
    assert result["goto"] == "init_context"
    [monitoring] = [
        record
        for record in loguru_records
        if record["extra"].get("event") == "compaction_completed"
    ]
    assert monitoring["extra"] | {"msg": None} == {
        "agent_id": 1,
        "compact_kind": "auto",
        "compactions": 1,
        "history_chars": 5000,
        "summary_chars": len(_LONG_SUMMARY),
        "summary_history_ratio": pytest.approx(len(_LONG_SUMMARY) / 5000),  # pyright: ignore[reportUnknownMemberType]
        "event": "compaction_completed",
        "msg": None,
    }


async def test_auto_compact_hook_skips_when_no_conversation(monkeypatch: pytest.MonkeyPatch):
    """Over threshold but only SystemMessage (no conversation messages) → returns None silently pass through,
    and does not send any LLM request (pre-check before ainvoke)."""
    _patch_compact_config(monkeypatch, auto_compact_tokens=1)
    state = AgentState(messages=[SystemMessage(content="x" * 100)], halted=False)

    llm = _fake_llm()
    result = await auto_compact_for_llm(state, _runtime_with_llm(llm), _fake_config())
    assert result is None
    _compaction_ainvoke(llm).assert_not_called()


async def test_auto_compact_hook_raises_when_summary_empty_every_attempt(
    monkeypatch: pytest.MonkeyPatch,
):
    """LLM returns empty text every time (defying instruction only gives tool_call) → hook retries COMPACT_MAX_ATTEMPTS
    times then fail fast throws RuntimeError — never replace history with empty/non-summary."""
    _patch_compact_config(monkeypatch, auto_compact_tokens=1)
    llm = _fake_llm(summary_text="")  # same empty response on every call
    state = _over_threshold_state()

    with pytest.raises(CompactionFailedError, match="no usable summary across"):
        await auto_compact_for_llm(state, _runtime_with_llm(llm), _fake_config())
    assert _compaction_ainvoke(llm).call_count == COMPACT_MAX_ATTEMPTS


async def test_auto_compact_hook_raises_when_summary_short_every_attempt(
    monkeypatch: pytest.MonkeyPatch,
):
    """Every summary shorter than COMPACT_MIN_SUMMARY_CHARS (model ignores template) → after retries exhausted
    fail fast, rather than silently replacing history with short summary (agent-240 type incident)."""
    _patch_compact_config(monkeypatch, auto_compact_tokens=1)
    llm = _fake_llm("too short")  # 9 chars < floor, on every call
    state = _over_threshold_state()
    publisher = MagicMock()
    ctx = AvaContext(
        ops_pool=None,
        llm=llm,
        event_publisher=publisher,
        agent=AgentSlices.resolve(),
        db=Database.from_settings(),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
    )

    with pytest.raises(CompactionFailedError, match="no usable summary across"):
        await auto_compact_for_llm(state, Runtime(context=ctx), _fake_config())

    assert _compaction_ainvoke(llm).call_count == COMPACT_MAX_ATTEMPTS
    # Task #3323: the failed run still reaches its terminal signal — the live
    # block stops ticking instead of hanging.
    events = [json.loads(c.args[0]) for c in publisher.emit.call_args_list]
    assert [e["role"] for e in events] == ["compact_started", "compact_finished"]
    assert events[1]["status"] == "failure"
    assert events[0]["compact_id"] == events[1]["compact_id"]


async def test_auto_compact_hook_retries_short_then_accepts_long(monkeypatch: pytest.MonkeyPatch):
    """First summary short → retry; second reaches length → accepted and replaces history, no more retries."""
    _patch_compact_config(monkeypatch, auto_compact_tokens=1)
    llm = _fake_llm_seq("too short", _LONG_SUMMARY)  # short, then long
    state = _over_threshold_state()

    result = await auto_compact_for_llm(state, _runtime_with_llm(llm), _fake_config())

    assert result is not None
    assert _compaction_ainvoke(llm).call_count == 2  # stopped as soon as one cleared the floor
    assert result["context_reset"].tail[0].content == compose_summary_message(_LONG_SUMMARY)  # pyright: ignore[reportUnknownMemberType]


async def test_auto_compact_hook_emits_compact_done_on_success(monkeypatch: pytest.MonkeyPatch):
    """After successful compact on auto path, emit CompactDone (with this agent id), so UI refreshes.

    Task #3323: the same run also emits its live start/terminal pair
    (compact_started / compact_finished, same compact_id, status=success) and
    the summary message carries the durable anchor ava_compact_id."""
    _patch_compact_config(monkeypatch, auto_compact_tokens=1)
    publisher = MagicMock()
    llm = _fake_llm(_LONG_SUMMARY)
    ctx = AvaContext(
        ops_pool=None,
        llm=llm,
        event_publisher=publisher,
        agent=AgentSlices.resolve(),
        db=Database.from_settings(),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
    )
    state = _over_threshold_state()

    result = await auto_compact_for_llm(state, Runtime(context=ctx), _fake_config())

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
    tail = result["context_reset"].tail  # pyright: ignore[reportUnknownMemberType]
    assert tail[0].additional_kwargs["ava_compact_id"] == started["compact_id"]  # pyright: ignore[reportUnknownMemberType]


# --- _compact_reminder (plugins/ava_compact/plugin.py) tests ---
#
# This wrapper is the implementation side of the Layer 3 monotonic counter producer. Tests cover three things:
# 1. inner returns None → wrap returns None (no bump version, pass-through)
# 2. compact successful (cur=0) → wrap returns dict with messages + compact.version=1
# 3. compact successful (cur=N>0) → wrap returns dict with compact.version=N+1
# Implementation detail: wrap reads `state.compact.version` then model_copy the whole compact channel
# reads the compact channel, which lives on BaseAgentState (no plugin state needed).


@pytest.fixture
def _ava_compact_loaded():
    """Compact is built-in (Issue #1284). The wrapper function lives in
    agent.hooks.compact; state fields are on BaseAgentState. Returns
    (state_cls, wrap_fn) for tests to call.
    """
    from agent.hooks.compact import _compact_reminder
    from agent.state import build_agent_state

    return build_agent_state(EMPTY), _compact_reminder


async def test_compact_reminder_passthrough_none(
    _ava_compact_loaded, monkeypatch: pytest.MonkeyPatch
):
    """inner auto_compact_for_llm returns None (under threshold) → wrap also returns None."""
    state_cls, wrap_fn = _ava_compact_loaded

    _patch_compact_config(monkeypatch, auto_compact_tokens=1_000_000)
    state = state_cls(
        messages=[HumanMessage(content="hi")],
        halted=False,
        compact=CompactState(version=0),
    )
    result = await wrap_fn(state, _runtime_with_llm(_fake_llm()), _fake_config())
    assert result is None


def _over_threshold_messages() -> list[AnyMessage]:
    return [
        SystemMessage(content="<sys>"),
        *(HumanMessage(content="x" * 1000) for _ in range(5)),
    ]


async def test_compact_reminder_zero_to_one(
    _ava_compact_loaded: tuple[type[AgentState], object], monkeypatch: pytest.MonkeyPatch
):
    """First compact successful → compact.version increments from 0 to 1, dict contains messages."""
    state_cls, _ = _ava_compact_loaded
    wrap_fn = auto_compact_for_llm

    _patch_compact_config(monkeypatch, auto_compact_tokens=1)
    state = state_cls(
        messages=_over_threshold_messages(), halted=False, compact=CompactState(version=0)
    )
    result = await wrap_fn(state, _runtime_with_llm(_fake_llm(_LONG_SUMMARY)), _fake_config())
    assert result is not None
    assert result["compact"].version == 1  # pyright: ignore[reportUnknownMemberType]
    assert "messages" in result


async def test_compact_reminder_increments_from_existing(
    _ava_compact_loaded: tuple[type[AgentState], object], monkeypatch: pytest.MonkeyPatch
):
    """Not first compact: state already has compact.version=5 → wrap increments to 6."""
    state_cls, _ = _ava_compact_loaded
    wrap_fn = auto_compact_for_llm

    _patch_compact_config(monkeypatch, auto_compact_tokens=1)
    state = state_cls(
        messages=_over_threshold_messages(), halted=False, compact=CompactState(version=5)
    )
    result = await wrap_fn(state, _runtime_with_llm(_fake_llm(_LONG_SUMMARY)), _fake_config())
    assert result is not None
    assert result["compact"].version == 6  # pyright: ignore[reportUnknownMemberType]


# --- compact reminder (plugins/ava_compact/plugin.py) tests ---
#
# The reminder is the same single before_llm hook's below-ceiling branch: when
# est sits in (compact_reminder_tokens, auto_compact_tokens] it injects a
# one-time qualitative note instead of force-compacting. Coverage: fires in
# band; silent below the threshold; yields to force above the ceiling; once per
# window; re-arms after a compaction; defers to the agent-reply note; silent
# with no conversation to compact.


def _reminder_state(state_cls, *, version=0, shown=False, seen=0, messages=None):
    """state with the compact reminder bookkeeping fields set explicitly."""
    return state_cls(
        messages=_over_threshold_messages() if messages is None else messages,
        halted=False,
        compact=CompactState(version=version, reminder_shown=shown, reminder_seen_version=seen),
    )


_COMPACT_SECTIONS = (
    "Requests",
    "Progress",
    "In flight",
    "Dead ends",
    "Pitfalls",
    "Verbatim tail",
)


# ============================================================
# claim node compact edge case tests
# ============================================================
# The following tests claim_node's boundary handling of compact_summary / compact_request.
# Testing style same as agent/tests/claim/test_claim.py — directly call claim_node to test dispatch.


# --- helpers (reusing pattern from test_claim.py) ---


def _insert_compact_summary(db: psycopg.Connection, tid: int, content: str) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind) "
            "VALUES (%s, %s, 'compact_summary')",
            (tid, content),
        )
    db.commit()


def _make_runtime(ops_pool=None, llm=None):
    if ops_pool is None:
        ops_pool = AsyncMock()
    if llm is None:
        llm = AsyncMock()
    ctx = AvaContext(
        ops_pool=ops_pool,  # pyright: ignore[reportUnknownArgumentType]
        llm=llm,  # pyright: ignore[reportUnknownArgumentType]
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(),
        db=Database.from_settings(),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
    )
    from langgraph.runtime import Runtime

    return Runtime(context=ctx)


def _config(tid: int) -> RunnableConfig:
    return {"configurable": {"thread_id": str(tid)}}
