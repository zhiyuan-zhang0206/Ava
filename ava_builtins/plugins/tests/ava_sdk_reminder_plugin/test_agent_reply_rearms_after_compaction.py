"""Ava sdk reminder plugin cases: agent reply rearms after compaction."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage

from agent.graph.llm_errors import LlmLedger
from agent.messages import inbound_message, tail_has_agent_inbound
from agent.state import CompactState
from ava_builtins.plugins.ava_sdk_reminder._state import AGENT_REPLY_CATEGORY
from ava_builtins.plugins.tests.test_ava_sdk_reminder_plugin import (
    _agent_inbound,
    _config,
    _nameerror_output,
    _pin_compact_budget,
    _runtime,
    _runtime_for_runner,
    _state,
)
from ava_builtins.plugins.tests.test_ava_sdk_reminder_plugin import (
    _load_ava_code_plugin as _load_ava_code_plugin,
)
from ava_builtins.plugins.tests.test_ava_sdk_reminder_plugin import (
    _loaded as _loaded,
)
from base.agents.messages.kwargs import ExecStatus
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog


async def test_agent_reply_rearms_after_compaction(_loaded: Any, *, database_gate: ProcessDbGate):
    hook = _loaded.sdk_reminder_agent_reply_before_llm
    # agent_reply reminded last window (bookmark 0); compaction advanced to 1 ->
    # the set re-arms and the note fires again.
    state = _state(
        [AIMessage(content="prev", id="a0"), _agent_inbound(source="agent:9")],
        ava_sdk_reminder__reminded={AGENT_REPLY_CATEGORY},
        ava_sdk_reminder__last_seen_compact=0,
        compact=CompactState(version=1),
    )
    result = await hook(state, _runtime(database_gate=database_gate), _config())

    assert result is not None
    [note] = result["messages"]
    assert "ava.agents.send_message" in note.content
    assert result["ava_sdk_reminder__last_seen_compact"] == 1
    assert result["ava_sdk_reminder__reminded"] == {AGENT_REPLY_CATEGORY}


async def test_agent_reply_user_inbound_is_noop(_loaded: Any, *, database_gate: ProcessDbGate):
    hook = _loaded.sdk_reminder_agent_reply_before_llm
    state = _state(
        [
            AIMessage(content="prev", id="a0"),
            inbound_message(content="hi", source="user", inbound_id=2, body_start=0),
        ]
    )
    result = await hook(state, _runtime(database_gate=database_gate), _config())
    assert result is None


async def test_agent_reply_defers_when_compaction_fires(
    _loaded: Any, monkeypatch: pytest.MonkeyPatch, *, database_gate: ProcessDbGate
):
    """When auto-compact would fire this same before_llm node run, the note is
    deferred (no inject, not marked) so it does not clobber / get clobbered by
    compaction's full-history message replacement."""
    # Force the compact threshold to 1 token so any history triggers it.
    _pin_compact_budget(monkeypatch, hard_tokens=1)

    hook = _loaded.sdk_reminder_agent_reply_before_llm
    # A non-empty conversation over the (forced-to-1) token threshold -> compaction fires.
    msgs: list[AnyMessage] = [
        AIMessage(content="prev", id="a0"),
        *(HumanMessage(content="x" * 200, id=f"h{i}") for i in range(5)),
        _agent_inbound(source="agent:9"),
    ]
    state = _state(msgs)
    result = await hook(state, _runtime(database_gate=database_gate), _config())
    assert result is None  # deferred; agent_reply not marked


async def test_agent_reply_once_cadence_dedups_and_rearms(
    _loaded: Any, monkeypatch: pytest.MonkeyPatch, *, database_gate: ProcessDbGate
):
    """`once_per_compaction` (the default, set explicitly here): a second inbound
    in the same window no-ops, and a compaction re-arms so the note fires again.
    Pins the behavior to the config value rather than the field default."""
    from base.config import settings

    monkeypatch.setattr(settings.agent, "agent_reply_reminder_cadence", "once_per_compaction")
    hook = _loaded.sdk_reminder_agent_reply_before_llm

    # Same window, already reminded -> no repeat.
    same_window = _state(
        [AIMessage(content="prev", id="a0"), _agent_inbound(source="agent:9")],
        ava_sdk_reminder__reminded={AGENT_REPLY_CATEGORY},
    )
    assert await hook(same_window, _runtime(database_gate=database_gate), _config()) is None

    # Compaction advanced the version past the bookmark -> re-armed, fires again.
    after_compact = _state(
        [AIMessage(content="prev", id="a0"), _agent_inbound(source="agent:9")],
        ava_sdk_reminder__reminded={AGENT_REPLY_CATEGORY},
        ava_sdk_reminder__last_seen_compact=0,
        compact=CompactState(version=1),
    )
    result = await hook(after_compact, _runtime(database_gate=database_gate), _config())
    assert result is not None
    assert result["ava_sdk_reminder__last_seen_compact"] == 1
    assert result["ava_sdk_reminder__reminded"] == {AGENT_REPLY_CATEGORY}


