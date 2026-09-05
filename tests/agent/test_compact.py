"""Compact tool tests.

`agent/hooks/compact.py` exports:
- `generate_summary(messages, llm) -> summary`: pure function (called by claim node processing
  inbound kind compact_request; also called internally by auto-compact hook). Request =
  whole conversation reused byte-for-byte (original SystemMessage + all content + same
  bind_tools(execute_code)) + a final COMPACTION_INSTRUCTION — hits prefix cache.
  Returns summary text (response.text; block content extracts text blocks; thinking excluded).
  LLM returns empty text (tool_use-only blocks, empty string) → RuntimeError; conversation empty → ValueError.
- `auto_compact_before_llm` built-in before_llm hook — when over threshold runs generate_summary,
  replaces entire history with `[system, summary]` (leaves no raw tail). Short summary retries ≤
  COMPACT_MAX_ATTEMPTS, still short then fail-fast. On success emits CompactDone.

After compact, history = `[RemoveAll, SystemMessage, HumanMessage(header+summary)]` —
summary is complete memory; framework no longer appends any original messages (no tail).
"""

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
from psycopg_pool import AsyncConnectionPool

from agent.graph._claim import BEFORE_LLM, END, claim_node
from agent.graph._context import AvaContext
from agent.hooks.compact import (
    COMPACT_MAX_ATTEMPTS,
    COMPACTION_INSTRUCTION,
    CompactionFailedError,
    auto_compact_before_llm,
    compose_summary_message,
    generate_summary,
)
from agent.llm import execute_code
from agent.messages import inbound_message
from agent.state import AgentState, CompactState
from shared.lm.context_budget import ContextBudget
from tests.conftest import spawn_agent


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
    monkeypatch.setattr("agent.hooks.compact.resolve_context_budget", lambda _model: budget)  # pyright: ignore[reportUnknownArgumentType]


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
    # the post-compact checkpoint trim treats it as a no-op (real-pool trimming
    # is covered by tests/test_checkpoint_cleanup.py and the claim_node compact
    # tests below, which use aops_pool).
    ctx = AvaContext(ops_pool=None, llm=llm, event_publisher=MagicMock())
    return Runtime(context=ctx)


def _fake_config() -> RunnableConfig:
    """Minimal config with agent_id=1 — hook's three-argument signature requires passing config."""
    return {"configurable": {"thread_id": "1"}}


# --- generate_summary tests ---


async def test_generate_summary_returns_summary():
    """generate_summary returns summary text (from LLM), no longer returns tail."""
    msgs: list[AnyMessage] = [HumanMessage(content=f"msg{i}") for i in range(8)]

    summary = await generate_summary(msgs, _fake_llm(summary_text="a synthetic summary"))

    assert summary == "a synthetic summary"


async def test_generate_summary_emits_agent_billing_span(
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
) -> None:
    """A completed compaction call records its provider usage in the ledger.

    The regression this catches is removing billing emission from the compact
    path, which is the largest individual agent provider call.
    """
    from opentelemetry import trace as otel_trace

    from shared import trace as trace_mod

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
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setitem(trace_mod._state, "initialized", True)
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
    llm.model_name = "deepseek-v4-pro"

    assert (
        await generate_summary([HumanMessage(content="conversation")], llm) == "a complete summary"
    )

    assert len(tracer.spans) == 1
    span = tracer.spans[0]
    assert span.name == "ava.billing.call"
    assert span.attributes["ava.billing.model"] == "deepseek-v4-pro"
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
    await generate_summary(convo, llm)

    [call] = _compaction_ainvoke(llm).call_args_list
    [llm_input] = call.args
    assert llm_input[:-1] == convo  # whole conversation, no hold-out
    assert isinstance(llm_input[-1], HumanMessage)
    assert llm_input[-1].content == COMPACTION_INSTRUCTION


async def test_generate_summary_reuses_conversation_prefix_for_cache():
    """compaction request = main conversation + a final instruction — original SystemMessage kept in place (not replaced with compaction-specific system prompt), bind_tools same as main llm node (tools field participates in prefix rendering); the whole conversation is the prefix of the previous main request, combined they hit the backend prefix cache."""
    sys_msg = SystemMessage(content="<real agent sys prompt>")
    content: list[AnyMessage] = [HumanMessage(content=f"m-{i}") for i in range(5)]

    llm = _fake_llm()
    await generate_summary([sys_msg, *content], llm)

    llm.bind_tools.assert_called_once_with([execute_code])
    [call] = _compaction_ainvoke(llm).call_args_list
    [llm_input] = call.args
    assert llm_input[0] is sys_msg  # original object in-place — same byte-for-byte prefix
    assert llm_input[:-1] == [sys_msg, *content]
    assert llm_input[-1].content == COMPACTION_INSTRUCTION


