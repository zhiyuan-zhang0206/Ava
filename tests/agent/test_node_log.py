"""`node_lifecycle` helper unit tests — three exit paths emitting node_enter / node_exit.

Death observability: 159/160 silent death and 167/168 BadRequestError were both tracked
down from a single turn_end signal. turn_end is the LLM node boundary — you can't see
which node the process was last inside before dying: claim / before_llm / before_exec /
exec / after_exec. `node_lifecycle` wraps each graph node with an enter + exit event,
outcome distinguishes ok / cancelled / exception:Type, and the exception path also
carries a traceback (via `_postgres_sink` auto-injecting record["exception"]).
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import io
import json
import sys
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import SystemMessage

from agent.graph._node_log import node_lifecycle
from shared.config import settings


def _enter_records(records):
    return [r for r in records if r["extra"].get("event") == "node_enter"]  # pyright: ignore[reportUnknownMemberType]


def _exit_records(records):
    return [r for r in records if r["extra"].get("event") == "node_exit"]  # pyright: ignore[reportUnknownMemberType]


def _wrap(node: str, msg_count: int, pub=None):
    """Construct a node_lifecycle context with a stub event publisher (emit is
    the only call site) + `msg_count` SystemMessages (used here purely as filler
    counted toward msg_count) + ops_pool=None (no inbound anchors needed)."""
    return node_lifecycle(
        node,
        messages=[SystemMessage(content="x")] * msg_count,
        ops_pool=None,
        event_publisher=pub if pub is not None else MagicMock(),  # pyright: ignore[reportUnknownArgumentType]
        agent_id=1,
    )


async def test_node_lifecycle_emits_enter_and_ok_exit(loguru_records) -> None:
    """Normal yield completes → enter + exit(outcome=ok), exit record has no exception."""

    async with _wrap("claim", 3):
        pass

    enters = _enter_records(loguru_records)  # pyright: ignore[reportUnknownArgumentType]
    exits = _exit_records(loguru_records)  # pyright: ignore[reportUnknownArgumentType]
    assert len(enters) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert enters[0]["extra"]["node"] == "claim"
    assert enters[0]["extra"]["msg_count"] == 3
    assert len(exits) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert exits[0]["extra"]["outcome"] == "ok"
    assert exits[0]["extra"]["node"] == "claim"
    assert exits[0]["exception"] is None


async def test_node_lifecycle_emits_cancelled_on_cancellederror(loguru_records) -> None:
    """asyncio.CancelledError → outcome=cancelled, not treated as exception with traceback;
    cancel is an expected lifecycle signal (cancel turn / agent restart path), it must
    not pollute the sidebar exception count."""

    async def _body():
        async with _wrap("llm", 5):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _body()

    exits = _exit_records(loguru_records)  # pyright: ignore[reportUnknownArgumentType]
    assert len(exits) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert exits[0]["extra"]["outcome"] == "cancelled"
    assert (
        exits[0]["level"].name == "INFO"  # pyright: ignore[reportUnknownMemberType]
    )  # cancelled is not WARNING  # pyright: ignore[reportUnknownMemberType]


async def test_node_lifecycle_emits_exception_with_traceback(loguru_records) -> None:
    """Business exception → outcome=exception:Type, exit record carries the active
    exception so `_postgres_sink` can put traceback / exception_type into
    events.payload. This is a regression test for the PR #60 latent bug fix:
    try/except + logger.opt(exception=True) captures inside the except block;
    finally couldn't reach it."""

    async def _body():
        async with _wrap("exec", 10):
            raise RuntimeError("simulated exec crash")

    with pytest.raises(RuntimeError, match="simulated exec crash"):
        await _body()

    exits = _exit_records(loguru_records)  # pyright: ignore[reportUnknownArgumentType]
    assert len(exits) == 1  # pyright: ignore[reportUnknownArgumentType]
    rec = exits[0]
    assert rec["extra"]["outcome"] == "exception:RuntimeError"
    # User ruling 2026-08-04: node transitions are never WARNING — all outcomes
    # log at INFO; the traceback in payload keeps the exception queryable.
    assert rec["level"].name == "INFO"  # pyright: ignore[reportUnknownMemberType]
    assert rec["exception"] is not None, (
        "logger.opt(exception=True) must attach an exception object — _postgres_sink relies on it to inject traceback"
    )
    assert rec["exception"].type is RuntimeError  # pyright: ignore[reportUnknownMemberType]
    assert "simulated exec crash" in str(rec["exception"].value)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