async def test_agent_reply_every_time_fires_even_when_already_reminded(
    _loaded: Any, monkeypatch: pytest.MonkeyPatch, *, database_gate: ProcessDbGate
):
    """`every_time`: the note fires on every agent inbound, even one already
    marked in the shared `reminded` set — and it does not touch that set (the
    after_exec hook owns the code-category re-arm)."""
    from base.config import settings

    monkeypatch.setattr(settings.agent, "agent_reply_reminder_cadence", "every_time")
    hook = _loaded.sdk_reminder_agent_reply_before_llm

    state = _state(
        [AIMessage(content="prev", id="a0"), _agent_inbound(source="agent:9")],
        ava_sdk_reminder__reminded={AGENT_REPLY_CATEGORY, "shell"},
    )
    result = await hook(state, _runtime(database_gate=database_gate), _config())

    assert result is not None
    [note] = result["messages"]
    assert note.additional_kwargs["ava_note_tag"] == "agent_reply"
    assert "ava.agents.send_message" in note.content
    # every_time does not participate in the once-per-window bookkeeping.
    assert "ava_sdk_reminder__reminded" not in result
    assert "ava_sdk_reminder__last_seen_compact" not in result


async def test_agent_reply_every_time_user_inbound_is_noop(
    _loaded: Any, monkeypatch: pytest.MonkeyPatch, *, database_gate: ProcessDbGate
):
    """`every_time` still gates on an agent-sourced inbound — a user inbound
    never triggers the reminder."""
    from base.config import settings

    monkeypatch.setattr(settings.agent, "agent_reply_reminder_cadence", "every_time")
    hook = _loaded.sdk_reminder_agent_reply_before_llm

    state = _state(
        [
            AIMessage(content="prev", id="a0"),
            inbound_message(content="hi", source="user", inbound_id=2, body_start=0),
        ]
    )
    assert await hook(state, _runtime(database_gate=database_gate), _config()) is None


async def test_agent_reply_every_time_still_defers_on_compaction(
    _loaded: Any, monkeypatch: pytest.MonkeyPatch, *, database_gate: ProcessDbGate
):
    """`every_time` defers exactly like `once_per_compaction` when auto-compact
    fires the same turn — the note would be clobbered by the message replacement."""
    from base.config import settings

    monkeypatch.setattr(settings.agent, "agent_reply_reminder_cadence", "every_time")
    _pin_compact_budget(monkeypatch, hard_tokens=1)
    hook = _loaded.sdk_reminder_agent_reply_before_llm

    msgs: list[AnyMessage] = [
        AIMessage(content="prev", id="a0"),
        *(HumanMessage(content="x" * 200, id=f"h{i}") for i in range(5)),
        _agent_inbound(source="agent:9"),
    ]
    assert await hook(_state(msgs), _runtime(database_gate=database_gate), _config()) is None


