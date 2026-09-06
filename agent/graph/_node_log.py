"""node_enter / node_exit lifecycle events — observability for graph node death +
driving frontend timeline sync.

Two responsibilities merged in the same wrapper:

1. **Observability**: wrap each node entry and exit with lifecycle records:
   - `node_enter`: payload contains node name + current msg_count
   - normal/cancelled `node_exit` records accumulate into one per-turn payload
   - exception `node_exit` records emit immediately with their traceback

   All outcomes log at INFO (user ruling 2026-08-04: node transitions are
   normal lifecycle flow, never WARNING — fatal turns surface through
   `agent/_runloop.py`'s ERROR crash line + Error events instead). The `exception:X` path
   uses `logger.opt(exception=True).info` in the except block to emit while the
   active exception is still in sys.exc_info() → events.payload automatically
   carries traceback / exception_type / exception_value (`shared.log._postgres_sink`
   PR #60 chain); the traceback keeps the exception queryable at INFO.

2. **Frontend timeline sync**: on **enter**, render a TimelineSnapshot from the
   in-memory `state.messages` and publish it → the gateway forwards it to the
   frontend → the frontend merges by item_id, keeping the single future
   partial (`partial.msg_idx == msg_count`). Snapshots are INCREMENTAL: only
   the messages committed since the last published snapshot (a per-process
   cursor), so render cost is O(new commits) instead of O(history) and the
   per-enter anchors DB query disappears. Full-window snapshots are published
   on first enter after process start, after a compaction shrink, and as the
   claim node's turn-end fallback. No system-prompt special-casing: the
   full-window paths render 0.0 when the tail window contains it (the
   spawn case is guaranteed — history is short), incremental snapshots
   never contain it (message 0 is below the cursor), and the frontend's
   id-replace merge keeps a single copy either way.

   **Why render from in-memory state**: LangGraph submits the checkpoint write
   into a background executor and proceeds to the next node without awaiting
   the commit, so a process that re-reads the checkpoint (the previous design,
   where the gateway re-read on each node-enter signal) races the commit and
   can produce a snapshot missing the just-claimed inbound. The node's
   in-memory `state.messages` already reflects the previous node's committed
   result the instant the reducer applies, so rendering from it is race-free.
   Inbound ts anchors come from the `inbound_messages` table, which commits
   synchronously and never races.

   On enter (not exit): the published snapshot reflects the previous node's
   committed state; the LLM/exec message of the current node is still
   streaming and is represented by a single future partial the frontend keeps
   until the next node-enter snapshot replaces it by item_id. Publishing in
   the `async with` finally would run before the function returns its Command,
   so it would see the same pre-current-node state but offer no benefit.
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import sys
import time
from collections.abc import AsyncGenerator, Generator, Sequence
from typing import Any, TextIO

from langchain_core.messages import BaseMessage
from psycopg_pool import AsyncConnectionPool

from agent._turn_progress import mark_turn_progress
from agent.db import list_chat_inbound_anchors
from shared.config import settings
from shared.event_publisher import AgentEventPublisher
from shared.live_events import TimelineSnapshot
from shared.log import logger
from shared.timeline import (
    DEFAULT_TIMELINE_LIMIT,
    build_timeline_items,
    needs_chat_anchors,
    tail_window,
)


def awaiter_chain_lines(task: asyncio.Task) -> list[str]:
    """Render a task's FULL await chain, outermost coroutine first. For a
    suspended task, `Task.get_stack()` returns only the outermost suspension
    frame ("suspended in langgraph ainvoke") — useless for naming the leaf
    await. Walking `cr_await` / `ag_await` descends through every nested
    coroutine / async generator down to the innermost awaited line (the pool
    SELECT, the redis publish, ...), which is the fact the probe exists for."""
    lines: list[str] = []
    awaitable: object = task.get_coro()
    while awaitable is not None:
        frame: Any = getattr(awaitable, "cr_frame", None) or getattr(awaitable, "ag_frame", None)  # pyright: ignore[reportUnknownArgumentType]
        if frame is not None:
            code = frame.f_code
            lines.append(f'  File "{code.co_filename}", line {frame.f_lineno}, in {code.co_name}')
        awaitable = getattr(awaitable, "cr_await", None) or getattr(awaitable, "ag_await", None)  # pyright: ignore[reportUnknownArgumentType]
    return lines


def _dump_async_tasks() -> None:
    """Print every asyncio task's full await chain to stderr. The faulthandler
    dump shows thread frames only — an event loop parked in `select()` looks
    identical no matter which await it is waiting on. The await chains carry
    the coroutine frames down to the leaf awaited line. Runs as a loop timer
    callback: a loop that is idle-in-select still wakes for timers, so this
    fires precisely when the loop is stuck waiting on a silent socket."""
    tasks = asyncio.all_tasks()
    out = [f"=== asyncio task await chains (stall probe, {len(tasks)} tasks) ==="]
    for task in tasks:
        out.append(f"--- task {task.get_name()} ---")
        out.extend(awaiter_chain_lines(task))
    # Target sys.__stderr__ (the interpreter's original stderr), not the
    # live sys.stderr, so a diagnostic lands on the real stream no matter
    # what user-visible routing is in place. One single write —
    # the faulthandler watchdog thread writes to this same stderr from another
    # thread, so a line-by-line loop would interleave with it. Skip if the
    # process has no real stderr: a diagnostic must never crash its host.
    stderr = sys.__stderr__
    if stderr is None:
        return
    stderr.write("\n".join(out) + "\n")
    stderr.flush()


# Nodes whose body legitimately blocks without bound — the claim node parks in
# the idle Redis pub/sub wait for hours. Arming the stall guard there would
# fire a full stack dump on every idle period, so it is exempt; every other
# node (llm / exec / before / after) has a bounded healthy runtime and a stall
# past the threshold is genuinely anomalous.
_STALL_GUARD_EXEMPT = frozenset({"claim"})


def _real_stderr() -> TextIO | None:
    """The stable real-fd stderr to point diagnostic dumps at. faulthandler
    writes via a raw file descriptor, so its target MUST expose a usable
    `fileno()`. `sys.__stderr__` is the interpreter's original stderr,
    never routed, so it keeps its real fd regardless of the calling
    context. Returns None if even that has no usable fileno (closed /
    detached) — a diagnostic must never crash its host, so the caller then
    skips the dump rather than hand faulthandler a bad fd."""
    stderr = sys.__stderr__
    if stderr is None:
        return None
    try:
        stderr.fileno()
    except (OSError, ValueError):  # io.UnsupportedOperation subclasses both
        return None
    return stderr


@contextlib.contextmanager
def _stall_dump_guard(node_name: str) -> Generator[None]:
    """Arm a one-shot stack dump if the body outlasts
    `settings.agent.node_stall_dump_seconds`: faulthandler dumps every thread's frames AND
    a loop timer dumps every asyncio task's coroutine frames (the thread dump
    of a loop awaiting a silent socket only shows `select()` — the task dump
    names the awaited line). Both land on the real stderr (captured per agent);
    the enclosing `[node enter ...]` log line identifies which node. 0 (prod
    default) is a no-op; nodes in `_STALL_GUARD_EXEMPT` never arm; a process with
    no real-fd stderr skips the dump rather than crash. Nodes run one at a time,
    so the single global faulthandler timer never collides.
    """
    threshold = settings.agent.node_stall_dump_seconds
    if threshold <= 0 or node_name in _STALL_GUARD_EXEMPT:
        yield
        return
    stderr = _real_stderr()
    if stderr is None:
        yield
        return
    # Task dump at threshold, faulthandler 2s later: both write to the real
    # stderr but from different threads (loop vs faulthandler's watchdog), so
    # firing them together interleaves the outputs into garbage.
    faulthandler.dump_traceback_later(threshold + 2.0, repeat=False, file=stderr)
    timer = asyncio.get_running_loop().call_later(threshold, _dump_async_tasks)
    try:
        yield
    finally:
        timer.cancel()
        faulthandler.cancel_dump_traceback_later()


# Per-agent cursor of the last msg_idx covered by a published timeline
# snapshot, keyed by agent_id because one host serves many agents.
# Semantics: after a snapshot covering messages[:cursor] is emitted, the next
# node enter renders only messages[cursor:] (incremental snapshot). cursor == 0
# means "never published in this process" (first enter / process restart /
# fork) and forces a full-window snapshot. A cursor past the current history
# (len(messages) < cursor — compaction REMOVE_ALL) also forces full-window.
_SNAPSHOT_CURSOR: dict[int, int] = {}

# Normal graph turns visit several nodes, so emitting each successful exit as a
# separate event makes lifecycle bookkeeping dominate the noise tier. Buffer
# normal flow per agent and flush at claim or invocation return. The agent key
# isolates concurrent turns sharing the host. Exceptions bypass this buffer because their traceback is
# load-bearing diagnostic data.
_NODE_EXIT_AGGREGATE: dict[int, list[dict[str, Any]]] = {}
_NODE_EXIT_AGGREGATE_CAP = 32


def flush_node_exit_aggregate(agent_id: int) -> None:
    """Emit and clear the buffered normal node exits for one graph turn."""
    entries = _NODE_EXIT_AGGREGATE.pop(agent_id, [])
    if not entries:
        return
    logger.info(
        "[node exits] {count}",
        event="node_exit",
        count=len(entries),
        nodes=entries,
    )


def _accumulate_node_exit(
    agent_id: int,
    node_name: str,
    outcome: str,
    duration_seconds: float,
) -> None:
    entries = _NODE_EXIT_AGGREGATE.setdefault(agent_id, [])
    entries.append(
        {
            "node": node_name,
            "outcome": outcome,
            "duration_seconds": duration_seconds,
        }
    )
    if len(entries) >= _NODE_EXIT_AGGREGATE_CAP:
        flush_node_exit_aggregate(agent_id)


@contextlib.asynccontextmanager
async def node_lifecycle(
    node_name: str,
    *,
    messages: Sequence[BaseMessage],
    ops_pool: AsyncConnectionPool | None,
    event_publisher: AgentEventPublisher,
    agent_id: int,
    full_window: bool = False,
) -> AsyncGenerator[None]:
    """Wrap node body with enter/exit events + publish a TimelineSnapshot on enter.

    `messages` is the node's in-memory `state.messages`; the snapshot is
    rendered from it (race-free, unlike a checkpoint re-read). `ops_pool`
    fetches chat inbound ts anchors; None (container / eval) renders inbound
    items with synthetic ts.

    `BaseException` catch (vs Exception) is so SystemExit / KeyboardInterrupt
    also write the exit event; all paths re-raise to preserve propagation.

    `agent_id` is already bound to logger.extra by the host turn identity, so
    all events carry it automatically; but `TimelineSnapshot.agent_id` is wire
    payload and must be passed explicitly.

    `full_window=True` forces the full tail-window snapshot path (used by the
    claim node's turn-end fallback — the only race-free view of a finished
    turn when the frontend may have missed events; see _claim.py). The cursor
    advances to `len(messages)` either way, so a subsequent incremental
    snapshot picks up exactly the next commits.
    """
    # A node enter is turn activity, full stop: the hosted stall guard and the
    # dispatcher's turn-level stale scan both read this clock, and a turn
    # blocked inside one node is exactly what they must be able to see.
    mark_turn_progress(agent_id)
    # The guard spans the pre-enter ops_pool query AND the node body (the
    # `yield`), so a hang in either lands a stack dump naming the blocked frame.
    with _stall_dump_guard(node_name):
        # Incremental snapshot protocol: on enter, publish only what was newly
        # committed since the last published snapshot. The full-window path
        # (first enter after process start, compaction shrink, or the claim
        # node's turn-end fallback) renders the whole tail window; every other
        # enter renders just messages[cursor:] — O(new commits) instead of
        # O(history), and no anchors DB query (modern messages carry their own
        # ava_created_at; anchors only serve legacy rows on the full path).
        cursor = _SNAPSHOT_CURSOR.get(agent_id, 0)
        if full_window or cursor == 0 or len(messages) < cursor:
            # Full-window render. Anchors (the kind='chat' inbound rows) only
            # feed LEGACY inbounds that predate ava_created_at; an all-modern
            # history renders identically with [] — so skip the query unless
            # the window actually needs it. The post-compact enter is exactly
            # such a case: REMOVE_ALL wiped the history and the rebuilt head
            # is freshly stamped, so the snapshot the frontend is waiting on
            # is emitted without a DB round trip.
            anchors = (
                await list_chat_inbound_anchors(ops_pool, agent_id)
                if ops_pool is not None and needs_chat_anchors(messages)
                else []
            )
            items, msg_count = build_timeline_items(messages, anchors)
            # Publish only the newest window — a streaming turn always lands in
            # the tail, and the frontend keeps older windows it scroll-loaded
            # (its merge preserves items below the snapshot's msg_idx floor).
            # msg_count stays the full state.messages length so the
            # future-partial boundary is unaffected.
            window, _ = tail_window(items, DEFAULT_TIMELINE_LIMIT)
        else:
            # Incremental: only the messages past the cursor. msg_count stays
            # the FULL len(messages) — the frontend's future-partial boundary
            # (`msg_idx == msg_count`) depends on it (hard invariant). The
            # incremental window can never contain the system-prompt item
            # (message 0 is always below the cursor).
            # NOTE: pass the FULL messages list — build_timeline_items computes
            # msg_count = len(messages) from its first argument, and that must
            # stay the full history length (hard invariant).
            inc_items, msg_count = build_timeline_items(messages, [], start=cursor)
            window = inc_items
        # An empty window carries no items and no signal — skip the emit on
        # BOTH paths. The incremental case (no new commits since the last
        # snapshot) would otherwise spam SSE at node-enter frequency; the
        # full-window case is the post-REMOVE_ALL init_context enter, where
        # emitting an empty snapshot made the frontend's compact-reset window
        # replace the timeline with a blank panel before the rebuilt-history
        # snapshot arrived (the "context UI doesn't refresh after compact"
        # report). The next enter renders the rebuilt head.
        if not window:
            # Keep the enter observability record even without a snapshot
            # (death analysis reads the node_enter trail).
            logger.info(
                "[node enter {node}] msgs={msg_count}",
                event="node_enter",
                node=node_name,
                msg_count=msg_count,
            )
            t0 = time.monotonic()
            try:
                yield
            except asyncio.CancelledError:
                _accumulate_node_exit(
                    agent_id,
                    node_name,
                    "cancelled",
                    time.monotonic() - t0,
                )
                raise
            except BaseException as e:
                logger.opt(exception=True).info(
                    "[node exit {node}] outcome=exception:{exc_name} {duration_seconds:.2f}s",
                    event="node_exit",
                    node=node_name,
                    outcome=f"exception:{type(e).__name__}",
                    exc_name=type(e).__name__,
                    duration_seconds=time.monotonic() - t0,
                )
                raise
            else:
                _accumulate_node_exit(
                    agent_id,
                    node_name,
                    "ok",
                    time.monotonic() - t0,
                )
            return
        # No system-prompt special-casing: the incremental protocol already
        # bounds the 0.0 payload — a snapshot never re-sends it unless the
        # full-window path renders it (first publish of a process, compact
        # shrink, claim turn-end fallback — all rare), and the frontend's
        # id-replace merge keeps a single copy either way. (The old Aw-Snap
        # drop rule existed because every node enter used to re-send the full
        # history; incremental snapshots fixed that at the source, so the drop
        # was redundant complexity — and it caused #615: at spawn GET
        # /timeline is empty and the dropped 0.0 never reached the frontend
        # until a manual refresh.)
        event_publisher.emit(
            TimelineSnapshot(
                agent_id=agent_id,
                items=[it.model_dump() for it in window],
                msg_count=msg_count,
            ).model_dump_json()
        )
        # Advance the cursor only after a successful emit: a render failure
        # (fail-loud raise) propagates → node fails → process restarts →
        # cursor resets to 0 → the next enter full-window recovers. Never
        # advancing past a failed render keeps the retry honest.
        _SNAPSHOT_CURSOR[agent_id] = len(messages)
        t0 = time.monotonic()
        logger.info(
            "[node enter {node}] msgs={msg_count}",
            event="node_enter",
            node=node_name,
            msg_count=msg_count,
        )
        try:
            yield
        except asyncio.CancelledError:
            _accumulate_node_exit(
                agent_id,
                node_name,
                "cancelled",
                time.monotonic() - t0,
            )
            raise
        except BaseException as e:
            logger.opt(exception=True).info(
                "[node exit {node}] outcome=exception:{exc_name} {duration_seconds:.2f}s",
                event="node_exit",
                node=node_name,
                outcome=f"exception:{type(e).__name__}",
                exc_name=type(e).__name__,
                duration_seconds=time.monotonic() - t0,
            )
            raise
        else:
            _accumulate_node_exit(
                agent_id,
                node_name,
                "ok",
                time.monotonic() - t0,
            )