async def test_generate_summary_raises_on_empty_llm_text():
    """LLM returns empty text (e.g., defying instruction only gives tool_call) → RuntimeError —
    empty summary cannot be used to replace history."""
    msgs: list[AnyMessage] = [HumanMessage(content=f"m{i}") for i in range(3)]

    with pytest.raises(RuntimeError, match="no text"):
        await generate_summary(msgs, _fake_llm(summary_text=""))


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

    summary = await generate_summary(msgs, _fake_llm(response=block_response))
    assert summary == "the real summary"


async def test_generate_summary_raises_on_tool_use_only_block_content():
    """Defying instruction only returns tool_use block (no text block) → response.text is empty →
    RuntimeError — cannot let block list repr become summary."""
    msgs: list[AnyMessage] = [HumanMessage(content=f"m{i}") for i in range(3)]
    tool_only = AIMessage(
        content=[{"type": "tool_use", "id": "c1", "name": "execute_code", "input": {"code": "x"}}]
    )

    with pytest.raises(RuntimeError, match="no text"):
        await generate_summary(msgs, _fake_llm(response=tool_only))


async def test_generate_summary_raises_on_empty_conversation():
    """Only SystemMessage (no conversation) → ValueError — nothing to summarize."""
    with pytest.raises(ValueError, match="empty"):
        await generate_summary([SystemMessage(content="<sys>")], _fake_llm())