@pytest.mark.parametrize(
    "history_len_kind,auto_compact_tokens,expect_fire",
    [
        # under the token threshold -> no fire
        ("long", 10_000_000, False),
        # exactly at the threshold (est_tokens == threshold) -> no fire (gate is >)
        ("at_threshold", None, False),
        # over the threshold with a non-empty conversation -> fire
        ("long", 1, True),
        # over the threshold but no conversation to compress (system prompt only) -> no fire
        ("no_conversation", 1, False),
    ],
)
async def test_defer_predicate_matches_real_gate(
    _loaded: Any,
    monkeypatch: pytest.MonkeyPatch,
    history_len_kind,
    auto_compact_tokens,
    expect_fire,
    fake_cancel_event,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
):
    """`auto_compact_will_fire(state)` is the single shared gate the reminder plugins
    call; this pins it to the real `auto_compact_for_llm` firing across the
    threshold boundary. For each parametrized state, the predicate must equal
    "does the real auto_compact_for_llm actually return a replacement"
    (generate_summary stubbed so a fire produces a non-None result without a
    live LLM).
    """
    from agent.hooks import compact as compact_mod
    from agent.hooks.compact import auto_compact_will_fire

    if history_len_kind == "no_conversation":
        # only the system prompt -> conversation_messages empty -> no fire
        msgs: list[AnyMessage] = [SystemMessage(content="x" * 400)]
    elif history_len_kind == "at_threshold":
        # Build messages whose estimated tokens (total_chars // 4) exactly equal
        # the configured threshold, then set the threshold to that value so the
        # `occupancy <= threshold` gate is exercised right at the boundary.
        msgs = [HumanMessage(content="y" * 400, id=f"h{i}") for i in range(8)]
        total_chars = sum(len(m.content) for m in msgs)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        auto_compact_tokens = total_chars // 4
    else:  # "long"
        msgs = [HumanMessage(content="z" * 400, id=f"h{i}") for i in range(8)]

    _pin_compact_budget(monkeypatch, hard_tokens=auto_compact_tokens)  # pyright: ignore[reportUnknownArgumentType]

    # Stub generate_summary so a "would fire" path produces a real replacement
    # dict without invoking a live Compaction LLM. Long enough to clear the
    # auto-compact retry floor on the first attempt.
    async def _fake_generate_summary(
        messages, llm, _model, *, catalog: ModelCatalog, binding: object = None
    ):
        assert catalog is model_catalog
        return "stub summary " * 100

    monkeypatch.setattr(compact_mod, "generate_summary", _fake_generate_summary)  # pyright: ignore[reportUnknownArgumentType]

    state = _state(msgs)
    runtime = _runtime_for_runner(database_gate=database_gate)
    runtime = replace(runtime, context=replace(runtime.context, catalog=model_catalog))
    predicate = auto_compact_will_fire(
        state, runtime.context.require_agent(), catalog=model_catalog
    )
    real = await compact_mod.auto_compact_for_llm(state, runtime, _config())  # pyright: ignore[reportUnknownMemberType]
    assert predicate is expect_fire
    assert predicate == (real is not None)


@pytest.mark.parametrize("reminder_first", [True, False])
async def test_real_runner_compaction_wins_no_note(
    _loaded: Any,
    monkeypatch: pytest.MonkeyPatch,
    reminder_first,
    fake_cancel_event,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
):
    """Both hook orderings defer to the LLM compaction operation.

    No reminder is committed above the ceiling. The real LLM node then
    replaces history once, with a single compact version increment.
    """
    from langgraph.graph.message import add_messages

    from agent.hooks import compact as compact_mod
    from agent.hooks import make_hook_runner

    # Force auto-compact to fire on any history.
    _pin_compact_budget(monkeypatch, hard_tokens=1)

    # The compact hook wrapper lives directly in agent.hooks.compact.
    from agent.hooks.compact import _compact_reminder

    # Long enough to clear the auto-compact retry floor on the first attempt.
    long_summary = "compacted summary " * 100

    async def _fake_generate_summary(
        messages, llm, _model, *, catalog: ModelCatalog, binding: object = None
    ):
        assert catalog is model_catalog
        return long_summary

    monkeypatch.setattr(compact_mod, "generate_summary", _fake_generate_summary)  # pyright: ignore[reportUnknownArgumentType]

    compact_hook = _compact_reminder
    reminder_hook = _loaded.sdk_reminder_agent_reply_before_llm

    reminder_entry = ("ava_sdk_reminder", reminder_hook)
    compact_entry = (None, compact_hook)
    hooks = [reminder_entry, compact_entry] if reminder_first else [compact_entry, reminder_entry]
    runner = make_hook_runner("before_llm", default_next="llm", hooks=hooks)
    sys_msg = SystemMessage(content="<sys>")
    msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content="x" * 1000, id=f"h{i}") for i in range(5)),
        _agent_inbound(source="agent:9"),
    ]
    state = _state(msgs, compact=CompactState(version=0))
    runtime = _runtime_for_runner(database_gate=database_gate)
    runtime = replace(runtime, context=replace(runtime.context, catalog=model_catalog))
    cmd = await runner(state, runtime, _config())

    assert cmd.goto == "llm"
    hook_update = cast("dict[str, object]", cmd.update)
    assert isinstance(hook_update, dict) and "messages" not in hook_update
    from agent.graph.llm.node import llm_node

    cmd = await llm_node(state, runtime, _config(), ledger=LlmLedger())
    update = cmd.update
    assert isinstance(update, dict)
    # Apply the real add_messages reducer to get the committed messages.
    final = add_messages(list(state.messages), update["messages"])  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(final, list)

    # Only init_context rebuilds the head; no reminder survives the wipe.
    assert final == []
    # The summary rides in the parked tail the compaction handed to that node.
    assert [m.content for m in update["context_reset"].tail] == [  # pyright: ignore[reportUnknownMemberType]
        compact_mod.compose_summary_message(long_summary)
    ]
    assert cmd.goto == "init_context"
    # compact.version bumped exactly +1; agent_reply not marked (deferred).
    assert update["compact"].version == 1  # pyright: ignore[reportUnknownMemberType]
    assert AGENT_REPLY_CATEGORY not in update.get("ava_sdk_reminder__reminded", set())  # pyright: ignore[reportUnknownMemberType]


