"""Hosted-runner wake dispatch — `services/agent_host/dispatcher.py`.

Phase 1 work item (a) of `future/infra/agent-runner-as-server.md`. The contracts
locked here are the ones the hosted model cannot be correct without:

1. **A wake starts a turn** — and an idle agent has no task at all, which is the
   whole point of the model.
2. **One agent's turns serialize; different agents overlap.** The design allows
   concurrency across agents and forbids it within one, because a single agent's
   turns share a checkpointer thread.
3. **The wake race** — a wake landing while a turn is winding down must still be
   served. This is the subtle one, and it is why the scheduler exists at all.
4. **Wakes coalesce** — two wakes during one turn cause one more turn, not two.
5. **A crashing turn does not wedge its agent or the host.**
"""

from __future__ import annotations

import asyncio

import pytest

from services.agent_host.dispatcher import (
    InboundWakeDispatcher,
    TurnScheduler,
    agent_id_from_channel,
)


class _Recorder:
    """A `run_turn` that records calls and can be released on command."""

    def __init__(self) -> None:
        self.started: list[int] = []
        self.finished: list[int] = []
        self.gates: dict[int, asyncio.Event] = {}
        self.entered: dict[int, asyncio.Event] = {}

    def gate(self, agent_id: int) -> asyncio.Event:
        return self.gates.setdefault(agent_id, asyncio.Event())

    def arrival(self, agent_id: int) -> asyncio.Event:
        return self.entered.setdefault(agent_id, asyncio.Event())

    async def __call__(self, agent_id: int) -> None:
        self.started.append(agent_id)
        self.arrival(agent_id).set()
        gate = self.gates.get(agent_id)
        if gate is not None:
            await gate.wait()
            gate.clear()
        self.finished.append(agent_id)


async def _settle() -> None:
    """Let every ready callback run. Two hops: one for the task to be scheduled,
    one for it to run to its next await."""
    for _ in range(6):
        await asyncio.sleep(0)


class TestBasicDispatch:
    async def test_wake_starts_a_turn_and_the_task_ends_when_idle(self) -> None:
        rec = _Recorder()
        sched = TurnScheduler(rec)

        assert sched.active_agents == frozenset(), "an idle agent must have no task"
        sched.wake(7)
        await _settle()

        assert rec.started == [7]
        # The turn returned (nothing gated it), so the agent is idle again —
        # idle costs nothing because there is nothing.
        assert sched.active_agents == frozenset()

    async def test_second_wake_after_idle_starts_a_fresh_turn(self) -> None:
        rec = _Recorder()
        sched = TurnScheduler(rec)

        sched.wake(7)
        await _settle()
        sched.wake(7)
        await _settle()

        assert rec.started == [7, 7]


class TestSerializationAndConcurrency:
    async def test_one_agents_turns_never_overlap(self) -> None:
        """The same agent's turns must serialize — they share a checkpointer
        thread. A wake during a running turn may not start a second task."""
        rec = _Recorder()
        rec.gate(7)  # turn 7 blocks until released
        sched = TurnScheduler(rec)

        sched.wake(7)
        await _settle()
        assert rec.started == [7]

        sched.wake(7)  # arrives mid-turn
        await _settle()
        assert rec.started == [7], "a second concurrent turn was started for one agent"
        assert len(sched.active_agents) == 1

        rec.gate(7).set()
        await _settle()
        assert rec.started == [7, 7], "the mid-turn wake did not produce a follow-up turn"

    async def test_different_agents_run_concurrently(self) -> None:
        """Cross-agent concurrency is the point of the model — one blocked agent
        must not hold up another."""
        rec = _Recorder()
        rec.gate(1)
        rec.gate(2)
        sched = TurnScheduler(rec)

        sched.wake(1)
        sched.wake(2)
        await _settle()

        assert sorted(rec.started) == [1, 2]
        assert sched.active_agents == frozenset({1, 2})

        rec.gate(1).set()
        rec.gate(2).set()
        await _settle()
        assert sched.active_agents == frozenset()


class TestWakeRace:
    async def test_wake_landing_as_the_turn_winds_down_is_not_lost(self) -> None:
        """THE race (see the module docstring of dispatcher.py).

        The wake is delivered after the turn body has finished its work but
        before the task object is gone. Without the wake-pending flag the
        dispatcher would see a live task, assume it would pick the inbound up,
        and the row would sit unclaimed. With the flag, the pump re-checks and
        loops.

        `release_then_wake` reproduces the interleaving deterministically: it
        releases the gate (so the turn body returns) and wakes in the SAME
        synchronous stretch, which is exactly the window the flag protects.
        """
        rec = _Recorder()
        rec.gate(7)
        sched = TurnScheduler(rec)

        sched.wake(7)
        await _settle()
        assert rec.started == [7]

        # The turn is about to finish AND a wake arrives — the exact overlap.
        rec.gate(7).set()
        sched.wake(7)
        await _settle()

        assert rec.started == [7, 7], "the wake was swallowed by the winding-down turn"

    async def test_wake_after_the_task_is_gone_starts_a_new_one(self) -> None:
        """The other side of the same window: once the task has been removed, a
        wake must start a fresh task rather than be recorded against nothing."""
        rec = _Recorder()
        sched = TurnScheduler(rec)

        sched.wake(7)
        await _settle()
        assert sched.active_agents == frozenset()

        sched.wake(7)
        await _settle()
        assert rec.started == [7, 7]

    async def test_two_wakes_during_one_turn_coalesce(self) -> None:
        """Two wakes are one reason to run again: the follow-up turn drains
        whatever is pending, so a chat burst must not queue a turn burst."""
        rec = _Recorder()
        rec.gate(7)
        sched = TurnScheduler(rec)

        sched.wake(7)
        await _settle()
        sched.wake(7)
        sched.wake(7)
        sched.wake(7)
        rec.gate(7).set()
        await _settle()

        assert rec.started == [7, 7], f"wakes did not coalesce: {rec.started}"


