"""subscribe_interrupt / has_pending_interrupt — durable DB interrupt delivery.

The in-flight node watches for a pending cancel/terminate inbound via a short
DB poll (the watcher deliberately does NOT share the agent's Redis inbound
listener with the claim node's idle wait — see `agent/graph/_interrupt.py` for
the lost-wake incident that motivated the decoupling), so a signal is never
dropped: a cancel that lands while no node is interruptible stays a pending
row, caught by the next claim pass (covered in test_claim) — and one that
lands just before / during a node is caught here.
"""

import asyncio

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.db import has_pending_interrupt, pending_interrupt_reason
from agent.graph._interrupt import subscribe_interrupt
from shared.db import create_agent
from shared.inbound import InterruptReason
from shared.machine import machine_name

# The watcher polls on a 2s cadence; the initial SELECT is immediate. Generous
# windows vs flake; the poll-interval tests are serial (flaky-marked) because
# they depend on real DB IO timing.
_TIMEOUT_S = 5.0

# The maintenance restart payload key (`payload ? 'maintenance'`) is part of
# the reap shape (task #4027): a restart without it is not the drain's signal.
_MAINTENANCE_PAYLOAD: dict[str, object] = {
    "maintenance": {"holder": "ops:test:interrupt", "acquired_at": "2026-09-19T00:00:00+00:00"}
}


def _insert(
    conn: psycopg.Connection,
    agent_id: int,
    kind: str,
    source: str = "user",
    payload: dict[str, object] | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, '', %s, %s, %s)",
            (agent_id, kind, source, Jsonb(payload) if payload is not None else None),
        )
    conn.commit()