# --- auto_compact_before_llm hook tests ---


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
    result = await auto_compact_before_llm(
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
    result = await auto_compact_before_llm(state, _runtime_with_llm(_fake_llm()), _fake_config())
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
    result = await auto_compact_before_llm(
        state, _runtime_with_llm(_fake_llm(_LONG_SUMMARY)), _fake_config()
    )
    assert result is not None  # 1000 chars/4 estimate > 100 ceiling -> compact


async def test_auto_compact_hook_returns_none_when_under_threshold(monkeypatch: pytest.MonkeyPatch):
    """token estimate ≤ threshold → hook returns None, no-op pass-through to llm."""
    _patch_compact_config(monkeypatch, auto_compact_tokens=1_000_000)
    state = AgentState(messages=[HumanMessage(content="hi" * 100)], halted=False)
    result = await auto_compact_before_llm(state, _runtime_with_llm(_fake_llm()), _fake_config())
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
    result = await auto_compact_before_llm(state, _runtime_with_llm(fake_llm), _fake_config())

    assert result is not None
    _compaction_ainvoke(fake_llm).assert_called_once()
    new_msgs = result["messages"]
    assert len(new_msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(new_msgs[0], RemoveMessage)
    assert new_msgs[0].id == REMOVE_ALL_MESSAGES  # pyright: ignore[reportUnknownMemberType]

    reset = result["context_reset"]
    assert [m.content for m in reset.tail] == [compose_summary_message(_LONG_SUMMARY)]  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(reset.tail[0], HumanMessage)  # pyright: ignore[reportUnknownMemberType]
    assert reset.resume == "llm"  # pyright: ignore[reportUnknownMemberType]
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
    result = await auto_compact_before_llm(state, _runtime_with_llm(llm), _fake_config())
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
        await auto_compact_before_llm(state, _runtime_with_llm(llm), _fake_config())
    assert _compaction_ainvoke(llm).call_count == COMPACT_MAX_ATTEMPTS


async def test_auto_compact_hook_raises_when_summary_short_every_attempt(
    monkeypatch: pytest.MonkeyPatch,
):
    """Every summary shorter than COMPACT_MIN_SUMMARY_CHARS (model ignores template) → after retries exhausted
    fail fast, rather than silently replacing history with short summary (agent-240 type incident)."""
    _patch_compact_config(monkeypatch, auto_compact_tokens=1)
    llm = _fake_llm("too short")  # 9 chars < floor, on every call
    state = _over_threshold_state()

    with pytest.raises(CompactionFailedError, match="no usable summary across"):
        await auto_compact_before_llm(state, _runtime_with_llm(llm), _fake_config())
    assert _compaction_ainvoke(llm).call_count == COMPACT_MAX_ATTEMPTS


async def test_auto_compact_hook_retries_short_then_accepts_long(monkeypatch: pytest.MonkeyPatch):
    """First summary short → retry; second reaches length → accepted and replaces history, no more retries."""
    _patch_compact_config(monkeypatch, auto_compact_tokens=1)
    llm = _fake_llm_seq("too short", _LONG_SUMMARY)  # short, then long
    state = _over_threshold_state()

    result = await auto_compact_before_llm(state, _runtime_with_llm(llm), _fake_config())

    assert result is not None
    assert _compaction_ainvoke(llm).call_count == 2  # stopped as soon as one cleared the floor
    assert result["context_reset"].tail[0].content == compose_summary_message(_LONG_SUMMARY)  # pyright: ignore[reportUnknownMemberType]


async def test_auto_compact_hook_emits_compact_done_on_success(monkeypatch: pytest.MonkeyPatch):
    """After successful compact on auto path, emit CompactDone (with this agent id), so UI refreshes."""
    _patch_compact_config(monkeypatch, auto_compact_tokens=1)
    publisher = MagicMock()
    pool = AsyncMock()

    # Set up pool.connection() as a no-op async context manager so the
    # insert_event_log_async call does not produce an unawaited-coroutine warning.
    async def _noop_conn():
        conn = AsyncMock()
        cur = AsyncMock()
        conn.cursor = MagicMock(return_value=cur)
        return conn

    pool.connection = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(side_effect=_noop_conn),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    ctx = AvaContext(ops_pool=pool, llm=_fake_llm(_LONG_SUMMARY), event_publisher=publisher)
    state = _over_threshold_state()

    result = await auto_compact_before_llm(state, Runtime(context=ctx), _fake_config())

    assert result is not None
    publisher.emit.assert_called_once()
    emitted = publisher.emit.call_args.args[0]
    assert '"compact_done"' in emitted


# --- _auto_compact_with_version_bump (plugins/ava_compact/plugin.py) tests ---
#
# This wrapper is the implementation side of the Layer 3 monotonic counter producer. Tests cover three things:
# 1. inner returns None → wrap returns None (no bump version, pass-through)
# 2. compact successful (cur=0) → wrap returns dict with messages + compact.version=1
# 3. compact successful (cur=N>0) → wrap returns dict with compact.version=N+1
# Implementation detail: wrap reads `state.compact.version` then model_copy the whole compact channel
# reads self-registered fields, must first register_plugin_state(AvaCompactState) so dynamic
# AgentState subclass carries this field. Fixture uses _ava_compact_registered for isolation.


@pytest.fixture
def _ava_compact_loaded():
    """Compact is now built-in (Issue #1284). The wrapper function lives in
    agent.hooks.compact; state fields are on BaseAgentState. Returns
    (state_cls, wrap_fn) for tests to call.

    Teardown clears hook registrations to prevent leakage into other tests.
    """
    from agent.hooks.compact import _auto_compact_with_version_bump
    from agent.state import build_agent_state, clear_plugin_registrations

    clear_plugin_registrations()

    yield build_agent_state(), _auto_compact_with_version_bump

    clear_plugin_registrations()


async def test_auto_compact_with_version_bump_passthrough_none(
    _ava_compact_loaded, monkeypatch: pytest.MonkeyPatch
):
    """inner auto_compact_before_llm returns None (under threshold) → wrap also returns None."""
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


async def test_auto_compact_with_version_bump_zero_to_one(
    _ava_compact_loaded, monkeypatch: pytest.MonkeyPatch
):
    """First compact successful → compact.version increments from 0 to 1, dict contains messages."""
    state_cls, wrap_fn = _ava_compact_loaded

    _patch_compact_config(monkeypatch, auto_compact_tokens=1)
    state = state_cls(
        messages=_over_threshold_messages(), halted=False, compact=CompactState(version=0)
    )
    result = await wrap_fn(state, _runtime_with_llm(_fake_llm(_LONG_SUMMARY)), _fake_config())
    assert result is not None
    assert result["compact"].version == 1  # pyright: ignore[reportUnknownMemberType]
    assert "messages" in result


async def test_auto_compact_with_version_bump_increments_from_existing(
    _ava_compact_loaded, monkeypatch: pytest.MonkeyPatch
):
    """Not first compact: state already has compact.version=5 → wrap increments to 6."""
    state_cls, wrap_fn = _ava_compact_loaded

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


async def test_compact_reminder_fires_in_band(_ava_compact_loaded, monkeypatch: pytest.MonkeyPatch):
    """reminder_tokens < est <= ceiling, fresh window, no agent inbound →
    inject the qualitative system_note + mark reminder_shown; not a force
    (compact.version unchanged, no history replacement)."""
    from agent.hooks import compact as _p

    state_cls, wrap_fn = _ava_compact_loaded
    _patch_compact_config(monkeypatch, compact_reminder_tokens=1, auto_compact_tokens=1_000_000)
    result = await wrap_fn(
        _reminder_state(state_cls),  # pyright: ignore[reportUnknownArgumentType]
        _runtime_with_llm(_fake_llm()),
        _fake_config(),
    )

    assert result is not None
    note = result["messages"][0]
    assert note.additional_kwargs["ava_msg_type"] == "system_note"  # pyright: ignore[reportUnknownMemberType]
    assert note.additional_kwargs["ava_note_tag"] == "compact_reminder"  # pyright: ignore[reportUnknownMemberType]
    assert note.content == f"[system] {_p.COMPACT_REMINDER_NOTE}"  # pyright: ignore[reportUnknownMemberType]
    assert result["compact"].reminder_shown is True  # pyright: ignore[reportUnknownMemberType]
    assert (
        result["compact"].version == 0  # pyright: ignore[reportUnknownMemberType]
    )  # reminder != force: version untouched


async def test_compact_reminder_silent_below_threshold(
    _ava_compact_loaded, monkeypatch: pytest.MonkeyPatch
):
    """est <= reminder_tokens → no note (and no force, est under ceiling)."""
    state_cls, wrap_fn = _ava_compact_loaded
    _patch_compact_config(
        monkeypatch, compact_reminder_tokens=1_000_000, auto_compact_tokens=2_000_000
    )
    result = await wrap_fn(
        _reminder_state(state_cls),  # pyright: ignore[reportUnknownArgumentType]
        _runtime_with_llm(_fake_llm()),
        _fake_config(),
    )
    assert result is None


async def test_compact_reminder_yields_to_force_above_ceiling(
    _ava_compact_loaded, monkeypatch: pytest.MonkeyPatch
):
    """est > ceiling → the force branch runs (history replaced + version bump),
    never the reminder; the two are mutually exclusive in the one hook."""
    state_cls, wrap_fn = _ava_compact_loaded
    _patch_compact_config(monkeypatch, compact_reminder_tokens=0, auto_compact_tokens=1)
    result = await wrap_fn(
        _reminder_state(state_cls),  # pyright: ignore[reportUnknownArgumentType]
        _runtime_with_llm(_fake_llm(_LONG_SUMMARY)),
        _fake_config(),
    )

    assert result is not None
    assert (
        result["compact"].version == 1  # pyright: ignore[reportUnknownMemberType]
    )  # force path bumped version
    assert (
        result["compact"].reminder_shown is False  # pyright: ignore[reportUnknownMemberType]
    )  # force preserves the flag, does not set it
    assert isinstance(result["messages"][0], RemoveMessage)  # full replacement


async def test_compact_reminder_once_per_window(
    _ava_compact_loaded, monkeypatch: pytest.MonkeyPatch
):
    """already reminded this window (shown=True, no compaction since) → silent."""
    state_cls, wrap_fn = _ava_compact_loaded
    _patch_compact_config(monkeypatch, compact_reminder_tokens=1, auto_compact_tokens=1_000_000)
    state = _reminder_state(state_cls, version=0, shown=True, seen=0)  # pyright: ignore[reportUnknownArgumentType]
    result = await wrap_fn(state, _runtime_with_llm(_fake_llm()), _fake_config())
    assert result is None


async def test_compact_reminder_rearms_after_compaction(
    _ava_compact_loaded, monkeypatch: pytest.MonkeyPatch
):
    """shown=True but a compaction advanced compact.version past the bookmark →
    the old note was summarized away, so the reminder re-arms and fires again,
    catching the bookmark up to the new version."""
    state_cls, wrap_fn = _ava_compact_loaded
    _patch_compact_config(monkeypatch, compact_reminder_tokens=1, auto_compact_tokens=1_000_000)
    state = _reminder_state(state_cls, version=1, shown=True, seen=0)  # pyright: ignore[reportUnknownArgumentType]
    result = await wrap_fn(state, _runtime_with_llm(_fake_llm()), _fake_config())

    assert result is not None
    assert result["compact"].reminder_shown is True  # pyright: ignore[reportUnknownMemberType]
    assert (
        result["compact"].reminder_seen_version == 1  # pyright: ignore[reportUnknownMemberType]
    )  # bookmark caught up


async def test_compact_reminder_defers_to_agent_reply(
    _ava_compact_loaded, monkeypatch: pytest.MonkeyPatch
):
    """in band, but the turn was woken by an agent inbound → defer (return None)
    so the agent-reply note owns the single messages-write this pass allows."""
    state_cls, wrap_fn = _ava_compact_loaded
    _patch_compact_config(monkeypatch, compact_reminder_tokens=1, auto_compact_tokens=1_000_000)
    msgs = [
        SystemMessage(content="<sys>"),
        *(HumanMessage(content="x" * 1000) for _ in range(5)),
        inbound_message(content="ping", source="agent:5", inbound_id=1),
    ]
    state = _reminder_state(state_cls, messages=msgs)  # pyright: ignore[reportUnknownArgumentType]
    result = await wrap_fn(state, _runtime_with_llm(_fake_llm()), _fake_config())
    assert result is None


async def test_compact_reminder_silent_when_no_conversation(
    _ava_compact_loaded, monkeypatch: pytest.MonkeyPatch
):
    """est over the reminder threshold but only a SystemMessage (nothing to
    compact) → no point reminding, return None."""
    state_cls, wrap_fn = _ava_compact_loaded
    _patch_compact_config(monkeypatch, compact_reminder_tokens=1, auto_compact_tokens=1_000_000)
    state = _reminder_state(state_cls, messages=[SystemMessage(content="x" * 100_000)])  # pyright: ignore[reportUnknownArgumentType]
    result = await wrap_fn(state, _runtime_with_llm(_fake_llm()), _fake_config())
    assert result is None


_COMPACT_SECTIONS = (
    "Requests",
    "Progress",
    "In flight",
    "Dead ends",
    "Pitfalls",
    "Verbatim tail",
)


def test_compact_contract_lives_in_docstring_and_reaches_prompt(_ava_compact_loaded):
    """The one compaction contract — the summary's sections + how to write it —
    lives in a single place, the `ava.self.compact` docstring, and is rendered
    into the agent's leading prompt (the SDK reference expands `self`). That is
    what lets every trigger be a short pointer instead of restating the
    template: the agent reads it in its own SDK, and the forced-compact request
    carries it in its own prompt. Pin both halves so the contract cannot be
    gutted or fall out of the prompt unnoticed."""
    from agent.graph._system_prompt import build_system_prompt
    from ava.self import compact

    contract = compact.__doc__
    assert contract is not None
    for section in _COMPACT_SECTIONS:
        assert section in contract, f"section {section!r} missing from the compact contract"

    # The contract must actually reach the prompt — otherwise the short triggers
    # below point at something the agent / forced-compact model never sees.
    system_prompt = build_system_prompt()
    for section in _COMPACT_SECTIONS:
        assert section in system_prompt, f"section {section!r} not rendered into the system prompt"


def test_compact_triggers_point_at_the_contract(_ava_compact_loaded):
    """Each compaction trigger (forced/auto instruction, the reminder nudge, the
    /compact command) is a short opener that defers to the `ava.self.compact`
    contract rather than carrying its own copy of the template."""
    from agent.hooks import compact as _p
    from shared.paths import repo_root

    compact_md = (repo_root() / "commands" / "compact.md").read_text(encoding="utf-8")
    assert "ava.self.compact" in COMPACTION_INSTRUCTION
    assert "ava.self.compact" in _p.COMPACT_REMINDER_NOTE
    assert "ava.self.compact" in compact_md


# ============================================================
# claim node compact edge case tests
# ============================================================
# The following tests claim_node's boundary handling of compact_summary / compact_request.
# Testing style same as tests/agent/test_claim.py — directly call claim_node to test dispatch.


async def test_compact_summary_preserves_agent_continuity(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """After processing compact_summary the batch resumes at BEFORE_LLM (not END) —
    the agent continues its conversation rather than being terminated. The goto
    itself is the init_context detour that rebuilds the standing head; where the
    batch was actually headed rides in `context_reset.resume`."""
    tid = spawn_agent()
    _insert_compact_summary(db_conn, tid, "summary after compact")

    sys_msg = SystemMessage(content="<test sys prompt>")
    initial_msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content=f"history-{i}") for i in range(8)),
    ]
    state = AgentState(messages=initial_msgs)

    cmd = await claim_node(state, _make_runtime(aops_pool), _config(tid))
    assert cmd.goto == "init_context"
    resume = cmd.update["context_reset"].resume  # type: ignore[index]
    assert resume == BEFORE_LLM, (
        f"after compact should continue conversation (resume=BEFORE_LLM), actual {resume}"
    )


