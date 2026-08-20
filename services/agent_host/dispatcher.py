"""Inbound wake -> turn task: the hosted runner's dispatcher.

Phase 1 work item (a) of `future/infra/agent-runner-as-server.md` — the
pull->push inversion. Today the push already exists at the transport level
(Redis pub/sub) but terminates in a **per-process idle wait**: every idle agent
process holds its own subscription and blocks in the claim node. Hosted mode
replaces N per-agent subscriptions with **one pattern subscription** in the
runner, and an idle agent becomes no task at all.

Two objects, split because only one of them needs Redis:

- `TurnScheduler` — the whole correctness story, in pure asyncio: which agents
  have a turn task, which have a wake nobody has consumed yet, and the rule
  that one agent's turns never overlap while different agents run freely.
- `InboundWakeDispatcher` — one `PSUBSCRIBE` over `<prefix>:inbound:*`, decoding
  the agent id off each channel name and handing it to the scheduler.

## The race this exists to kill

A wake is a hint that `agents_inbound` has a row; the claim node is what
actually takes it. So the dangerous interleaving is not "two turns at once", it
is **a wake that lands exactly as a turn is winding down**:

    turn task: claim finds nothing -> returns  ...........  task object ends
    dispatcher:                        ^ wake for this agent arrives here

If the dispatcher decides "a task is already running, it will pick this up" and
the task has already made its last claim, the inbound sits unclaimed until
something else notices — the 30s SELECT recheck at best, forever at worst.

The fix is a **wake-pending flag per agent**, set on every wake without looking
at whether a task is running. A task re-checks the flag after each turn and
loops instead of exiting when it is set. The check and the task's removal from
the registry happen in the same synchronous stretch (no `await` between them),
and asyncio is single-threaded, so no wake can slip through the gap: either the
dispatcher sets the flag before the check and the running task loops, or it sets
it after the removal and starts a fresh task. There is no third ordering, which
is why this needs no lock.

The flag is deliberately a set membership and not a counter: two wakes that
arrive during one turn are one reason to run again, not two. The turn drains
whatever is pending, so collapsing them is correct and keeps a burst of chats
from queueing a burst of turns.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable

from shared.log import logger

# The pattern one subscription covers: every agent's inbound channel. Kept
# derived from `inbound_channel` (via the shared prefix) so the publish side
# and this pattern cannot drift — the same reason `inbound_channel` exists.
_INBOUND_PATTERN_SUFFIX = ":inbound:*"

# How long a cancelled turn gets to unwind before the host stops waiting and
# reports it as uncancellable.
#
# `asyncio.Task.cancel()` only lands at the task's next await point. That covers
# the overwhelming majority of a turn — waiting on the model, on I/O, on a lock —
# but NOT a task blocked inside a C call, which cannot be interrupted at all.
# Waiting forever on such a task is the failure this bound exists to convert into
# a visible one: without it, `aclose` hangs, the supervisor eventually SIGKILLs
# the host, and nothing anywhere says why.
#
# The value is bounded from above by the stop path's own window:
# `cli/commands/stop.py:_reap_agent_sessions` sends SIGTERM and waits
# `timeout_s` (15s) before force-killing. This wait plus the rest of shutdown —
# emitting the report, closing the pool, the healthz server, the pidfile — has to
# fit inside that, or the host is killed before it can say what was stuck. 5s
# leaves the remaining ~10s for the rest, and
# `tests/services/test_turn_dispatcher.py` pins the ordering against the stop
# path's actual default rather than against this comment.
_CANCEL_UNWIND_TIMEOUT_S = 5.0


class TurnScheduler:
    """Per-agent turn-task registry with wake coalescing.

    `run_turn(agent_id)` is injected: it must run the agent until it has nothing
    left to claim (the host re-invokes the graph until claim reports idle) and
    return. The scheduler is what guarantees the shape *around* it — one task per
    agent at a time, and never a lost wake.

    Not thread-safe and does not need to be: every method runs on the host's
    event loop.
    """

    def __init__(self, run_turn: Callable[[int], Awaitable[None]]) -> None:
        self._run_turn = run_turn
        self._tasks: dict[int, asyncio.Task[None]] = {}
        # Agents with a wake no turn has consumed yet. Membership, not a count:
        # see the module docstring.
        self._pending: set[int] = set()
        self._closed = False

    @property
    def active_agents(self) -> frozenset[int]:
        """Agents with a turn task right now — the hosted answer to "who is
        running", replacing the per-process status row."""
        return frozenset(self._tasks)

    def wake(self, agent_id: int) -> None:
        """Record a wake for `agent_id` and make sure a turn will follow.

        Idempotent while a turn is running: the flag is set and the running
        task will loop. Safe to call from the dispatcher's read loop — it never
        awaits, so it cannot interleave with a task's exit check.
        """
        if self._closed:
            return
        self._pending.add(agent_id)
        if agent_id not in self._tasks:
            self._start(agent_id)

    def _start(self, agent_id: int) -> None:
        task = asyncio.create_task(self._pump(agent_id), name=f"turn-{agent_id}")
        self._tasks[agent_id] = task

    async def _pump(self, agent_id: int) -> None:
        """Run turns for one agent until no wake is outstanding.

        The loop body, not the caller, consumes the flag: it is cleared BEFORE
        the turn runs, so a wake arriving *during* the turn is preserved and
        causes another pass. Clearing it after would swallow exactly the wake
        the turn could not have seen.
        """
        try:
            while True:
                self._pending.discard(agent_id)
                try:
                    await self._run_turn(agent_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # One agent's failed turn must not take the host down or
                    # wedge that agent: log it, drop the task, and let the next
                    # wake start a fresh one. The turn's own state is
                    # checkpointed, so the retry resumes rather than restarts.
                    logger.exception(
                        "hosted turn crashed — dropping the task; the next wake retries",
                        event="host_turn_crashed",
                        agent_id=agent_id,
                    )
                    return
                # No await between this check and the `finally` below, so a
                # wake cannot land in the gap (see the module docstring).
                if agent_id not in self._pending:
                    return
        finally:
            self._tasks.pop(agent_id, None)

    async def aclose(self) -> None:
        """Cancel every turn task and wait, BOUNDED, for them to unwind.

        Turns are checkpointed, so cancelling one loses at most the in-flight
        step — the same recovery path a runner restart already exercises.

        The bound is the point. `cancel()` lands at the task's next await, so a
        turn blocked inside a C call never unwinds, and an unbounded `await task`
        would hang shutdown until the supervisor SIGKILLs the host — leaving no
        record of which agent was stuck. Waiting `_CANCEL_UNWIND_TIMEOUT_S` and
        then REPORTING the stragglers turns a silent hang into a named one.

        Returns after the report either way: a turn that will not unwind is not
        something this process can fix, and the host exiting is what the
        supervisor is waiting for.
        """
        self._closed = True
        tasks = dict(self._tasks)
        for task in tasks.values():
            task.cancel()
        if tasks:
            await self._await_unwind(tasks)
        self._tasks.clear()
        self._pending.clear()

    async def _await_unwind(self, tasks: dict[int, asyncio.Task[None]]) -> None:
        """Wait out the cancellations and name whatever is still running.

        `asyncio.wait` returns the stragglers rather than raising, so a task that
        refuses to unwind is data here instead of an exception — which is what
        lets the host report it and keep shutting down.
        """
        started = time.monotonic()
        _, pending = await asyncio.wait(tasks.values(), timeout=_CANCEL_UNWIND_TIMEOUT_S)
        # Retrieving the outcome of the tasks that DID finish keeps asyncio from
        # logging "exception was never retrieved" for a turn that raised on its
        # way out — noise that would sit next to the real report below.
        for task in tasks.values():
            if task not in pending and not task.cancelled():
                with contextlib.suppress(Exception):
                    task.exception()
        if not pending:
            return
        waited = time.monotonic() - started
        for agent_id, task in tasks.items():
            if task in pending:
                logger.error(
                    "hosted turn for agent {agent_id} did not unwind {waited:.1f}s after cancel "
                    "— it is blocked somewhere asyncio cannot interrupt (a C call); the host is "
                    "exiting anyway and this agent's turn resumes from its checkpoint on restart",
                    event="host_turn_uncancellable",
                    agent_id=agent_id,
                    waited_s=round(waited, 1),
                )


def agent_id_from_channel(channel: str) -> int | None:
    """The agent id in an `<prefix>:inbound:<id>` channel name, or None.

    None rather than a raise: the value comes off the wire, and one malformed
    channel name must not kill the subscription that serves every other agent.
    """
    _, sep, tail = channel.rpartition(":")
    if not sep or not tail.isdigit():
        return None
    return int(tail)


class InboundWakeDispatcher:
    """One `PSUBSCRIBE` over every local agent's inbound channel.

    Replaces the per-agent `RedisInboundListener` subscriptions of process mode:
    one connection covers the whole runner, so adding an agent costs no Redis
    resources at all.

    The wake payload is ignored — as in process mode, a wake means "look at the
    queue", and the claim CAS is what decides who gets the row. That keeps this
    loop free of any delivery semantics: a duplicate wake is harmless and a
    coalesced one is correct.
    """

    def __init__(self, redis_url: str, scheduler: TurnScheduler) -> None:
        self._redis_url = redis_url
        self._scheduler = scheduler

    async def run(self) -> None:
        """Subscribe and feed wakes to the scheduler until cancelled.

        Reconnects on failure with a short backoff: pub/sub is fire-and-forget,
        so a wake published while this is down is lost — which is exactly what
        the delivery watchdog's re-publish and the claim SELECT recheck already
        cover for process mode, and they cover it here unchanged.
        """
        from redis import asyncio as aredis

        from shared.cluster import redis_channel_prefix

        pattern = f"{redis_channel_prefix()}{_INBOUND_PATTERN_SUFFIX}"
        while True:
            redis = aredis.Redis.from_url(self._redis_url, decode_responses=True)  # pyright: ignore[reportUnknownMemberType]
            try:
                pubsub = redis.pubsub(ignore_subscribe_messages=True)  # pyright: ignore[reportUnknownMemberType]
                await pubsub.psubscribe(pattern)
                logger.info(
                    "hosted dispatcher subscribed to {pattern}",
                    event="host_dispatcher_subscribed",
                    pattern=pattern,
                )
                async for message in pubsub.listen():  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                    self._handle(message)  # pyright: ignore[reportUnknownArgumentType]
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "hosted dispatcher subscription dropped — reconnecting",
                    event="host_dispatcher_reconnect",
                )
                await asyncio.sleep(1.0)
            finally:
                with contextlib.suppress(Exception):
                    await redis.aclose()

    def _handle(self, message: dict[str, object]) -> None:
        """Turn one pub/sub message into a wake. Never raises: a bad frame must
        not break the subscription every other agent depends on."""
        if message.get("type") != "pmessage":
            return
        channel = message.get("channel")
        if not isinstance(channel, str):
            return
        agent_id = agent_id_from_channel(channel)
        if agent_id is None:
            logger.warning(
                "hosted dispatcher ignoring unparseable wake channel {channel!r}",
                event="host_dispatcher_bad_channel",
                channel=channel,
            )
            return
        self._scheduler.wake(agent_id)
