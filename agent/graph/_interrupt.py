"""Interrupt-in-node RAII.

When entering an interruptible section (the LLM stream or code execution), the
node uses `async with subscribe_interrupt(...) as event:` to get an
asyncio.Event; the context manager spawns a background task that watches for a
durable interrupt inbound (kind 'cancel' or 'terminate') for this agent and
sets the event the moment one is queued. The node races its work vs
`event.wait()` and aborts when the event fires.

Durability is the whole point. The signal is a real `inbound_messages` row
(INSERTed by /api/cancel for a pause, or the terminate path), detected via a
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
2476, live incident: 30.06s pickup). A polling watcher owns no shared
resource, so even a cancellation-lost survivor is inert: it holds nothing and
exits at its next loop check because `stop` is set.

Cancel latency trades from "instant (pub/sub)" to "≤ one poll interval" — 2s
is well inside what a pause/terminate UI action tolerates, and the claim
path's instant wake is untouched.

Container/eval mode has no inbound queue (pool None); subscribe_interrupt then
yields an event that never fires, so the wrapped action runs uninterruptibly —
matching the pre-existing no-cancel behavior there.
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool

from agent.db import has_pending_interrupt
from agent.graph._node_log import awaiter_chain_lines
from shared.log import logger


async def _watch_for_interrupt(
    pool: AsyncConnectionPool,
    event: asyncio.Event,
    agent_id: int,
    stop: asyncio.Event,
) -> None:
    """Set `event` as soon as a pending cancel/terminate row exists for agent_id.

    Polls `has_pending_interrupt` on `_INTERRUPT_POLL_S` cadence. Deliberately
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
    stop_task = asyncio.create_task(stop.wait())
    try:
        while not stop.is_set() and not await has_pending_interrupt(pool, agent_id):
            done, _ = await asyncio.wait({stop_task}, timeout=_INTERRUPT_POLL_S)
            if done:
                break
    finally:
        stop_task.cancel()
    if not stop.is_set():
        event.set()


# How long exit waits for the cancelled watcher to unwind. Cancellation lands
# instantly on a healthy await, but a watcher blocked on a wedged DB call
# (a frozen Postgres host, a network blip) can sit for up to the kernel's TCP
# retry budget (~2 min) — an unbounded `await watcher` then freezes the whole
# turn for that long after the model already finished. The watcher does
# nothing after cancel besides unwinding (the stop flag is already set), so
# abandoning it is safe: it holds no shared resource anymore.
_WATCHER_EXIT_TIMEOUT_S = 5.0

# Poll cadence for the interrupt watcher (see `_watch_for_interrupt`). 2s
# bounds cancel/terminate abort latency for an in-flight llm/exec; the old
# shared pub/sub listener woke instantly but coupled the watcher to the claim
# node's idle wait — the coupling was the bug.
_INTERRUPT_POLL_S = 2.0


@asynccontextmanager
async def subscribe_interrupt(
    pool: AsyncConnectionPool | None,
    agent_id: int,
) -> AsyncGenerator[asyncio.Event]:
    """RAII watch for a durable interrupt (cancel/terminate) on this agent.

    Yields an `asyncio.Event` the node races its work against. On body exit
    (completion / exception / cancel) the watcher task is cancelled; if it does
    not unwind within `_WATCHER_EXIT_TIMEOUT_S` (wedged on a dead DB call) it
    is abandoned with a warning instead of stalling the turn.

    `pool` None (container/eval, no inbound queue) -> yields an event that
    never fires; the wrapped action runs uninterruptibly.
    """
    event = asyncio.Event()
    if pool is None:
        yield event
        return
    stop = asyncio.Event()
    watcher = asyncio.create_task(
        _watch_for_interrupt(pool, event, agent_id, stop),
        name=f"interrupt-watcher-{agent_id}",
    )
    try:
        yield event
    finally:
        stop.set()
        watcher.cancel()
        # asyncio.wait (not wait_for): wait_for would await the cancellation's
        # completion, which is exactly the wedge being bounded here.
        done, _ = await asyncio.wait({watcher}, timeout=_WATCHER_EXIT_TIMEOUT_S)
        if not done:
            # Name the exact line the watcher is wedged on — the live incident
            # burned hours of forensics because the abandon warning could not
            # say WHERE the orphan sat (psycopg wait? lock acquire? pool
            # checkout?). The await chain is a pure frame walk, no IO.
            chain = "\n".join(awaiter_chain_lines(watcher)) or "  <no frames captured>"
            logger.warning(
                "subscribe_interrupt watcher did not unwind {timeout}s after cancel "
                "(agent_id={agent_id}) — abandoning it; wedged await chain:\n{chain}",
                timeout=_WATCHER_EXIT_TIMEOUT_S,
                agent_id=agent_id,
                chain=chain,
            )
            # Record the orphan's eventual fate: whether (and when, and how)
            # it actually finished is the difference between "wedged on IO"
            # and "survived its cancellation and kept running" — unknowable
            # in the live incident, one callback here.
            abandoned_at = asyncio.get_running_loop().time()

            def _log_orphan_fate(t: asyncio.Task, _abandoned_at: float = abandoned_at) -> None:
                if t.cancelled():
                    fate = "cancelled"
                elif t.exception() is not None:
                    fate = f"raised {t.exception()!r}"
                else:
                    fate = "returned normally (survived its cancellation)"
                logger.info(
                    "abandoned interrupt watcher finished {dt:.1f}s after abandonment "
                    "(agent_id={agent_id}): {fate}",
                    dt=asyncio.get_running_loop().time() - _abandoned_at,
                    agent_id=agent_id,
                    fate=fate,
                )

            watcher.add_done_callback(_log_orphan_fate)  # pyright: ignore[reportUnknownArgumentType]
        elif not watcher.cancelled() and watcher.exception() is not None:
            logger.opt(exception=watcher.exception()).warning(
                "subscribe_interrupt watcher exited with exception (agent_id={})",
                agent_id,
            )