class TestHasPendingInterrupt:
    @pytest.mark.parametrize(
        ("source", "reason"),
        [("user", InterruptReason.USER), ("agent:9", InterruptReason.SYSTEM)],
    )
    async def test_reason_retains_first_pending_command(
        self, db_conn, aops_pool: AsyncConnectionPool, source: str, reason: InterruptReason
    ):
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(db_conn, tid, "cancel", source=source)  # pyright: ignore[reportUnknownArgumentType]
        _insert(db_conn, tid, "cancel", source="user")  # pyright: ignore[reportUnknownArgumentType]
        assert await pending_interrupt_reason(aops_pool, tid) is reason
        async with subscribe_interrupt(aops_pool, tid) as event:
            await asyncio.wait_for(event.wait(), timeout=_TIMEOUT_S)
            assert event.reason is reason
            event.set(
                InterruptReason.SYSTEM if reason is InterruptReason.USER else InterruptReason.USER
            )
            assert event.reason is reason
        assert db_conn.execute(  # pyright: ignore[reportUnknownMemberType]
            "SELECT status,claimed_at FROM inbound_messages WHERE agent_id=%s ORDER BY id",
            (tid,),
        ).fetchall() == [("pending", None), ("pending", None)]

    async def test_false_when_empty(self, db_conn, aops_pool: AsyncConnectionPool):
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        assert await has_pending_interrupt(aops_pool, tid) is False

    async def test_true_on_cancel(self, db_conn, aops_pool: AsyncConnectionPool):
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(db_conn, tid, "cancel")  # pyright: ignore[reportUnknownArgumentType]
        assert await has_pending_interrupt(aops_pool, tid) is True

    async def test_true_on_terminate(self, db_conn, aops_pool: AsyncConnectionPool):
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(db_conn, tid, "terminate")  # pyright: ignore[reportUnknownArgumentType]
        assert await has_pending_interrupt(aops_pool, tid) is True

    async def test_false_on_chat_only(self, db_conn, aops_pool: AsyncConnectionPool):
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(db_conn, tid, "chat")  # pyright: ignore[reportUnknownArgumentType]
        assert await has_pending_interrupt(aops_pool, tid) is False

    async def test_ignores_other_agent(self, db_conn, aops_pool: AsyncConnectionPool):
        mine = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        other = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(db_conn, other, "cancel")  # pyright: ignore[reportUnknownArgumentType]
        assert await has_pending_interrupt(aops_pool, mine) is False

    async def test_ignores_self_initiated_terminate(self, db_conn, aops_pool: AsyncConnectionPool):
        # ava.self.terminate() inserts a terminate row source='self' then raises
        # AgentTermination in-thread; the in-flight watcher must NOT fire on it
        # (it would inject a KeyboardInterrupt into the self-terminating thread,
        # racing the clean lifecycle exit). claim still dispatches the row.
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(db_conn, tid, "terminate", source="self")  # pyright: ignore[reportUnknownArgumentType]
        assert await has_pending_interrupt(aops_pool, tid) is False

    async def test_external_terminate_still_fires(self, db_conn, aops_pool: AsyncConnectionPool):
        # a peer / admin / user terminate (non-self source) does interrupt mid-turn
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(db_conn, tid, "terminate", source="agent:9")  # pyright: ignore[reportUnknownArgumentType]
        assert await has_pending_interrupt(aops_pool, tid) is True

    async def test_true_on_straggler_reap_mark(self, db_conn, aops_pool: AsyncConnectionPool):
        # Task #4016: the drain CAS-marked the row 'restarting' while its
        # un-applied maintenance restart is still pending/claimed — that pair
        # IS the durable truncation signal for this agent's in-flight turn.
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(
            db_conn,  # pyright: ignore[reportUnknownArgumentType]
            tid,
            "restart",
            source="system:maintenance",
            payload=_MAINTENANCE_PAYLOAD,
        )
        db_conn.execute(  # pyright: ignore[reportUnknownMemberType]
            "INSERT INTO agents_meta(id,status,machine,runtime_kind) "
            "VALUES(%s,'restarting',%s,'hosted')",
            (tid, machine_name()),
        )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]
        assert await has_pending_interrupt(aops_pool, tid) is True
        assert await pending_interrupt_reason(aops_pool, tid) is InterruptReason.SYSTEM

    async def test_false_on_unmarked_maintenance_restart(
        self, db_conn, aops_pool: AsyncConnectionPool
    ):
        # The pre-#4016 drain waits for the turn boundary: a pending restart
        # alone never aborts in-flight work.
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(
            db_conn,  # pyright: ignore[reportUnknownArgumentType]
            tid,
            "restart",
            source="system:maintenance",
            payload=_MAINTENANCE_PAYLOAD,
        )
        db_conn.execute(  # pyright: ignore[reportUnknownMemberType]
            "INSERT INTO agents_meta(id,status,machine,runtime_kind) "
            "VALUES(%s,'running',%s,'hosted')",
            (tid, machine_name()),
        )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]
        assert await has_pending_interrupt(aops_pool, tid) is False

    async def test_false_on_reap_mark_with_a_resolved_command(
        self, db_conn, aops_pool: AsyncConnectionPool
    ):
        # A mark whose command already applied is not a truncation signal —
        # the turn reached its boundary; the settle face owns such a row.
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(
            db_conn,  # pyright: ignore[reportUnknownArgumentType]
            tid,
            "restart",
            source="system:maintenance",
            payload=_MAINTENANCE_PAYLOAD,
        )
        from uuid import uuid4

        owner, generation = uuid4(), uuid4()
        db_conn.execute(  # pyright: ignore[reportUnknownMemberType]
            "UPDATE inbound_messages SET status='claimed', claimed_at=clock_timestamp(), "
            "target_generation=%s, target_owner=%s, applied_at=clock_timestamp() "
            "WHERE agent_id=%s AND kind='restart'",
            (generation, owner, tid),
        )
        db_conn.execute(  # pyright: ignore[reportUnknownMemberType]
            "INSERT INTO agents_meta(id,status,machine,runtime_kind) "
            "VALUES(%s,'restarting',%s,'hosted')",
            (tid, machine_name()),
        )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]
        assert await has_pending_interrupt(aops_pool, tid) is False

    async def test_false_on_reap_mark_with_a_non_maintenance_restart(
        self, db_conn, aops_pool: AsyncConnectionPool
    ):
        # Only the maintenance restart names the reap shape (task #4027): a
        # 'restarting' row with any other un-applied restart must not fire.
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(db_conn, tid, "restart")  # pyright: ignore[reportUnknownArgumentType]
        db_conn.execute(  # pyright: ignore[reportUnknownMemberType]
            "INSERT INTO agents_meta(id,status,machine,runtime_kind) "
            "VALUES(%s,'restarting',%s,'hosted')",
            (tid, machine_name()),
        )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]
        assert await has_pending_interrupt(aops_pool, tid) is False