async def test_node_lifecycle_msg_count_in_enter(loguru_records) -> None:
    """enter event payload carries msg_count — lets death analysis see "how large was
    state.messages at death", for investigating compaction / abnormal truncation and
    other data issues."""
    async with _wrap("before_llm", 42):
        pass
    enters = _enter_records(loguru_records)  # pyright: ignore[reportUnknownArgumentType]
    assert enters[0]["extra"]["msg_count"] == 42


def _patch_faulthandler(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Record (arm, threshold) / (cancel,) without touching the real timer."""
    calls: list[tuple] = []

    def _arm(threshold, **_kwargs):
        calls.append(("arm", threshold))  # pyright: ignore[reportUnknownMemberType]

    def _cancel():
        calls.append(("cancel",))  # pyright: ignore[reportUnknownMemberType]

    monkeypatch.setattr(faulthandler, "dump_traceback_later", _arm)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(faulthandler, "cancel_dump_traceback_later", _cancel)
    return calls


async def test_stall_guard_disabled_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """node_stall_dump_seconds == 0 (prod default) → never arms the timer."""
    monkeypatch.setattr(settings.agent, "node_stall_dump_seconds", 0.0)
    calls = _patch_faulthandler(monkeypatch)
    async with _wrap("claim", 1):
        pass
    assert calls == []


async def test_stall_guard_exempts_claim_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """The claim node legitimately parks in the idle wait for hours — arming
    the guard there would fire a full stack dump on every idle period, so it
    never arms even with the probe enabled. This is what makes a prod-enabled
    threshold safe."""
    monkeypatch.setattr(settings.agent, "node_stall_dump_seconds", 12.0)
    calls = _patch_faulthandler(monkeypatch)
    async with _wrap("claim", 1):
        pass
    assert calls == []


async def test_stall_guard_arms_with_threshold_and_cancels(monkeypatch: pytest.MonkeyPatch) -> None:
    """node_stall_dump_seconds > 0 → arm while the body runs, cancel on the way out.
    faulthandler arms at threshold+2s — staggered after the loop-timer task
    dump so the two stderr writers (watchdog thread vs loop) don't interleave."""
    monkeypatch.setattr(settings.agent, "node_stall_dump_seconds", 12.0)
    calls = _patch_faulthandler(monkeypatch)
    async with _wrap("llm", 1):
        assert calls == [("arm", 14.0)]  # armed for the duration of the body
    assert calls == [("arm", 14.0), ("cancel",)]


async def test_stall_guard_cancels_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """A node body raising must still disarm the timer (finally), or a later
    fast node would inherit a stale armed dump."""
    monkeypatch.setattr(settings.agent, "node_stall_dump_seconds", 5.0)
    calls = _patch_faulthandler(monkeypatch)
    with pytest.raises(RuntimeError, match="boom"):
        async with _wrap("exec", 1):
            raise RuntimeError("boom")
    assert calls == [("arm", 7.0), ("cancel",)]


async def test_stall_guard_survives_redirected_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a leaked exec-stream redirect leaves the process-global
    sys.stderr pointed at a fileno-less capture buffer — a plugin mutating
    sys.stderr, or any redirect that never unwinds. The next node's stall
    guard must hand faulthandler a stable real-fd stderr, never that live
    sys.stderr; passing a fileno-less stream raises io.UnsupportedOperation
    and kills the agent the diagnostic exists to observe."""
    from agent.graph._exec_stream import StreamingTextIO

    monkeypatch.setattr(settings.agent, "node_stall_dump_seconds", 12.0)
    monkeypatch.setattr(sys, "stderr", StreamingTextIO())  # the leaked redirect

    armed: list = []

    def _arm(_threshold, *, file, **_kwargs):
        file.fileno()  # mimic faulthandler's contract: it requires a usable fd  # pyright: ignore[reportUnknownMemberType]
        armed.append(file)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    monkeypatch.setattr(faulthandler, "dump_traceback_later", _arm)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(faulthandler, "cancel_dump_traceback_later", lambda: None)

    # Before the fix this raises io.UnsupportedOperation: fileno out of __aenter__.
    async with _wrap("after_exec", 1):
        pass

    assert len(armed) == 1  # pyright: ignore[reportUnknownArgumentType]
    real_stderr = sys.__stderr__
    assert real_stderr is not None
    assert (
        armed[0].fileno() == real_stderr.fileno()  # pyright: ignore[reportUnknownMemberType]
    )  # real fd, not the buffer  # pyright: ignore[reportUnknownMemberType]


async def test_dump_async_tasks_names_the_full_await_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """The task dump must walk cr_await down to the LEAF awaited frame — a
    suspended Task.get_stack() only shows the outermost suspension point, which
    cannot name the blocked call. Spawn a task awaiting through a nested
    coroutine and assert both the outer and the inner function names appear.

    Reads from sys.__stderr__ (the real process stderr the dump targets), not
    the swappable sys.stderr the exec stream redirects — so the dump still lands
    when a leaked redirect has replaced sys.stderr."""
    from agent.graph._node_log import _dump_async_tasks

    sink = io.StringIO()
    monkeypatch.setattr(sys, "__stderr__", sink)

    async def _inner_leaf_await() -> None:
        await asyncio.sleep(30)

    async def _outer_wrapper() -> None:
        await _inner_leaf_await()

    task = asyncio.create_task(_outer_wrapper(), name="stall-probe-target")
    await asyncio.sleep(0)  # let the task reach its suspension point
    try:
        _dump_async_tasks()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    err = sink.getvalue()
    assert "asyncio task await chains (stall probe" in err
    assert "stall-probe-target" in err
    assert "_outer_wrapper" in err  # outermost coroutine frame
    assert "_inner_leaf_await" in err  # nested frame — the chain walk
    assert "in sleep" in err  # the true leaf: asyncio.sleep's own frame


async def test_node_lifecycle_publishes_timeline_snapshot() -> None:
    """On enter, publish a timeline_snapshot rendered from the in-memory
    state.messages → the gateway forwards it, driving the frontend partial
    reset protocol. Carries msg_count = len(state.messages)."""
    pub = MagicMock()
    async with _wrap("llm", 7, pub=pub):
        pass
    assert pub.emit.call_count >= 1
    payloads = [json.loads(c.args[0]) for c in pub.emit.call_args_list]
    snapshots = [p for p in payloads if p["role"] == "timeline_snapshot"]
    assert len(snapshots) == 1
    assert snapshots[0]["agent_id"] == 1
    assert snapshots[0]["msg_count"] == 7


async def test_node_lifecycle_first_snapshot_carries_system_prompt_then_drops_it() -> None:
    """The FIRST (full-window) snapshot of a process lifetime carries the
    system-prompt item; every later incremental snapshot never does.

    No special-casing: the full-window path renders the whole history (which
    includes 0.0); the incremental path renders only messages past the cursor
    (message 0 is always below it). #615: at spawn the checkpoint is empty
    (first super-step not yet committed), so GET /timeline returns no items —
    the first full-window snapshot is the only source of 0.0, and it must
    carry it.
    """
    from langchain_core.messages import AIMessage

    pub = MagicMock()
    msgs = [SystemMessage(content="the prompt"), AIMessage(content="hello")]
    async with node_lifecycle(
        "llm",
        messages=msgs,
        ops_pool=None,
        event_publisher=pub,
        agent_id=1,
    ):
        pass
    # third enter with a new committed message — incremental path, must NOT
    # carry 0.0 (the frontend keeps its copy after the first snapshot)
    msgs2 = [*msgs, AIMessage(content="second")]
    async with node_lifecycle(
        "llm",
        messages=msgs2,
        ops_pool=None,
        event_publisher=pub,
        agent_id=1,
    ):
        pass
    payloads = [json.loads(c.args[0]) for c in pub.emit.call_args_list]
    snapshots = [p for p in payloads if p["role"] == "timeline_snapshot"]
    assert len(snapshots) == 2
    # first snapshot: carries 0.0 + the committed message
    assert snapshots[0]["msg_count"] == 2
    first_items = snapshots[0]["items"]
    assert [it["item_id"] for it in first_items] == ["0.0", "1.0"]
    assert first_items[0]["kind"] == "system_prompt"
    # incremental snapshot: only the new message, no 0.0
    assert [it["item_id"] for it in snapshots[1]["items"]] == ["2.0"]


def _snapshots_from(pub) -> list[dict]:
    payloads = [json.loads(c.args[0]) for c in pub.emit.call_args_list]  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    return [p for p in payloads if p["role"] == "timeline_snapshot"]


async def test_node_lifecycle_incremental_second_enter_emits_only_new_messages() -> None:
    """Incremental design: the second enter (same process, cursor set by the
    first) publishes ONLY the messages committed since — with msg_count still
    the FULL history length."""
    pub = MagicMock()
    from langchain_core.messages import AIMessage

    # first enter: full-window path (cursor == 0), 1 message
    async with node_lifecycle(
        "llm",
        messages=[SystemMessage(content="prompt"), AIMessage(content="first")],
        ops_pool=None,
        event_publisher=pub,
        agent_id=1,
    ):
        pass
    # second enter: same state + one more committed message
    async with node_lifecycle(
        "llm",
        messages=[
            SystemMessage(content="prompt"),
            AIMessage(content="first"),
            AIMessage(content="second"),
        ],
        ops_pool=None,
        event_publisher=pub,
        agent_id=1,
    ):
        pass
    snaps = _snapshots_from(pub)
    assert len(snaps) == 2  # pyright: ignore[reportUnknownArgumentType]
    # second snapshot is incremental: only message 2, but full msg_count
    assert [it["item_id"] for it in snaps[1]["items"]] == ["2.0"]
    assert snaps[1]["msg_count"] == 3


async def test_node_lifecycle_empty_incremental_window_skips_emit() -> None:
    """A node enter with nothing new commits renders an empty incremental
    window and must NOT emit (SSE noise at node-enter frequency)."""
    pub = MagicMock()
    async with _wrap("llm", 7, pub=pub):
        pass
    # identical state again — no new commits
    async with _wrap("llm", 7, pub=pub):
        pass
    snaps = _snapshots_from(pub)
    assert (
        len(snaps) == 1  # pyright: ignore[reportUnknownArgumentType]
    )  # only the first (full-window) enter emitted  # pyright: ignore[reportUnknownArgumentType]


async def test_node_lifecycle_empty_full_window_skips_emit() -> None:
    """A full-window enter with an empty history (the post-REMOVE_ALL
    init_context enter after a compaction) renders an empty window and must
    NOT emit — the empty snapshot was the trigger that blanked the frontend's
    context panel inside its compact-reset window before the rebuilt-history
    snapshot arrived (the "context UI doesn't refresh after compact" report).
    """
    from langchain_core.messages import AIMessage

    pub = MagicMock()
    msgs = [SystemMessage(content="x"), AIMessage(content="a")]
    async with node_lifecycle("llm", messages=msgs, ops_pool=None, event_publisher=pub, agent_id=1):
        pass
    # compact: history wiped to zero — the next enter is the full-window path
    # (len(messages) < cursor) with an empty render.
    async with node_lifecycle("llm", messages=[], ops_pool=None, event_publisher=pub, agent_id=1):
        pass
    snaps = _snapshots_from(pub)
    assert (
        len(snaps) == 1  # pyright: ignore[reportUnknownArgumentType]
    )  # only the first (non-empty) enter emitted  # pyright: ignore[reportUnknownArgumentType]
    # The skip does NOT advance the cursor (the "never advance past a render
    # that produced nothing" rule), so the pre-shrink cursor stays. A rebuilt
    # history SHORTER than that cursor (the real post-compact head) still
    # takes the full-window path on the next enter and re-renders everything.
    async with node_lifecycle(
        "llm",
        messages=[SystemMessage(content="x"), AIMessage(content="b")],
        ops_pool=None,
        event_publisher=pub,
        agent_id=1,
    ):
        pass
    snaps = _snapshots_from(pub)
    assert (
        len(snaps) == 1  # pyright: ignore[reportUnknownArgumentType]
    )  # len(2) == cursor(2) → incremental path, nothing new  # pyright: ignore[reportUnknownArgumentType]
    # A history strictly shorter than the cursor re-renders full-window.
    async with node_lifecycle(
        "llm",
        messages=[SystemMessage(content="x")],
        ops_pool=None,
        event_publisher=pub,
        agent_id=1,
    ):
        pass
    snaps = _snapshots_from(pub)
    assert len(snaps) == 2  # pyright: ignore[reportUnknownArgumentType]
    assert snaps[1]["msg_count"] == 1


async def test_node_lifecycle_full_window_skips_anchors_query_for_modern_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full-window path must not query chat inbound anchors when no
    message in the history needs them (all-modern = every inbound carries its
    own ava_created_at). The post-compact rebuilt history is exactly such a
    case — skipping the query removes a DB round trip from the snapshot the
    frontend is waiting on after compact_done (seconds on remote machines).
    """
    from langchain_core.messages import HumanMessage

    import agent.graph._node_log as nl
    from shared.message_kwargs import AvaMsgType

    queried: list[int] = []

    async def fake_anchors(pool, agent_id):
        queried.append(agent_id)  # pyright: ignore[reportUnknownArgumentType]
        return []

    monkeypatch.setattr(nl, "list_chat_inbound_anchors", fake_anchors)  # pyright: ignore[reportUnknownArgumentType]
    pool = MagicMock()

    # All-modern history: inbound carries ava_created_at → no query.
    modern = HumanMessage(
        content="hi",
        additional_kwargs={
            "ava_msg_type": AvaMsgType.INBOUND.value,
            "ava_created_at": "2026-08-02T00:00:00+00:00",
        },
    )
    pub = MagicMock()
    async with node_lifecycle(
        "llm",
        messages=[SystemMessage(content="x"), modern],
        ops_pool=pool,
        event_publisher=pub,
        agent_id=7,
    ):
        pass
    assert queried == [], f"anchors queried for an all-modern history: {queried}"
    snaps = _snapshots_from(pub)
    assert len(snaps) == 1  # pyright: ignore[reportUnknownArgumentType]

    # Legacy inbound (no ava_created_at) → query still happens. Fresh agent
    # id so the enter takes the full-window path (agent 7's cursor already
    # advanced past it).
    legacy = HumanMessage(
        content="old", additional_kwargs={"ava_msg_type": AvaMsgType.INBOUND.value}
    )
    pub2 = MagicMock()
    async with node_lifecycle(
        "llm",
        messages=[SystemMessage(content="x"), legacy],
        ops_pool=pool,
        event_publisher=pub2,
        agent_id=8,
    ):
        pass
    assert queried == [8]


async def test_node_lifecycle_cursor_reset_after_shrink_forces_full_window() -> None:
    """Compaction shrinks history below the cursor — the next enter must
    degrade to the full-window path (and the shrink is detectable)."""
    from langchain_core.messages import AIMessage

    pub = MagicMock()
    msgs42 = [SystemMessage(content="x")] + [AIMessage(content=str(i)) for i in range(41)]
    async with node_lifecycle(
        "llm", messages=msgs42, ops_pool=None, event_publisher=pub, agent_id=1
    ):
        pass
    # compact: history shrinks to 3
    msgs3 = [SystemMessage(content="x"), AIMessage(content="a"), AIMessage(content="b")]
    async with node_lifecycle(
        "llm", messages=msgs3, ops_pool=None, event_publisher=pub, agent_id=1
    ):
        pass
    snaps = _snapshots_from(pub)
    assert len(snaps) == 2  # pyright: ignore[reportUnknownArgumentType]
    # second snapshot is the full tail window of the new history — the whole
    # 3-item history (0.0 included; no drop rule anymore)
    assert snaps[1]["msg_count"] == 3
    assert [it["item_id"] for it in snaps[1]["items"]] == ["0.0", "1.0", "2.0"]


async def test_node_lifecycle_full_window_flag_forces_full_snapshot() -> None:
    """full_window=True (claim turn-end fallback) publishes the tail window
    even when the cursor is current, and advances the cursor."""
    pub = MagicMock()
    from langchain_core.messages import AIMessage

    msgs = [SystemMessage(content="prompt"), AIMessage(content="first")]
    async with node_lifecycle("llm", messages=msgs, ops_pool=None, event_publisher=pub, agent_id=1):
        pass
    # same state, but forced full window (claim entering idle)
    async with node_lifecycle(
        "llm",
        messages=msgs,
        ops_pool=None,
        event_publisher=pub,
        agent_id=1,
        full_window=True,
    ):
        pass
    snaps = _snapshots_from(pub)
    assert len(snaps) == 2  # pyright: ignore[reportUnknownArgumentType]
    # forced full-window re-renders the whole (short) history — 0.0 included
    assert [it["item_id"] for it in snaps[1]["items"]] == ["0.0", "1.0"]
    # cursor advanced: a third enter with the same state emits nothing
    async with node_lifecycle("llm", messages=msgs, ops_pool=None, event_publisher=pub, agent_id=1):
        pass
    assert len(_snapshots_from(pub)) == 2  # pyright: ignore[reportUnknownArgumentType]


async def test_node_lifecycle_render_failure_does_not_advance_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A render failure (fail-loud) propagates without advancing the cursor:
    a fresh process re-entering after a crash re-renders the full window
    instead of skipping everything past a phantom cursor."""
    from langchain_core.messages import AIMessage

    from agent.graph import _node_log as nl

    pub = MagicMock()
    msgs = [SystemMessage(content="x"), AIMessage(content="a"), AIMessage(content="b")]
    # first enter: render blows up (simulated crash)
    real_build = nl.build_timeline_items

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated render failure")

    monkeypatch.setattr(nl, "build_timeline_items", _boom)  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(RuntimeError, match="simulated render failure"):
        async with node_lifecycle(
            "llm", messages=msgs, ops_pool=None, event_publisher=pub, agent_id=1
        ):
            pass
    # cursor must be untouched (still 0) → the next enter goes full-window again
    monkeypatch.setattr(nl, "build_timeline_items", real_build)
    async with node_lifecycle("llm", messages=msgs, ops_pool=None, event_publisher=pub, agent_id=1):
        pass
    snaps = _snapshots_from(pub)
    assert len(snaps) == 1  # pyright: ignore[reportUnknownArgumentType]
    # first publish of the process → carries 0.0 (see #615)
    assert [it["item_id"] for it in snaps[0]["items"]] == ["0.0", "1.0", "2.0"]