async def test_after_exec_leaves_exec_output_untouched(
    _loaded: Any, *, database_gate: ProcessDbGate
):
    """The hint is a fresh system-note, not a rewrite of the exec-output
    message: the injected message is a new id-less HumanMessage (so the reducer
    appends it after the output rather than replacing it), and the real
    exec_output message keeps its content + every additional_kwargs field."""
    from agent.messages import exec_output_message

    hook = _loaded.contribute().after_exec[0]
    out = exec_output_message(
        content="original stdout",
        tool_call_id="c1",
        exec_ms=1300,
        status=ExecStatus.COMPLETED,
        body_start=0,
    )
    out.id = "out-1"
    out_kwargs_before = dict(out.additional_kwargs)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    ai = AIMessage(
        content="",
        tool_calls=[
            {"name": "execute_code", "args": {"code": "subprocess.run(['ls'])"}, "id": "c1"}
        ],
    )
    state = _state([HumanMessage(content="do it", id="h1"), ai, out])

    result = await hook(state, _runtime(database_gate=database_gate), _config())
    assert result is not None
    [note] = result["messages"]
    # a fresh system-note (id-less -> the reducer appends), not the output message
    assert isinstance(note, HumanMessage)
    assert note.additional_kwargs["ava_msg_type"] == "system_note"  # pyright: ignore[reportUnknownMemberType]
    assert note.id is None
    assert "ava.shell.run" in note.content  # pyright: ignore[reportUnknownMemberType]
    # the real exec-output message is untouched: content + metadata intact
    assert out.content == "original stdout"  # pyright: ignore[reportUnknownMemberType]
    assert out.additional_kwargs == out_kwargs_before  # pyright: ignore[reportUnknownMemberType]


def test_tail_has_agent_inbound_exec_tail_false():
    """A normal exec tail [..., AIMessage(tool_call), ToolMessage] — the scan
    stops at the AIMessage, the trailing ToolMessage is not a HumanMessage, so
    no agent inbound -> False."""
    msgs: list[AnyMessage] = [
        AIMessage(
            content="",
            tool_calls=[{"name": "execute_code", "args": {"code": "x"}, "id": "c1"}],
            id="a1",
        ),
        ToolMessage(content="out", tool_call_id="c1", id="o1"),
    ]
    assert tail_has_agent_inbound(msgs) is False


def test_tail_has_agent_inbound_first_turn_agent_only_true():
    """First turn, no prior AIMessage at all, a single agent inbound -> True
    (the scan walks the whole list without hitting an AIMessage boundary)."""
    msgs: list[AnyMessage] = [_agent_inbound(source="agent:7")]
    assert tail_has_agent_inbound(msgs) is True


def test_tail_has_agent_inbound_user_only_no_ai_false():
    """Only a user inbound, no AIMessage -> False."""
    msgs: list[AnyMessage] = [
        inbound_message(content="hi", source="user", inbound_id=2, body_start=0)
    ]
    assert tail_has_agent_inbound(msgs) is False


@pytest.mark.parametrize(
    "codes,outputs,expected",
    [
        (["subprocess.run(['ls'])", "open('file')"], ["ok", "ok"], {"shell", "files"}),
        (
            ["shared_name = 1", "print(shared_name)"],
            ["ok", _nameerror_output("shared_name")],
            {"nameerror:shared_name"},
        ),
    ],
)
async def test_multiple_calls_match_results_by_id(
    _loaded: Any,
    codes: list[str],
    outputs: list[str],
    expected: set[str],
    *,
    database_gate: ProcessDbGate,
):
    ai = AIMessage(
        content="",
        tool_calls=[
            {"name": "execute_code", "args": {"code": code}, "id": str(i)}
            for i, code in enumerate(codes)
        ],
    )
    results = [ToolMessage(content=output, tool_call_id=str(i)) for i, output in enumerate(outputs)]
    state = _state([ai, *results, HumanMessage(content="attachment")])
    result = await _loaded.contribute().after_exec[0](
        state, _runtime(database_gate=database_gate), _config()
    )
    assert result is not None
    assert result["ava_sdk_reminder__reminded"] == expected
    assert len(result["messages"]) == len(expected)