class TestSubscribeInterrupt:
    @pytest.mark.flaky  # initial SELECT fire within a real IO window
    async def test_fires_on_already_pending_cancel(self, db_conn, aops_pool: AsyncConnectionPool):
        # cancel landed BEFORE the node subscribed — the initial SELECT catches it.
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(db_conn, tid, "cancel")  # pyright: ignore[reportUnknownArgumentType]
        async with subscribe_interrupt(aops_pool, tid) as event:
            await asyncio.wait_for(event.wait(), timeout=_TIMEOUT_S)
            assert event.is_set()

    @pytest.mark.flaky  # poll cadence + real DB IO window
    async def test_fires_on_cancel_inserted_after_subscribe(
        self, db_conn, aops_pool: AsyncConnectionPool
    ):
        # cancel arrives mid-action -> the watcher's next DB poll catches it
        # within one poll interval.
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        async with subscribe_interrupt(aops_pool, tid) as event:
            assert not event.is_set()
            _insert(db_conn, tid, "cancel")  # pyright: ignore[reportUnknownArgumentType]
            await asyncio.wait_for(event.wait(), timeout=_TIMEOUT_S)
            assert event.is_set()

    @pytest.mark.flaky  # initial SELECT fire within a real IO window
    async def test_fires_on_terminate(self, db_conn, aops_pool: AsyncConnectionPool):
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(db_conn, tid, "terminate")  # pyright: ignore[reportUnknownArgumentType]
        async with subscribe_interrupt(aops_pool, tid) as event:
            await asyncio.wait_for(event.wait(), timeout=_TIMEOUT_S)
            assert event.is_set()

    async def test_does_not_fire_on_chat(self, db_conn, aops_pool: AsyncConnectionPool):
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(db_conn, tid, "chat")  # pyright: ignore[reportUnknownArgumentType]
        async with subscribe_interrupt(aops_pool, tid) as event:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=0.5)
            assert not event.is_set()

    async def test_none_pool_never_fires(self):
        # container/eval: no inbound queue -> the wrapped action is uninterruptible.
        async with subscribe_interrupt(None, 1) as event:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=0.3)
            assert not event.is_set()