async def test_compact_summary_replaces_whole_history_no_tail(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """compact_summary → the whole history is cleared and the parked tail is the
    summary alone; not a single original message survives (the summary is the
    complete memory, no raw tail)."""
    tid = spawn_agent()
    sys_msg = SystemMessage(content="<test sys prompt>")
    initial_msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content=f"history-{i}") for i in range(8)),
    ]
    state = AgentState(messages=initial_msgs)

    _insert_compact_summary(db_conn, tid, "the whole memory")
    cmd = await claim_node(state, _make_runtime(aops_pool), _config(tid))

    tail = _compact_tail(cmd.update)
    assert [m.content for m in tail] == [compose_summary_message("the whole memory")]  # pyright: ignore[reportUnknownMemberType]
    assert cmd.goto == "init_context"


async def test_compact_summary_emits_compact_done(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    loguru_records: list[dict[str, Any]],
):
    """When claim processes compact_summary (agent-written summary), emit CompactDone
    at the same place where history is replaced — so UI refreshes, aligning with auto path (agent/hooks/compact.py).
    User-triggered compact_request goes through the same compact_payload block, emit is path-agnostic."""
    tid = spawn_agent()
    _insert_compact_summary(db_conn, tid, "summary after compact")
    state = AgentState(
        messages=[
            SystemMessage(content="<sys>"),
            *(HumanMessage(content=f"history-{i}") for i in range(4)),
        ]
    )
    # Explicit publisher MagicMock (not via runtime.context, which is Optional)
    # so the emit assertion types cleanly — mirrors the auto-path emit test.
    publisher = MagicMock()
    ctx = AvaContext(ops_pool=aops_pool, llm=AsyncMock(), event_publisher=publisher)
    runtime = Runtime(context=ctx)

    await claim_node(state, runtime, _config(tid))

    emitted = [call.args[0] for call in publisher.emit.call_args_list]
    assert any('"compact_done"' in e for e in emitted), f"no CompactDone emitted; saw {emitted}"
    [monitoring] = [
        record
        for record in loguru_records
        if record["extra"].get("event") == "compaction_completed"
    ]
    assert monitoring["extra"] | {"msg": None} == {
        "agent_id": tid,
        "compact_kind": "compact_summary",
        "compactions": 1,
        "history_chars": len("history-0") * 4,
        "summary_chars": len("summary after compact"),
        "summary_history_ratio": pytest.approx(  # pyright: ignore[reportUnknownMemberType]
            len("summary after compact") / (len("history-0") * 4)
        ),
        "event": "compaction_completed",
        "msg": None,
    }


