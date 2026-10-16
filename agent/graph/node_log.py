from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import sys
import time
from collections.abc import AsyncGenerator, Callable, Generator, Sequence
from typing import Any, TextIO

from langchain_core.messages import BaseMessage

from base.agents.history.timeline import (
    build_timeline_items,
    tail_window,
    timeline_default_limit,
)
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.agents.observation.turn_progress import TurnProgress
from base.events.live.projection import TimelineSnapshot
from base.events.live.publisher import AgentEventPublisher
from base.log import logger


# ava:ref agent/graph/docs/node-lifecycle.ava.okf.md#stall-probe-arming
def awaiter_chain_lines(task: asyncio.Task[Any]) -> list[str]:
    lines: list[str] = []
    awaitable: Any = task.get_coro()
    while awaitable is not None:
        frame = getattr(awaitable, "cr_frame", None) or getattr(awaitable, "ag_frame", None)
        if frame is not None:
            code = frame.f_code
            lines.append(f'  File "{code.co_filename}", line {frame.f_lineno}, in {code.co_name}')
        awaitable = getattr(awaitable, "cr_await", None) or getattr(awaitable, "ag_await", None)
    return lines


def dump_async_tasks() -> None:
    tasks = asyncio.all_tasks()
    out = [f"=== asyncio task await chains (stall probe, {len(tasks)} tasks) ==="]
    for task in tasks:
        out.append(f"--- task {task.get_name()} ---")
        out.extend(awaiter_chain_lines(task))
    stderr = sys.__stderr__
    if stderr is None:
        return
    stderr.write("\n".join(out) + "\n")
    stderr.flush()


# ava:ref agent/graph/docs/node-lifecycle.ava.okf.md#stall-probe-arming
_STALL_GUARD_EXEMPT = frozenset({"claim"})


def _real_stderr() -> TextIO | None:
    stderr = sys.__stderr__
    if stderr is None:
        return None
    try:
        stderr.fileno()
    except (OSError, ValueError):
        return None
    return stderr


# ava:ref agent/graph/docs/node-lifecycle.ava.okf.md#stall-probe-arming
@contextlib.contextmanager
def _stall_dump_guard(
    node_name: str, *, read_stall_seconds: Callable[[], float]
) -> Generator[None]:
    threshold = read_stall_seconds()
    if threshold <= 0 or node_name in _STALL_GUARD_EXEMPT:
        yield
        return
    stderr = _real_stderr()
    if stderr is None:
        yield
        return
    faulthandler.dump_traceback_later(threshold + 2.0, repeat=False, file=stderr)
    timer = asyncio.get_running_loop().call_later(threshold, dump_async_tasks)
    try:
        yield
    finally:
        timer.cancel()
        faulthandler.cancel_dump_traceback_later()


# ava:ref agent/graph/docs/node-lifecycle.ava.okf.md#timeline-snapshot-cursor
SNAPSHOT_CURSOR: dict[int, int] = {}

_NODE_EXIT_AGGREGATE: dict[int, list[dict[str, Any]]] = {}

_NODE_EXIT_AGGREGATE_CAP = 32


# ava:ref agent/graph/docs/node-lifecycle.ava.okf.md#node-exit-aggregation
def flush_node_exit_aggregate(agent_id: int) -> None:
    entries = _NODE_EXIT_AGGREGATE.pop(agent_id, [])
    if not entries:
        return
    logger.info(
        "[node exits] {count}",
        event="node_exit",
        agent_id=agent_id,
        count=len(entries),
        nodes=entries,
    )


# ava:ref agent/graph/docs/node-lifecycle.ava.okf.md#node-exit-aggregation
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


# ava:ref agent/graph/docs/node-lifecycle.ava.okf.md#node-exit-owner
@contextlib.asynccontextmanager
async def node_lifecycle(
    node_name: str,
    *,
    messages: Sequence[BaseMessage],
    event_publisher: AgentEventPublisher,
    agent_id: int,
    turn_progress: TurnProgress,
    read_stall_seconds: Callable[[], float],
    timeline_inputs: TimelineReadInputs,
    limit_reader: Callable[[], int],
    full_window: bool = False,
) -> AsyncGenerator[None]:
    turn_progress.mark(agent_id)
    with _stall_dump_guard(node_name, read_stall_seconds=read_stall_seconds):
        cursor = SNAPSHOT_CURSOR.get(agent_id, 0)
        if full_window or cursor == 0 or len(messages) < cursor:
            items, msg_count = build_timeline_items(messages, inputs=timeline_inputs)
            window, _ = tail_window(items, timeline_default_limit(limit_reader=limit_reader))
        else:
            inc_items, msg_count = build_timeline_items(
                messages, start=cursor, inputs=timeline_inputs
            )
            window = inc_items
        # ava:ref agent/graph/docs/node-lifecycle.ava.okf.md#timeline-snapshot-cursor
        if not window:
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
                    agent_id=agent_id,
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
        event_publisher.emit(
            TimelineSnapshot(
                agent_id=agent_id,
                items=[it.model_dump() for it in window],
                msg_count=msg_count,
            ).model_dump_json()
        )
        SNAPSHOT_CURSOR[agent_id] = len(messages)
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
                agent_id=agent_id,
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