class TestWatcherDecoupledFromSharedListener:
    """The watcher must never touch the agent's Redis inbound listener — that
    listener is owned by the claim node's idle wait. Sharing it was the root
    cause of the lost-wake incident (2026-08-02, agent 2476: an orphaned
    watcher held the listener lock while the wake publish for a fresh inbound
    went unheard → 30s SELECT-recheck pickup)."""

    async def test_watcher_never_uses_listener(self, db_conn, aops_pool: AsyncConnectionPool):
        """A listener whose surface raises on any touch still works: the
        watcher polls the DB only."""
        calls: list[str] = []

        class _BoomListener:
            async def ensure_listening(self) -> None:  # pragma: no cover
                calls.append("ensure_listening")
                raise AssertionError("watcher must not call ensure_listening")

            async def wait_one(self, timeout: float) -> None:  # pragma: no cover
                calls.append("wait_one")
                raise AssertionError("watcher must not call wait_one")

            async def close(self) -> None:  # pragma: no cover
                calls.append("close")

        # prove the subscribe path itself does not require a listener at all:
        # pool + agent_id are the only inputs (the old signature took one).
        _ = _BoomListener()
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(db_conn, tid, "cancel")  # pyright: ignore[reportUnknownArgumentType]
        async with subscribe_interrupt(aops_pool, tid) as event:
            await asyncio.wait_for(event.wait(), timeout=_TIMEOUT_S)
            assert event.is_set()
        assert not calls, f"watcher touched the listener: {calls}"

    async def test_cancel_surviving_watcher_exits_promptly(
        self, db_conn, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
    ):
        """Even when a cancellation is swallowed (the cancel-vs-completion race
        that orphaned watchers in the shared-listener design), the stop belt
        terminates the watcher at its next loop check — and because it holds
        no shared resource, a lingering survivor is inert."""
        from agent.graph import _interrupt as mod

        real = mod.pending_interrupt_reason
        entered = asyncio.Event()
        swallowed = False

        async def _swallow_once(pool, agent_id):
            # Deterministic race simulation: the watcher is "inside the SELECT"
            # (blocked here) when the turn ends; the cancellation lands at this
            # await and is swallowed, then the SELECT "completes" normally.
            nonlocal swallowed
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                swallowed = True
            return await real(pool, agent_id)  # pyright: ignore[reportUnknownArgumentType]

        monkeypatch.setattr(mod, "_WATCHER_EXIT_TIMEOUT_S", 0.5)
        monkeypatch.setattr(mod, "_INTERRUPT_POLL_S", 0.05)
        monkeypatch.setattr(mod, "pending_interrupt_reason", _swallow_once)  # pyright: ignore[reportUnknownArgumentType]

        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        async with subscribe_interrupt(aops_pool, tid):
            await entered.wait()  # watcher is inside the SELECT when we exit
            watchers = [
                t
                for t in asyncio.all_tasks()
                if "_watch_for_interrupt" in t.get_coro().__qualname__  # type: ignore[union-attr]
            ]
        assert watchers, "expected the watcher task to be observable"
        _, pending = await asyncio.wait(watchers, timeout=3.0)
        assert not pending, "cancel-surviving watcher still running — stop signal not honored"
        assert swallowed, "the swallow-once path never exercised — race not simulated"

    async def test_survived_cancel_never_fires_after_stop(
        self, db_conn, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
    ):
        """P0-2 regression: in the lost-cancel race the watcher can return
        "normally" (the CancelledError was swallowed by a completing await).
        The stop belt must make that survivor exit at its next loop check —
        and the `if not stop.is_set()` guard must keep it from firing the
        event after the node's turn already ended (a spurious interrupt would
        inject a KeyboardInterrupt into the next action)."""
        from agent.graph import _interrupt as mod

        real = mod.pending_interrupt_reason
        recorded_events: list[asyncio.Event] = []
        entered = asyncio.Event()
        swallowed = False

        async def _swallow_once(pool, agent_id):
            nonlocal swallowed
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                swallowed = True  # cancel lost; the SELECT "completes" normally
            return await real(pool, agent_id)  # pyright: ignore[reportUnknownArgumentType]

        real_watch = mod._watch_for_interrupt

        async def _recording_watch(pool, event, agent_id, stop):
            recorded_events.append(event)  # pyright: ignore[reportUnknownArgumentType]
            await real_watch(pool, event, agent_id, stop)  # pyright: ignore[reportUnknownArgumentType]

        monkeypatch.setattr(mod, "_WATCHER_EXIT_TIMEOUT_S", 1.0)
        monkeypatch.setattr(mod, "_INTERRUPT_POLL_S", 0.05)
        monkeypatch.setattr(mod, "pending_interrupt_reason", _swallow_once)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(mod, "_watch_for_interrupt", _recording_watch)  # pyright: ignore[reportUnknownArgumentType]

        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        async with subscribe_interrupt(aops_pool, tid):
            await entered.wait()  # watcher is inside the SELECT when we exit
        # turn over; the swallow-once path must have been exercised
        assert swallowed, "race not simulated — cancel was delivered cleanly"
        assert recorded_events, "watcher never started"
        # the survivor must not have fired its event after exit
        assert not recorded_events[0].is_set(), "survivor fired the event after stop"
        # and it must be gone within a bounded window (stop belt)
        watchers = [
            t
            for t in asyncio.all_tasks()
            if "_watch_for_interrupt" in t.get_coro().__qualname__  # type: ignore[union-attr]
        ]
        if watchers:
            _, pending = await asyncio.wait(watchers, timeout=3.0)
            assert not pending, "survivor still running after stop"

    async def test_none_pool_never_fires_after_decouple(self):
        async with subscribe_interrupt(None, 1) as event:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=0.3)
            assert not event.is_set()


class TestWatcherExitBounded:
    """Exit must not stall the turn when the watcher's cleanup is wedged (a
    frozen Postgres host / network blip can hold the DB call past the kernel's
    TCP retry budget)."""

    @pytest.mark.flaky  # wall-clock upper-bound assertion (elapsed < 2.0) on a bounded exit
    async def test_exit_abandons_wedged_watcher(
        self, monkeypatch: pytest.MonkeyPatch, loguru_records
    ):
        """A watcher whose cancellation cleanup never unwinds → exit returns
        within the bounded window and logs the abandonment."""
        from agent.graph import _interrupt as mod

        release = asyncio.Event()

        async def _wedged(*_a: object, **_k: object) -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await release.wait()  # cleanup blocked, like a wedged DB call
                raise

        monkeypatch.setattr(mod, "_watch_for_interrupt", _wedged)
        monkeypatch.setattr(mod, "_WATCHER_EXIT_TIMEOUT_S", 0.2)
        t0 = asyncio.get_running_loop().time()
        async with subscribe_interrupt(object(), 1):  # type: ignore[arg-type]
            await asyncio.sleep(
                0
            )  # let the watcher actually start (an unstarted task cancels instantly)
        elapsed = asyncio.get_running_loop().time() - t0
        assert elapsed < 2.0, f"exit took {elapsed:.2f}s — the bounded abandon did not bound"
        warnings = [r["message"] for r in loguru_records if "abandoning" in r["message"]]
        assert warnings
        # The warning must name WHERE the orphan is wedged: the await chain
        # walks down to this test's _wedged coroutine (suspended in
        # release.wait() during its cancellation cleanup).
        assert "wedged await chain" in warnings[0]
        assert "_wedged" in warnings[0], f"await chain missing from warning:\n{warnings[0]}"
        release.set()  # let the orphan unwind so the loop closes clean
        await asyncio.sleep(0.01)
        # The orphan's eventual fate is logged with its delay since abandonment.
        fates = [
            r["message"] for r in loguru_records if "abandoned interrupt watcher" in r["message"]
        ]
        assert len(fates) == 1, f"expected one orphan-fate line, got {fates}"  # pyright: ignore[reportUnknownArgumentType]
        assert "cancelled" in fates[0]

    async def test_exit_still_reaps_prompt_watcher(
        self, monkeypatch: pytest.MonkeyPatch, loguru_records
    ):
        """The normal path is unchanged: a healthy watcher unwinds on cancel
        immediately and no abandonment is logged."""
        from agent.graph import _interrupt as mod

        async def _healthy(*_a: object, **_k: object) -> None:
            await asyncio.sleep(3600)

        monkeypatch.setattr(mod, "_watch_for_interrupt", _healthy)
        async with subscribe_interrupt(object(), 1):  # type: ignore[arg-type]
            await asyncio.sleep(0)  # watcher running, suspended in its sleep
        assert not any("abandoning" in r["message"] for r in loguru_records)