async def test_consecutive_compacts_both_processed(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """Two consecutive compact_summary → first replaces with [sys, summary1]; second on that
    state replaces again with [sys, summary2]."""
    tid = spawn_agent()
    sys_msg = SystemMessage(content="<test sys prompt>")

    # ---- first compact ----
    _insert_compact_summary(db_conn, tid, "first compact summary")
    initial_msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content=f"gen1-msg-{i}") for i in range(8)),
    ]
    state = AgentState(messages=initial_msgs)

    cmd1 = await claim_node(state, _make_runtime(aops_pool), _config(tid))
    tail1 = _compact_tail(cmd1.update)
    assert tail1[0].content == compose_summary_message("first compact summary")  # pyright: ignore[reportUnknownMemberType]

    # ---- second compact (on state after compact) ----
    # state after first compact (RemoveMessage already processed by reducer) = [sys, summary1]
    compacted_state = AgentState(messages=[sys_msg, HumanMessage(content="first compact summary")])
    _insert_compact_summary(db_conn, tid, "second compact summary")
    cmd2 = await claim_node(compacted_state, _make_runtime(aops_pool), _config(tid))
    tail2 = _compact_tail(cmd2.update)
    assert [m.content for m in tail2] == [compose_summary_message("second compact summary")]  # pyright: ignore[reportUnknownMemberType]
    assert cmd2.update["context_reset"].resume != END  # type: ignore[index]  # can continue


