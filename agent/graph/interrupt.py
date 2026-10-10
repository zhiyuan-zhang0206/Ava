"""Interrupt-in-node RAII.

When entering an interruptible section (the LLM stream or code execution), the
node uses `async with subscribe_interrupt(...) as event:` to get an
asyncio.Event; the context manager spawns a background task that watches for a
durable interrupt inbound (kind 'cancel'/'terminate') for this agent and sets
the event the moment one is queued. The node races its work vs `event.wait()`
and aborts when the event fires.

Durability is the whole point. The signal is a real `inbound_messages` row
(retained native cancel inbounds or the terminate path), detected via a
short DB poll. A signal that lands while no node is interruptible is NOT lost:
it stays a pending row and the next claim pass dispatches it. This node only
*aborts* the current action; the semantic dispatch (cancel -> halt to idle,
terminate -> goto END) stays in the claim node, the single owner of inbound
semantics.

The watcher polls `inbound_messages` on a short cadence (`_INTERRUPT_POLL_S`)
instead of sharing the agent's Redis inbound listener with the claim node's
idle wait. Sharing was the root cause of a lost-wake class of incidents: the
watcher's cancellation could be swallowed in the cancel-vs-completion race,
leaving an orphan holding the listener's `_wait_lock` (and its connection) for
up to a full wait cycle while every pub/sub wake for fresh inbounds went
unheard — the agent then only woke via the claim loop's 30s SELECT recheck,
which is the user-visible "message stuck ~30s" symptom (2026-08-02, agent
2476, live incident: 30.06s pickup). A polling watcher borrows its explicit interrupt pool. Its original service
retains a cancellation-lost poll until it returns; stop prevents a later poll
result from setting the old event. Finite caller return does not close its pool.

Cancel latency trades from "instant (pub/sub)" to "≤ one poll interval" — 2s
is well inside what a pause/terminate UI action tolerates, and the claim
path's instant wake is untouched.

Container/eval mode has no inbound queue (pool None); subscribe_interrupt then
yields an event that never fires, so the wrapped action runs uninterruptibly —
matching the pre-existing no-cancel behavior there.
"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Coroutine
from contextlib import asynccontextmanager
from typing import Any

from psycopg_pool import AsyncConnectionPool

from agent.db import pending_interrupt_reason
from agent.graph.node_log import awaiter_chain_lines
from agent.ownership.native_cancel import observe_bound_cancel
from base.agents.incarnation.native_work_models import NativeCancelMarker, NativeWorkTarget
from base.agents.messages.inbound import InterruptReason
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedTurnResources


class InterruptEvent(asyncio.Event):
    """An abort event retaining the first observed cause through cleanup.

    Setting the event never claims or applies the durable command. Later
    commands cannot relabel an execution that was already interrupted.
    """

    def __init__(self) -> None:
        super().__init__()
        self.reason = InterruptReason.USER
        self.native_cancel: NativeCancelMarker | None = None

    def set(
        self,
        reason: InterruptReason = InterruptReason.USER,
        *,
        native_cancel: NativeCancelMarker | None = None,
    ) -> None:
        if not self.is_set():
            self.reason = reason
            self.native_cancel = native_cancel
            super().set()


class ModelInterruptedError(Exception):
    """A model operation settled without a result; claim still owns the command."""


async def interruptible_model[T](
    operation: Coroutine[Any, Any, T],
    event: asyncio.Event,
) -> T:
    """Race side-effect-free model work against a durable interruption.

    The same-tick race belongs to cancellation. Wait for the owned operation
    to settle before returning to claim; the host's external stop boundary
    owns escalation if a provider refuses to unwind.
    """

    async def observed_operation() -> tuple[T] | BaseException:
        # The bounded owner inspects the original error after both tasks settle.
        # Letting TaskGroup raise it would cancel its sibling and wrap its identity.
        try:
            return (await operation,)
        except BaseException as error:
            return error

    async with asyncio.TaskGroup() as tasks:
        task = tasks.create_task(observed_operation())
        interrupted = tasks.create_task(event.wait())
        try:
            done, _ = await asyncio.wait({task, interrupted}, return_when=asyncio.FIRST_COMPLETED)
            cancelled = interrupted in done
        finally:
            task.cancel()
            interrupted.cancel()
    outcome = task.result()
    if isinstance(outcome, BaseException) and (
        not cancelled or not isinstance(outcome, asyncio.CancelledError)
    ):
        raise outcome
    if cancelled:
        raise ModelInterruptedError
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome[0]


async def _watch_for_interrupt(
    pool: AsyncConnectionPool,
    event: InterruptEvent,
    agent_id: int,
    stop: asyncio.Event,
    *,
    incarnation: RuntimeIncarnation | None,
    work: NativeWorkTarget | None,
) -> None:
    """Set `event` as soon as a pending cancel/terminate row exists for agent_id.

    Polls `pending_interrupt_reason` on `_INTERRUPT_POLL_S` cadence. Deliberately
    does NOT touch the Redis inbound listener: that listener is owned by the
    claim node's idle wait, and a watcher sharing it can starve the claim wait
    of wakes (module docstring — the 2026-08-02 lost-wake incident). Between
    polls it waits on `stop` so exit is prompt.

    `stop` is the belt to cancellation's braces: a CancelledError can be lost
    in the cancel-vs-completion race (the task already queued to resume when
    cancel lands), and a watcher that survives its own cancellation would
    otherwise keep polling. Exit sets `stop` before cancelling; the survivor
    terminates at its next loop check, one poll at most — and because the
    watcher holds no shared resource, even that lingering poll is harmless.
    """
    while not stop.is_set():
        marker = await observe_bound_cancel(pool, agent_id, incarnation=incarnation, work=work)
        if marker is not None:
            if not stop.is_set():
                event.set(InterruptReason.USER, native_cancel=marker)
            return
        reason = await pending_interrupt_reason(pool, agent_id, incarnation=incarnation, work=work)
        if reason is not None:
            if not stop.is_set():
                event.set(reason)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_INTERRUPT_POLL_S)


# How long exit waits for the cancelled watcher to unwind. Cancellation lands
# instantly on a healthy await, but a watcher blocked on a wedged DB call
# (a frozen Postgres host, a network blip) can sit for up to the kernel's TCP
# retry budget (~2 min) — an unbounded `await watcher` then freezes the whole
# turn for that long after the model already finished. The watcher does
# nothing after cancel besides unwinding (the stop flag is already set).
# The original service still owns its actual task and eventual result.
_WATCHER_EXIT_TIMEOUT_S = 5.0

# Poll cadence for the interrupt watcher (see `_watch_for_interrupt`). 2s
# bounds cancel/terminate abort latency for an in-flight llm/exec; the old
# shared pub/sub listener woke instantly but coupled the watcher to the claim
# node's idle wait — the coupling was the bug.
_INTERRUPT_POLL_S = 2.0


class _InterruptWatch:
    """An invocation's watcher, retained by its original service after bounded exit."""

    def __init__(self, resources: HostedTurnResources, agent_id: int) -> None:
        self.resources = resources
        self.agent_id = agent_id
        self.event = InterruptEvent()
        self.stop = asyncio.Event()
        self.error: BaseException | None = None
        self.late = False
        self.retained_at = 0.0

    async def run(
        self,
        pool: AsyncConnectionPool,
        *,
        incarnation: RuntimeIncarnation | None,
        work: NativeWorkTarget | None,
    ) -> None:
        cancelled = False
        try:
            await _watch_for_interrupt(
                pool, self.event, self.agent_id, self.stop, incarnation=incarnation, work=work
            )
        except asyncio.CancelledError:
            cancelled = True
            raise
        except BaseException as error:
            self.error = error
            if self.late:
                self.resources.require_service().record_failure(
                    self.resources, error, name=f"interrupt-watcher-{self.agent_id}"
                )
            else:
                logger.opt(exception=error).warning(
                    "interrupt watcher failed during invocation (agent_id={})", self.agent_id
                )
                if not self.stop.is_set():
                    self.event.set()
        finally:
            if self.late:
                fate = (
                    f"raised {self.error!r}"
                    if self.error is not None
                    else "cancelled"
                    if cancelled
                    else "returned normally"
                )
                logger.info(
                    "retained interrupt watcher finished {elapsed:.1f}s after handoff "
                    "(agent_id={agent_id}): {fate}",
                    elapsed=asyncio.get_running_loop().time() - self.retained_at,
                    agent_id=self.agent_id,
                    fate=fate,
                )

    async def finish(self, task: asyncio.Task[None], primary: BaseException | None) -> None:
        self.stop.set()
        task.cancel()
        # wait_for waits for cancelled cleanup; wait preserves the five-second return.
        deadline = asyncio.get_running_loop().time() + _WATCHER_EXIT_TIMEOUT_S
        cancellation: asyncio.CancelledError | None = None
        while not task.done() and asyncio.get_running_loop().time() < deadline:
            try:
                await asyncio.wait(
                    {task}, timeout=max(0, deadline - asyncio.get_running_loop().time())
                )
            except asyncio.CancelledError as error:
                cancellation = error
        if not task.done():
            self.late = True
            self.retained_at = asyncio.get_running_loop().time()
            chain = "\n".join(awaiter_chain_lines(task)) or "  <no frames captured>"
            logger.info(
                "subscribe_interrupt watcher did not unwind {timeout}s after cancel "
                "(agent_id={agent_id}) — retained by its service; wedged await chain:\n{chain}",
                timeout=_WATCHER_EXIT_TIMEOUT_S,
                agent_id=self.agent_id,
                chain=chain,
            )
        elif self.error is not None:
            primary = primary if primary is not None else cancellation
            if primary is None:
                raise self.error
            primary.add_note(f"interrupt watcher cleanup also failed: {self.error!r}")
            secondary = [self.error]
            if primary.__cause__ is not None:
                secondary.insert(0, primary.__cause__)
            raise primary from BaseExceptionGroup("interrupt cleanup failures", secondary)
        if primary is None and cancellation is not None:
            raise cancellation


