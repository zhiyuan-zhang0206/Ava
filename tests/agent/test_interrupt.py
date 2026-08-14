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
from psycopg_pool import AsyncConnectionPool

from agent.db import has_pending_interrupt
from agent.graph._interrupt import subscribe_interrupt
from shared.db import create_agent

# The watcher polls on a 2s cadence; the initial SELECT is immediate. Generous
# windows vs flake; the poll-interval tests are serial (flaky-marked) because
# they depend on real DB IO timing.
_TIMEOUT_S = 5.0


def _insert(conn: psycopg.Connection, agent_id: int, kind: str, source: str = "user") -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, '', %s, %s)",
            (agent_id, kind, source),
        )
    conn.commit()


class TestHasPendingInterrupt:
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

        real = mod.has_pending_interrupt
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
        monkeypatch.setattr(mod, "has_pending_interrupt", _swallow_once)  # pyright: ignore[reportUnknownArgumentType]

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

        real = mod.has_pending_interrupt
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
        monkeypatch.setattr(mod, "has_pending_interrupt", _swallow_once)  # pyright: ignore[reportUnknownArgumentType]
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