async def test_compact_with_empty_state_injects_system_message_and_summary(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """A compact_summary arriving on an empty window behaves like any other: the
    window is cleared and the summary parked, exactly as when there was history
    to clear. Claim used to lay down a cold-start head here and then pop it back
    off — the head is `init_context`'s now, so there is nothing to undo and the
    two cases stopped differing."""
    tid = spawn_agent()
    _insert_compact_summary(db_conn, tid, "compact before any chat")

    state = AgentState()  # empty messages

    cmd = await claim_node(state, _make_runtime(aops_pool), _config(tid))

    tail = _compact_tail(cmd.update)
    assert [m.content for m in tail] == [compose_summary_message("compact before any chat")]  # pyright: ignore[reportUnknownMemberType]
    assert cmd.goto == "init_context"


async def test_compact_with_super_long_summary_in_claim(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """claim_node processes compact_summary with super-long summary (50K chars) —
    no truncation, no error thrown."""
    tid = spawn_agent()
    long_summary = "LONG_" * 10_000  # 50K chars

    sys_msg = SystemMessage(content="<test sys prompt>")
    initial_msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content=f"m{i}") for i in range(6)),
    ]
    state = AgentState(messages=initial_msgs)

    _insert_compact_summary(db_conn, tid, long_summary)
    cmd = await claim_node(state, _make_runtime(aops_pool), _config(tid))

    tail = _compact_tail(cmd.update)
    assert tail[0].content == compose_summary_message(long_summary)  # pyright: ignore[reportUnknownMemberType]
    # the 50K summary rides through untruncated (only the fixed header is added)
    assert len(tail[0].content) == len(compose_summary_message(long_summary))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert long_summary in tail[0].content  # pyright: ignore[reportUnknownMemberType]


