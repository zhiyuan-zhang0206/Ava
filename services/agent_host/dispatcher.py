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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

from agent._turn_progress import turn_progress_age_s, turn_progress_snapshot
from services.agent_host.runtime import _active_turn_config_fingerprint
from shared.hosted_db_wait import database_wait_snapshot
from shared.log import logger
from shared.stop_timing import CANCEL_UNWIND_TIMEOUT_S, CLOCK_READ_TIMEOUT_S

# The pattern one subscription covers: every agent's inbound channel. Kept
# derived from `inbound_channel` (via the shared prefix) so the publish side
# and this pattern cannot drift — the same reason `inbound_channel` exists.
_INBOUND_PATTERN_SUFFIX = ":inbound:*"

# The subscription supplies the fast path. This deadline supplies the recovery
# boundary: a private-network connection can remain TCP-established while its
# peer no longer answers reads, so an unbounded pub/sub iterator would silently
# disable both delivery and the hosted pending scan forever. It intentionally
# is the durable scan backstop when a Redis wake is lost.
_DEFAULT_SUBSCRIPTION_READ_TIMEOUT_S = 30.0
# A per-call scheduler allowance, not a lifecycle grace with a cross-component
# ordering. It gives `asyncio.wait_for` room to cancel a Redis read after the
# client's own timeout before the connection is rebuilt.
_DEFAULT_SUBSCRIPTION_READ_SLACK_S = 1.0
_DEFAULT_RECONNECT_DELAY_S = 1.0
# The database scan is a durable backstop, not the subscription's liveness
# probe. Bound its outage retries independently so a long DB outage does not
# turn into an unbounded delay before the next reconciliation attempt.
_DEFAULT_MAX_SCAN_BACKOFF_S = 300.0

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
# These bounds limit diagnostics after cancellation, not graceful maintenance.
# Normal pause/stop waits for durable drain and fails on timeout without an
# implicit force-kill. Explicit force can interrupt the host and its report.
#
# Ceiling on reading the stuck agents' activity clocks, on the shutdown path.
# Small on purpose: this is a diagnostic enrichment of a report already worth
# emitting without it, and the DB may be exactly what a wedged turn is stuck on.
# Spent once for all stragglers (one concurrent gather), not per agent, so it
# adds a flat 2s to the shutdown budget the cancel wait above already bounds —
# `test_the_unwind_bound_fits_inside_the_stop_paths_kill_window` asserts the SUM
# of the two against the stop path's force-kill window.