@pytest.mark.parametrize("maintenance", [False, True])
async def test_auto_compaction_cancels_at_llm_node_without_replacing_context(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    maintenance: bool,
) -> None:
    import json
    from unittest.mock import MagicMock

    from langchain_core.messages import HumanMessage
    from langgraph.runtime import Runtime

    from agent.graph._llm import llm_node
    from agent.state import AgentState, CompactState
    from shared.context import AvaContext
    from shared.lm.context_budget import ContextBudget

    tid = create_agent(db_conn)
    monkeypatch.setattr("agent.graph._interrupt._INTERRUPT_POLL_S", 0.01)

    def small_budget(_model: str) -> ContextBudget:
        return ContextBudget(
            max_context_tokens=10_000, soft_compact_tokens=1, hard_compact_tokens=1
        )

    monkeypatch.setattr("agent.hooks.compact.resolve_context_budget", small_budget)
    started, settled = asyncio.Event(), asyncio.Event()

    async def summarizing(_messages: object, _llm: object) -> str:
        started.set()
        try:
            await asyncio.Future()
        finally:
            settled.set()
        raise AssertionError("unreachable")

    monkeypatch.setattr("agent.hooks.compact.generate_summary", summarizing)
    state = AgentState(
        messages=[HumanMessage(content="uncompacted original work " * 30)],
        compact=CompactState(version=4),
        halted=False,
    )
    publisher, model = MagicMock(), MagicMock()
    runtime = Runtime(context=AvaContext(ops_pool=aops_pool, llm=model, event_publisher=publisher))
    invocation = asyncio.create_task(
        llm_node(state, runtime, {"configurable": {"thread_id": str(tid)}})
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        if maintenance:
            db_conn.execute(
                "INSERT INTO agents_meta(id,status,machine,runtime_kind) "
                "VALUES(%s,'restarting',%s,'hosted')",
                (tid, machine_name()),
            )
            _insert(db_conn, tid, "restart", source="system:update", payload=_MAINTENANCE_PAYLOAD)
        else:
            _insert(db_conn, tid, "cancel")
        result = await asyncio.wait_for(invocation, timeout=3)
    finally:
        invocation.cancel()
        await asyncio.gather(invocation, return_exceptions=True)
    assert settled.is_set()
    assert result.goto == "claim"
    assert result.update == {"halted": True}
    assert state.compact.version == 4
    assert state.context_reset.tail == []
    assert state.messages[0].content == "uncompacted original work " * 30
    model.astream.assert_not_called()
    assert db_conn.execute(
        "SELECT status,claimed_at FROM inbound_messages WHERE agent_id=%s",
        (tid,),
    ).fetchall() == [("pending", None)]
    compact_events = [
        json.loads(call.args[0])
        for call in publisher.emit.call_args_list
        if json.loads(call.args[0])["role"].startswith("compact_")
    ]
    assert [event["role"] for event in compact_events] == ["compact_started", "compact_finished"]
    assert compact_events[1]["status"] == "replaced"
    assert compact_events[0]["compact_id"] == compact_events[1]["compact_id"]


async def test_model_completion_and_cancel_same_tick_discards_result() -> None:
    from agent.graph._interrupt import ModelInterruptedError, interruptible_model

    interrupted = asyncio.Event()
    interrupted.set()

    async def completed() -> str:
        return "a complete but superseded summary"

    with pytest.raises(ModelInterruptedError):
        await interruptible_model(completed(), interrupted)


@pytest.mark.parametrize("marker", ["compact_summary", "compact_request"])
async def test_compaction_returns_through_claim_then_generates_before_compacting_again(  # noqa: PLR0915 -- one checkpoint/cancel/resume transition proof.
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    marker: str,
) -> None:
    from typing import Any, cast
    from unittest.mock import AsyncMock, MagicMock

    from langchain_core.messages import AIMessageChunk, HumanMessage
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.graph.message import add_messages
    from langgraph.runtime import Runtime
    from langgraph.types import Command

    from agent.graph._claim import claim_node
    from agent.graph._init_context import init_context_node
    from agent.graph._llm import llm_node
    from agent.hooks.compact import _compact_reminder, auto_compact_will_fire
    from agent.state import AgentState, checkpoint_msgpack_allowlist
    from shared.context import AvaContext
    from shared.lm.context_budget import ContextBudget
    from tests.conftest import spawn_agent

    def small_budget(_model: str) -> ContextBudget:
        return ContextBudget(10_000, 1, 1)

    def apply(state: AgentState, command: Command[Any]) -> AgentState:
        assert isinstance(command.update, dict)
        update = dict(cast("dict[str, Any]", command.update))  # pyright: ignore[reportUnknownMemberType]
        if "messages" in update:
            update["messages"] = add_messages(list(state.messages), update["messages"])
        return state.model_copy(update=update)

    monkeypatch.setattr("agent.hooks.compact.resolve_context_budget", small_budget)
    monkeypatch.setattr("agent.graph._init_context.build_system_prompt", lambda: "standing head")
    monkeypatch.setattr("agent.graph._init_context.context_notes", list)
    summary = AsyncMock(return_value="the retained summary is still above the ceiling " * 30)
    monkeypatch.setattr("agent.hooks.compact.generate_summary", summary)

    async def ordinary_generation():
        yield AIMessageChunk(
            content="resumed ordinary work",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 500, "output_tokens": 5, "total_tokens": 505},
        )

    model, publisher = MagicMock(), MagicMock()
    model.bind_tools.return_value = model
    model.astream.return_value = ordinary_generation()
    tid = spawn_agent()
    config: RunnableConfig = {"configurable": {"thread_id": str(tid)}}
    runtime = Runtime(context=AvaContext(ops_pool=aops_pool, llm=model, event_publisher=publisher))
    state = AgentState(messages=[HumanMessage(content="old work " * 100)], halted=False)
    compacted = await llm_node(state, runtime, config)
    assert compacted.goto == "init_context"
    state = apply(state, compacted)
    state.context_reset.tail[0].additional_kwargs["ava_msg_type"] = marker
    rebuilt = await init_context_node(state, runtime, config)
    assert rebuilt.goto == "claim"
    state = apply(state, rebuilt)

    # A real checkpoint round trip and a cancel must preserve the exemption.
    serde = JsonPlusSerializer(allowed_msgpack_modules=checkpoint_msgpack_allowlist())
    state = state.model_copy(
        update={"messages": serde.loads_typed(serde.dumps_typed(state.messages))}
    )
    assert not auto_compact_will_fire(state)
    _insert(db_conn, tid, "cancel")
    cancelled = await claim_node(state, runtime, config)
    assert cancelled.goto == "claim"
    state = apply(state, cancelled)
    assert state.halted
    assert not auto_compact_will_fire(state)
    model.astream.assert_not_called()

    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,kind,source,content) VALUES(%s,'chat','user','continue')",
        (tid,),
    )
    db_conn.commit()
    resumed = await claim_node(state, runtime, config)
    assert resumed.goto == "before_llm"
    state = apply(state, resumed)
    assert not auto_compact_will_fire(state)
    assert await _compact_reminder(state, runtime, config) is None
    generated = await llm_node(state, runtime, config)
    assert generated.goto == "after_exec"
    state = apply(state, generated)
    assert state.messages[-1].content == "resumed ordinary work"
    assert state.compact.version == 1
    summary.assert_awaited_once()
    model.astream.assert_called_once()
    assert auto_compact_will_fire(state)  # A committed ordinary result re-arms the threshold.