async def test_terminate_preserves_pending_summary_without_wiping_history(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """Lifecycle acceptance is serial; a summary cannot run in the exiting owner."""
    from agent._starting import claim_agent_row

    tid = spawn_agent()
    claim_agent_row(tid)
    summary_text = "summary before terminate"
    _insert_compact_summary(db_conn, tid, summary_text)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind) VALUES (%s, '', 'terminate')",
            (tid,),
        )
    db_conn.commit()

    sys_msg = SystemMessage(content="<test sys prompt>")
    initial_msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content=f"h-{i}") for i in range(8)),
    ]
    state = AgentState(messages=initial_msgs)

    cmd = await claim_node(state, _make_runtime(aops_pool), _config(tid))

    assert cmd.goto == END
    assert "context_reset" not in cmd.update  # type: ignore[operator]
    assert state.messages == initial_msgs
    assert db_conn.execute(
        "SELECT kind,status,applied_at IS NOT NULL,observed_at FROM inbound_messages "
        "WHERE agent_id=%s ORDER BY id",
        (tid,),
    ).fetchall() == [
        ("compact_summary", "pending", False, None),
        ("terminate", "claimed", True, None),
    ]
    # This process remains alive: NULL/released state must not fabricate exit
    # merely to let the summary run. Explicit resurrection is covered by the
    # real-process E2E, which proves exit and successor response separately.
    from shared.lifecycle_termination_observe import observe_applied_termination
    from shared.machine import machine_name

    assert not observe_applied_termination(db_conn, tid, machine_name())
    db_conn.commit()