def _age_seconds(moment: datetime) -> float:
    """Seconds since `moment`, tolerating a naive timestamp.

    psycopg returns `TIMESTAMPTZ` as aware, but this runs on a shutdown path
    where a wrong assumption would raise inside the very report that exists to
    explain what went wrong. A naive value is read as UTC, which is what the
    column stores."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (datetime.now(UTC) - moment).total_seconds()


def _database_waiting(agent_id: int) -> bool:
    progress = turn_progress_snapshot(agent_id)
    last = progress["last_marks"][-1] if progress is not None else None
    return database_wait_snapshot(agent_id, last_progress=last) is not None


class TurnScheduler:
    """Per-agent turn-task registry with wake coalescing.

    `run_turn(agent_id)` is injected: it must run the agent until it has nothing
    left to claim (the host re-invokes the graph until claim reports idle) and
    return. The scheduler is what guarantees the shape *around* it — one task per
    agent at a time, and never a lost wake.

    Not thread-safe and does not need to be: every method runs on the host's
    event loop.
    """

    def __init__(
        self,
        run_turn: Callable[[int], Awaitable[None]],
        *,
        activity_clock: Callable[[int], Awaitable[datetime | None]] | None = None,
    ) -> None:
        self._run_turn = run_turn
        # Optional, and INJECTED rather than read here, for the same reason
        # `run_turn` is: this class is the correctness story in pure asyncio and
        # owns no database handle. The host supplies a reader; without one the
        # uncancellable-turn report simply omits the clock (see `_await_unwind`).
        self._activity_clock = activity_clock
        self._tasks: dict[int, asyncio.Task[None]] = {}
        # Agents with a wake no turn has consumed yet. Membership, not a count:
        # see the module docstring.
        self._pending: set[int] = set()
        self._closed = False
        # Set by a turn task that refuses its bounded unwind: the dispatcher
        # loop checks it each iteration and raises HostRestartRequiredError so
        # the daemon exits and the supervisor restarts from the checkpoint.
        self._restart_required = False

    @property
    def active_agents(self) -> frozenset[int]:
        """Agents with a turn task right now — the in-process answer to "who is
        running"; the host also mirrors that state in the database row."""
        return frozenset(self._tasks)

    @property
    def restart_required(self) -> bool:
        """True when a turn refused its bounded cancellation and this daemon
        must exit. The dispatcher loop picks it up at the next iteration."""
        return self._restart_required

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
        """One Task owns one actual turn; a queued successor gets a new Task.

        Reusing a Task across incarnations would let delayed cancellation of
        the captured old Task interrupt its successor. Wake handoff and registry
        replacement have no intervening await, preserving single-flight.
        """
        completed = False
        try:
            self._pending.discard(agent_id)
            await self._run_turn(agent_id)
            completed = True
        except asyncio.CancelledError:
            raise
        except HostRestartRequiredError:
            # A turn refused its bounded cancellation and still owns the task
            # slot; rescheduling beside it would violate one-turn-per-agent.
            # Escalate to the dispatcher loop, which exits the daemon; the
            # supervisor restarts it from the durable checkpoint (the same
            # recovery the pending scan's stale-turn branch uses).
            logger.error(
                "hosted turn for agent {agent_id} refused its bounded "
                "cancellation after a stall — requesting host restart",
                event="host_turn_stall_uncancellable",
                agent_id=agent_id,
            )
            self._restart_required = True
        except TurnStallTimeoutError:
            # The no-progress stall guard aborted the invocation cleanly (it
            # DID unwind): the task ends, the runtime was dropped by run_turn,
            # and the next wake resumes from the checkpoint after re-running
            # the startup reconcile.
            logger.error(
                "hosted turn for agent {agent_id} aborted after a "
                "no-progress stall — dropping the task; the next wake "
                "resumes from the checkpoint",
                event="host_turn_stall_aborted",
                agent_id=agent_id,
            )
        except Exception as exc:
            # One agent's failed turn must not take the host down or wedge
            # that agent: log it, drop the task, and let the next wake start a
            # fresh one. The turn's own state is checkpointed, so the retry
            # resumes rather than restarts.
            fingerprint = _active_turn_config_fingerprint.get()
            logger.exception(
                "hosted turn crashed — dropping the task; the next wake retries",
                event="host_turn_crashed",
                agent_id=agent_id,
                exception_type=type(exc).__name__,
                **({"config_fingerprint": fingerprint} if fingerprint is not None else {}),
            )
        finally:
            if self._tasks.get(agent_id) is asyncio.current_task():
                self._tasks.pop(agent_id)
                if completed and not self._closed and agent_id in self._pending:
                    self._start(agent_id)

    async def cancel_exact_force(
        self, agent_id: int, command_id: int, validate: Callable[[int, int], Awaitable[bool]]
    ) -> bool:
        """Capture a Task before validating force; a later Task is never cancelled."""
        task = self._tasks.get(agent_id)
        if not await validate(agent_id, command_id):
            return False
        if task is None:
            # Only the original host's normal serialized pump can prove idle.
            self.wake(agent_id)
            return False
        if self._tasks.get(agent_id) is not task:
            return False
        task.cancel()
        await self._await_unwind({agent_id: task})
        return task.done()

    async def cancel_agent(self, agent_id: int) -> bool:
        """Cancel ONE agent's turn task, with the same bounded unwind as `aclose`.

        The hosted replacement for force-terminating a wedged agent's process:
        `Task.cancel()` lands at the task's next await point, the same
        `CANCEL_UNWIND_TIMEOUT_S` bound applies, and a task that refuses to
        unwind produces the same `host_turn_uncancellable` report — while
        staying in the registry, so a later wake for that agent does not
        double-schedule (the turn it is stuck in is still the turn it owns).

        Returns True only after the captured task ended; stragglers stay false.
        External force uses ``cancel_exact_force`` instead of this watchdog path.
        """
        task = self._tasks.get(agent_id)
        if task is None:
            return False
        task.cancel()
        await self._await_unwind({agent_id: task})
        return task.done()

    async def aclose(self) -> None:
        """Cancel every turn task and wait, BOUNDED, for them to unwind.

        Turns are checkpointed, so cancelling one loses at most the in-flight
        step — the same recovery path a runner restart already exercises.

        The bound is the point. `cancel()` lands at the task's next await, so a
        turn blocked inside a C call never unwinds, and an unbounded `await task`
        would hang shutdown until the supervisor SIGKILLs the host — leaving no
        record of which agent was stuck. Waiting `CANCEL_UNWIND_TIMEOUT_S` and
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
        _, pending = await asyncio.wait(tasks.values(), timeout=CANCEL_UNWIND_TIMEOUT_S)
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
        stuck = [agent_id for agent_id, task in tasks.items() if task in pending]
        clocks = await self._activity_clocks(stuck)
        for agent_id in stuck:
            last_active = clocks.get(agent_id)
            idle_s = None if last_active is None else round(_age_seconds(last_active), 1)
            logger.error(
                "hosted turn for agent {agent_id} did not unwind {waited_s:.1f}s after cancel "
                "— it is blocked somewhere asyncio cannot interrupt (a C call); the host is "
                "exiting anyway and this agent's turn resumes from its checkpoint on restart "
                "(last completed LLM step: {idle_s}s ago)",
                event="host_turn_uncancellable",
                agent_id=agent_id,
                waited_s=round(waited, 1),
                # The agent's REAL activity clock (agents_meta.last_active_at),
                # not the `/api/agents` field of the same name — that one is
                # MAX(inbound_messages.created_at) and goes stale during exactly
                # the long turns where "is it wedged?" is a real question
                # (issue #183). Reported so a reader can tell a slow shutdown
                # (cancel pending 5s, last step 2s ago) from a genuine wedge
                # (cancel pending 5s, last step 20 minutes ago) — see
                # `_activity_clocks` for what the signal does and does not mean.
                last_active_at=None if last_active is None else last_active.isoformat(),
                idle_s=idle_s,
            )

    async def _activity_clocks(self, agent_ids: list[int]) -> dict[int, datetime]:
        """Each stuck agent's real activity clock — best-effort, bounded, never raises.

        `agents_meta.last_active_at` is written on every COMPLETED LLM step
        (`agent/graph/_llm.py:_persist_last_active`), so a stale value means "no
        LLM step has completed since then". That is the best wedge discriminator
        available and it is **not an oracle**: a turn sitting in one long exec,
        or one long model stream, also reads stale without being wedged. It
        separates a slow shutdown from a turn that has been silent for minutes;
        it does not by itself prove a wedge.

        Deliberately reported rather than acted on. Everything here runs while
        the host is already shutting down, so a read that fails or hangs must
        cost the REPORT nothing — an omitted clock still leaves the agent id and
        the pending duration, which is the part that cannot be reconstructed
        later.
        """
        if self._activity_clock is None or not agent_ids:
            return {}

        async def _one(agent_id: int) -> tuple[int, datetime | None]:
            try:
                return agent_id, await self._activity_clock(agent_id)  # pyright: ignore[reportOptionalCall]
            except Exception:
                return agent_id, None

        try:
            pairs = await asyncio.wait_for(
                asyncio.gather(*(_one(a) for a in agent_ids)), CLOCK_READ_TIMEOUT_S
            )
        except (TimeoutError, Exception):
            logger.warning(
                "could not read the activity clock for {n} stuck agent(s) — reporting without it",
                n=len(agent_ids),
            )
            return {}
        return {agent_id: value for agent_id, value in pairs if value is not None}


def agent_id_from_channel(channel: str) -> int | None:
    """The agent id in an `<prefix>:inbound:<id>` channel name, or None.

    None rather than a raise: the value comes off the wire, and one malformed
    channel name must not kill the subscription that serves every other agent.
    """
    _, sep, tail = channel.rpartition(":")
    if not sep or not tail.isdigit():
        return None
    return int(tail)


@dataclass(frozen=True)
class PendingInboundWake:
    """One local hosted agent with a pending inbound row.

    ``stale`` means BOTH that one pending inbound has passed the running
    turn grace and that the agent has made no completed-turn progress for
    that grace (an active turn may legitimately run up to the exec +
    LLM-retry budget). A fresh pending row still receives the scan's normal
    wake, but does not interrupt a legitimate turn that happens to be running.
    """

    agent_id: int
    stale: bool


class _WakeScheduler(Protocol):
    """The dispatcher-facing subset of `TurnScheduler`."""

    @property
    def active_agents(self) -> frozenset[int]: ...

    @property
    def restart_required(self) -> bool: ...

    def wake(self, agent_id: int) -> None: ...

    async def cancel_agent(self, agent_id: int) -> bool: ...


class HostRestartRequiredError(RuntimeError):
    """A hosted turn refused its bounded cancellation and owns the task slot.

    Rescheduling beside it would violate one-turn-per-agent. Letting the daemon
    exit hands recovery to its supervisor, which restarts from the durable
    checkpoint instead.
    """


class TurnStallTimeoutError(RuntimeError):
    """A hosted turn was aborted by the no-progress stall guard (host.py).

    Its ``graph.ainvoke`` made no progress for
    ``AVA_HOST_TURN_NO_PROGRESS_TIMEOUT_SECONDS`` and was cancelled; the turn
    task ends (the task DID unwind — that is the whole point of the bounded
    abort) and the next wake resumes from the checkpoint. Distinct from
    ``HostRestartRequiredError``, which is raised when the cancel refuses to
    unwind: the daemon there must exit, here it must not.
    """


@dataclass(frozen=True)
class _ScanSchedule:
    """The next durable-scan deadline and retry delay after one scan attempt."""

    next_scan_at: float
    scan_backoff_s: float


class InboundWakeDispatcher:
    """One `PSUBSCRIBE` over every local agent's inbound channel.

    One subscription covers the whole runner, so adding an agent costs no Redis
    resources at all.

    The wake payload is ignored — a wake means "look at the queue", and the claim CAS is what decides who gets the row. That keeps this
    loop free of any delivery semantics: a duplicate wake is harmless and a
    coalesced one is correct.

    Subscription failures rebuild Redis. Database scan failures leave that
    subscription in place and retry the durable backstop with bounded backoff.
    """

    def __init__(
        self,
        redis_url: str,
        scheduler: _WakeScheduler,
        *,
        pending_scan: Callable[[float], Awaitable[list[PendingInboundWake]]] | None = None,
        stale_after_s: float | None = None,
        scan_interval_s: float = _DEFAULT_SUBSCRIPTION_READ_TIMEOUT_S,
        max_scan_backoff_s: float = _DEFAULT_MAX_SCAN_BACKOFF_S,
        subscription_read_timeout_s: float = _DEFAULT_SUBSCRIPTION_READ_TIMEOUT_S,
        subscription_read_deadline_grace_s: float = _DEFAULT_SUBSCRIPTION_READ_SLACK_S,
        reconnect_delay_s: float = _DEFAULT_RECONNECT_DELAY_S,
    ) -> None:
        self._redis_url = redis_url
        self._scheduler = scheduler
        self._pending_scan = pending_scan
        self._stale_after_s = stale_after_s
        self._scan_interval_s = scan_interval_s
        self._max_scan_backoff_s = max_scan_backoff_s
        self._subscription_read_timeout_s = subscription_read_timeout_s
        self._subscription_read_deadline_grace_s = subscription_read_deadline_grace_s
        self._reconnect_delay_s = reconnect_delay_s

    def _raise_if_restart_required(self) -> None:
        """Escalate a turn task's refused-bounded-unwind to a daemon exit.

        A plain raise inline trips TRY301 inside the subscription loop's
        broad except; the escalation is the same one `scan_once` raises
        directly for a stale pending turn that will not unwind.
        """
        if self._scheduler.restart_required:
            raise HostRestartRequiredError(
                "a hosted turn refused its bounded cancellation after "
                "a stall; exiting for supervisor recovery"
            )

    async def run(self) -> None:
        """Subscribe and feed wakes to the scheduler until cancelled.

        Subscription failures reconnect with a short backoff. The periodic
        database scan catches a wake published while the subscription is down;
        its own failures leave a healthy subscription alone and retry with
        bounded backoff. This shared scan supplies durable recovery for all agents.
        """
        from shared.cluster import redis_channel_prefix
        from shared.redis_client import open_async_redis, retry_auth_failures_async

        pattern = f"{redis_channel_prefix()}{_INBOUND_PATTERN_SUFFIX}"
        while True:
            redis = open_async_redis(self._redis_url)
            pubsub = None
            try:
                pubsub = redis.pubsub(ignore_subscribe_messages=True)  # pyright: ignore[reportUnknownMemberType]
                await retry_auth_failures_async(
                    lambda pubsub=pubsub: pubsub.psubscribe(pattern),
                    attempt_timeout_s=self._subscription_read_timeout_s,
                )
                logger.info(
                    "hosted dispatcher subscribed to {pattern}",
                    event="host_dispatcher_subscribed",
                    pattern=pattern,
                )
                next_scan_at = 0.0
                scan_backoff_s = self._scan_interval_s
                while True:
                    self._raise_if_restart_required()
                    now = time.monotonic()
                    if now >= next_scan_at:
                        scan_schedule = await self._next_scan_schedule(scan_backoff_s)
                        next_scan_at = scan_schedule.next_scan_at
                        scan_backoff_s = scan_schedule.scan_backoff_s
                    until_scan_s = max(0.001, next_scan_at - time.monotonic())
                    read_timeout_s = min(self._subscription_read_timeout_s, until_scan_s)
                    message = await asyncio.wait_for(
                        cast(
                            "Awaitable[dict[str, object] | None]",
                            pubsub.get_message(timeout=read_timeout_s),  # pyright: ignore[reportUnknownMemberType]
                        ),
                        timeout=read_timeout_s + self._subscription_read_deadline_grace_s,
                    )
                    if message is not None:
                        self._handle(message)  # pyright: ignore[reportUnknownArgumentType]
            except asyncio.CancelledError:
                raise
            except HostRestartRequiredError:
                logger.error(
                    "hosted stale turn would not cancel; exiting for supervisor recovery",
                    event="host_dispatcher_restart_required",
                )
                raise
            except Exception:
                logger.exception(
                    "hosted dispatcher subscription dropped — reconnecting",
                    event="host_dispatcher_reconnect",
                )
                await asyncio.sleep(self._reconnect_delay_s)
            finally:
                if pubsub is not None:
                    with contextlib.suppress(Exception):
                        await pubsub.aclose()  # pyright: ignore[reportUnknownMemberType]
                with contextlib.suppress(Exception):
                    await redis.aclose()

    async def _next_scan_schedule(self, scan_backoff_s: float) -> _ScanSchedule:
        """Run the durable backstop without treating its DB failure as Redis failure."""
        try:
            await self.scan_once()
        except asyncio.CancelledError:
            raise
        except HostRestartRequiredError:
            raise
        except Exception:
            backoff_s = min(scan_backoff_s, self._max_scan_backoff_s)
            logger.warning(
                "hosted dispatcher pending scan failed — keeping the subscription "
                "and retrying in {backoff_s:.3f}s",
                event="host_dispatcher_scan_failed",
                backoff_s=backoff_s,
            )
            return _ScanSchedule(
                next_scan_at=time.monotonic() + backoff_s,
                scan_backoff_s=min(backoff_s * 2, self._max_scan_backoff_s),
            )
        return _ScanSchedule(
            next_scan_at=time.monotonic() + self._scan_interval_s,
            scan_backoff_s=self._scan_interval_s,
        )

    async def scan_once(self) -> None:
        """Schedule every locally pending agent; cancel stale active turns first.

        This is public only as the narrow test seam for the durable backstop.
        Production calls it before each deadline-bounded subscription read.
        """
        if self._pending_scan is None:
            return
        if self._stale_after_s is None:
            raise RuntimeError("hosted pending scan configured without stale_after_s")

        started: set[int] = set()
        for candidate in await self._pending_scan(self._stale_after_s):
            if (
                candidate.stale
                and candidate.agent_id in self._scheduler.active_agents
                and not _database_waiting(candidate.agent_id)
            ):
                await self._scheduler.cancel_agent(candidate.agent_id)
                if candidate.agent_id in self._scheduler.active_agents:
                    raise HostRestartRequiredError(
                        f"hosted turn for agent {candidate.agent_id} did not unwind"
                    )
            if candidate.agent_id not in self._scheduler.active_agents:
                started.add(candidate.agent_id)
            self._scheduler.wake(candidate.agent_id)

        # Turn-level fake-alive: an in-flight agent whose turn-progress clock
        # is stale is not making progress even though NO pending inbound has
        # aged — the exact shape the pending-row checks above cannot see.
        # The 2026-09-04 incident: agent 2998 claimed its whole inbound queue
        # (the chats are committed into the checkpoint, none stay 'pending'),
        # then hung inside graph.ainvoke for 3.5 hours; every detector keyed on
        # a pending row or a pid stayed blind, and the row kept its heartbeat
        # lease. An in-flight agent is the answer to "is anything running for
        # this agent" — its stale clock is the answer to "has that turn done
        # ANYTHING since" (see agent/_turn_progress.py for what counts).
        for agent_id in self._scheduler.active_agents:
            # A task just started by this scan has not entered its pump yet.
            # Neither an idle agent's old clock nor its predecessor's clock
            # justifies cancelling that new task before it can run.
            if agent_id in started or _database_waiting(agent_id):
                continue
            age = turn_progress_age_s(agent_id)
            if age is None or age < self._stale_after_s:
                continue
            await self._scheduler.cancel_agent(agent_id)
            if agent_id in self._scheduler.active_agents:
                raise HostRestartRequiredError(f"hosted turn for agent {agent_id} did not unwind")
            logger.warning(
                "hosted turn for agent {agent_id} showed no progress for "
                "{no_progress_s:.0f}s (turn-level fake-alive) — turn task "
                "cancelled and the agent rescheduled; its next wake rebuilds "
                "the runtime, which re-runs the startup reconcile",
                event="host_turn_stall_detected",
                agent_id=agent_id,
                no_progress_s=age,
            )
            self._scheduler.wake(agent_id)

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
