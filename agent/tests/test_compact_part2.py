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

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
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

from agent.hooks.compact import (
    auto_compact_for_llm,
)
from agent.state import AgentState, CompactState
from base.agents.context import AvaContext
from base.clock import Clock
from base.config import settings
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
        agent=AgentSlices.resolve(
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
        ),
        db=Database.from_settings(),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
    )
    return Runtime(context=ctx)


def _fake_config() -> RunnableConfig:
    """Minimal config with agent_id=1 — hook's three-argument signature requires passing config."""
    return {"configurable": {"thread_id": "1"}}


# --- generate_summary tests ---


# --- auto_compact_for_llm hook tests ---


def _over_threshold_state() -> AgentState:
    return AgentState(
        messages=[
            SystemMessage(content="<sys>"),
            *(HumanMessage(content="x" * 1000) for _ in range(5)),
        ],
        halted=False,
    )


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


def _over_threshold_messages() -> list[AnyMessage]:
    return [
        SystemMessage(content="<sys>"),
        *(HumanMessage(content="x" * 1000) for _ in range(5)),
    ]


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
        agent=AgentSlices.resolve(
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
        ),
        db=Database.from_settings(),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
    )
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
    result = await auto_compact_for_llm(state, _runtime_with_llm(fake_llm), _fake_config())
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