async def test_compact_in_same_batch_as_restart(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """The admitted successor, not the exiting owner, consumes the same summary."""
    from agent._starting import claim_agent_row
    from shared.runtime_incarnation import current_incarnation

    tid = spawn_agent()
    claim_agent_row(tid)
    old = current_incarnation(tid)
    assert old is not None

    summary_text = "summary pre-restart"
    _insert_compact_summary(db_conn, tid, summary_text)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind) VALUES (%s, '', 'restart')",
            (tid,),
        )
    db_conn.commit()

    sys_msg = SystemMessage(content="<test sys prompt>")
    initial_msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content=f"h-{i}") for i in range(8)),
    ]
    state = AgentState(messages=initial_msgs)

    cmd = await claim_node(state, _make_runtime(aops_pool), _config(tid))

    assert cmd.goto == END
    assert "context_reset" not in cmd.update  # type: ignore[operator]
    assert state.messages == initial_msgs
    rows = db_conn.execute(
        "SELECT id,kind,status,applied_at IS NOT NULL,observed_at FROM inbound_messages "
        "WHERE agent_id=%s ORDER BY id",
        (tid,),
    ).fetchall()
    assert [row[1:] for row in rows] == [
        ("compact_summary", "pending", False, None),
        ("restart", "claimed", True, None),
    ]
    summary_id, restart_id = rows[0][0], rows[1][0]
    # Simulate the controller's already-verified exit/launch boundary only;
    # actual OS disappearance is proved by strict test_self_restart E2E.
    db_conn.execute("UPDATE agents_meta SET status='idling',pid=NULL WHERE id=%s", (tid,))
    db_conn.execute(
        "UPDATE inbound_messages SET payload=payload||jsonb_build_object('launch_attempts',1) "
        "WHERE id=%s",
        (restart_id,),
    )
    db_conn.commit()
    claim_agent_row(tid, restart_command_id=restart_id)
    assert current_incarnation(tid) != old
    assert db_conn.execute(
        "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (restart_id,)
    ).fetchone() == ("done", True)
    resumed = await claim_node(state, _make_runtime(aops_pool), _config(tid))
    tail = _compact_tail(resumed.update)
    assert tail[0].content == compose_summary_message(summary_text)  # pyright: ignore[reportUnknownMemberType]
    assert resumed.goto == "init_context"
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (summary_id,)
    ).fetchone() == ("done",)


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
    from unittest.mock import AsyncMock

    if ops_pool is None:
        ops_pool = AsyncMock()
    if llm is None:
        llm = AsyncMock()
    ctx = AvaContext(ops_pool=ops_pool, llm=llm, event_publisher=MagicMock())  # pyright: ignore[reportUnknownArgumentType]
    from langgraph.runtime import Runtime

    return Runtime(context=ctx)


def _config(tid: int) -> RunnableConfig:
    return {"configurable": {"thread_id": str(tid)}}


async def test_auto_compact_summary_message_carries_msg_type(monkeypatch: pytest.MonkeyPatch):
    """Task #1017: the auto-compact summary message must carry the same
    ava_msg_type stamp the claim-node (force) compact path writes. Without it
    the timeline read side classifies the HumanMessage as a catch-all
    system_marker with source=null and the frontend renders the red
    UNRECOGNIZED SYSTEM_MARKER alarm (2026-08-07 user report)."""
    _patch_compact_config(monkeypatch, auto_compact_tokens=1)
    state = _over_threshold_state()

    fake_llm = _fake_llm(_LONG_SUMMARY)
    result = await auto_compact_before_llm(state, _runtime_with_llm(fake_llm), _fake_config())
    assert result is not None

    tail = result["context_reset"].tail  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(tail[0], HumanMessage)
    kwargs = tail[0].additional_kwargs  # pyright: ignore[reportUnknownMemberType]
    assert kwargs.get("ava_msg_type") == "compact_summary"  # pyright: ignore[reportUnknownMemberType]
    assert "ava_created_at" in kwargs


async def test_claim_compact_request_summary_message_carries_msg_type(
    monkeypatch: pytest.MonkeyPatch,
):
    """Task #1017: the claim-node (force / UI /compact) compact path stamps its
    summary message with ava_msg_type=compact_request — the two compact paths
    must produce the same message contract so the frontend never sees an
    unrecognized system_marker."""
    from agent.hooks.compact import build_compact_transition

    transition = build_compact_transition(
        "the summary",
        resume="llm",
        summary_kwargs={
            "additional_kwargs": {
                "ava_msg_type": "compact_request",
                "ava_created_at": "2026-08-07T00:00:00+00:00",
            },
        },
    )
    tail = transition["context_reset"].tail
    assert isinstance(tail[0], HumanMessage)
    assert tail[0].additional_kwargs.get("ava_msg_type") == "compact_request"