@asynccontextmanager
async def subscribe_interrupt(
    pool: AsyncConnectionPool | None,
    agent_id: int,
    *,
    incarnation: RuntimeIncarnation | None,
    work: NativeWorkTarget | None,
    resources: HostedTurnResources | None,
) -> AsyncGenerator[InterruptEvent]:
    """RAII watch for a durable interrupt (cancel/terminate) on this agent.

    Yields an `asyncio.Event` the node races its work against. On body exit
    (completion / exception / cancel) the watcher task is cancelled; if it does
    not unwind within `_WATCHER_EXIT_TIMEOUT_S`, its original service keeps
    the task through stop/join before pool close. Unknown in-flight failure
    reaches the caller; late failure is reported immediately and raised by
    that service at stop/join, without cancelling sibling agents.

    `pool` None (container/eval, no inbound queue) -> yields an event that
    never fires; the wrapped action runs uninterruptibly.
    """
    event = InterruptEvent()
    if pool is None:
        yield event
        return
    if resources is None:
        raise RuntimeError("a database interrupt watcher requires its explicit service resources")
    service = resources.require_service()
    owned = _InterruptWatch(resources, agent_id)
    watcher = service.watch(
        owned.run(pool, incarnation=incarnation, work=work),
        name=f"interrupt-watcher-{agent_id}",
    )
    primary: BaseException | None = None
    try:
        yield owned.event
    except BaseException as error:
        primary = error
        raise
    finally:
        await owned.finish(watcher, primary)