class TestFailureIsolation:
    async def test_a_crashing_turn_drops_the_task_without_killing_the_host(self) -> None:
        """A turn that raises must not wedge its agent (the task has to be
        removed so the next wake can start one) and must not propagate."""
        calls: list[int] = []

        async def boom(agent_id: int) -> None:
            calls.append(agent_id)
            raise RuntimeError("turn exploded")

        sched = TurnScheduler(boom)
        sched.wake(7)
        await _settle()

        assert calls == [7]
        assert sched.active_agents == frozenset(), "a crashed turn left its agent wedged"

        sched.wake(7)  # the agent is still serviceable
        await _settle()
        assert calls == [7, 7]

    async def test_aclose_cancels_running_turns(self) -> None:
        rec = _Recorder()
        rec.gate(7)
        sched = TurnScheduler(rec)
        sched.wake(7)
        await _settle()
        assert sched.active_agents == frozenset({7})

        await sched.aclose()

        assert sched.active_agents == frozenset()
        # Closed: further wakes are ignored rather than resurrecting a task
        # during shutdown.
        sched.wake(7)
        await _settle()
        assert sched.active_agents == frozenset()


class TestChannelParsing:
    @pytest.mark.parametrize(
        ("channel", "expected"),
        [
            ("ava:inbound:42", 42),
            ("ava:inbound:1", 1),
            ("someprefix:inbound:9999", 9999),
        ],
    )
    def test_valid_channels(self, channel: str, expected: int) -> None:
        assert agent_id_from_channel(channel) == expected

    @pytest.mark.parametrize(
        "channel",
        ["ava:inbound:", "ava:inbound:abc", "nocolons", "ava:inbound:12x"],
    )
    def test_malformed_channel_is_none_not_a_raise(self, channel: str) -> None:
        """One bad frame must not kill the subscription every other agent shares."""
        assert agent_id_from_channel(channel) is None


class TestPatternMatchesTheRealChannel:
    def test_pattern_covers_what_inbound_channel_publishes(self) -> None:
        """The dispatcher's PSUBSCRIBE pattern and the publisher's channel name
        are derived from the same prefix, and this asserts they actually meet:
        a drift here is silent (no error, just a runner that never wakes), which
        is exactly the failure `inbound_channel` was centralised to prevent."""
        from fnmatch import fnmatchcase

        from services.agent_host.dispatcher import _INBOUND_PATTERN_SUFFIX
        from shared.cluster import inbound_channel, redis_channel_prefix

        pattern = f"{redis_channel_prefix()}{_INBOUND_PATTERN_SUFFIX}"
        for agent_id in (1, 42, 999999):
            channel = inbound_channel(agent_id)
            assert fnmatchcase(channel, pattern), f"{channel!r} not covered by {pattern!r}"
            assert agent_id_from_channel(channel) == agent_id


class TestDispatcherMessageHandling:
    def _dispatcher(self) -> tuple[InboundWakeDispatcher, list[int]]:
        woken: list[int] = []

        class _Sched:
            def wake(self, agent_id: int) -> None:
                woken.append(agent_id)

        return InboundWakeDispatcher("redis://unused", _Sched()), woken  # pyright: ignore[reportArgumentType]

    def test_pmessage_wakes_its_agent(self) -> None:
        disp, woken = self._dispatcher()
        disp._handle({"type": "pmessage", "channel": "ava:inbound:42", "data": "x"})  # pyright: ignore[reportPrivateUsage]
        assert woken == [42]

    def test_non_pmessage_frames_are_ignored(self) -> None:
        """psubscribe confirmations and plain messages share the stream."""
        disp, woken = self._dispatcher()
        disp._handle({"type": "psubscribe", "channel": "ava:inbound:*", "data": 1})  # pyright: ignore[reportPrivateUsage]
        assert woken == []

    def test_unparseable_channel_is_dropped_quietly(self) -> None:
        disp, woken = self._dispatcher()
        disp._handle({"type": "pmessage", "channel": "ava:inbound:oops", "data": "x"})  # pyright: ignore[reportPrivateUsage]
        assert woken == []
