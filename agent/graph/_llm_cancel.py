"""Cancel race for the llm node — streaming task vs durable-interrupt event.

``_race_stream_vs_cancel`` races the streaming coroutine against the
``subscribe_interrupt`` cancel event and decides which won: a cancel that
fires first discards the partial generation and returns the halted Command
(goto=after_exec, so claim dispatches the inbound); a completed stream
propagates exceptions (consecutive-error tracking + provider-error
classification applied) and returns None.

Split out of ``_llm.py`` (Task #1004 >800-line outlier). Imports ``LlmGoto``
from ``_llm.py`` (the node module owns the goto type); ``_llm.py`` therefore
imports this module lazily inside ``_llm_node_impl`` to keep the import graph
acyclic.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Coroutine
from typing import Any

from langgraph.types import Command

from agent.nodes import AFTER_EXEC
from shared.live_events import Cancelled
from shared.log import logger

from ._callbacks import RedisStreamHandler
from ._context import AvaContext
from ._interrupt import subscribe_interrupt
from ._llm import LlmGoto
from ._llm_errors import (
    LLMStreamError,
    _classify_and_log_provider_error,
    _record_consecutive_error,
)


async def _race_stream_vs_cancel(
    ctx: AvaContext,
    agent_id: int,
    stream_coro: Coroutine[Any, Any, None],
    handler: RedisStreamHandler,
) -> Command[LlmGoto] | None:
    """Race the streaming task against the durable-interrupt cancel event.

    Returns the cancelled Command (halted=True → after_exec, so claim
    dispatches the inbound) when the cancel event fires first; returns None
    once the stream completed cleanly (stream exceptions propagate, with the
    consecutive-error tracker and provider-error classification applied).

    subscribe_interrupt is RAII: on node entry it watches for a durable
    interrupt inbound (kind cancel/terminate) for this agent via the same
    Redis pub/sub path as every inbound, with an initial SELECT. On context
    exit the watcher is cancelled. A missed signal is not lost — it stays a
    pending row the claim node dispatches next pass.
    """
    assert ctx.event_publisher is not None, (  # noqa: S101
        "_race_stream_vs_cancel requires ctx.event_publisher"
    )
    async with subscribe_interrupt(ctx.ops_pool, agent_id) as cancel_event:
        stream_task = asyncio.create_task(stream_coro)
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {stream_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except BaseException:
            stream_task.cancel()
            cancel_task.cancel()
            # gather(return_exceptions=True) swallows task internal exceptions
            # back into the result list; the only thing suppress here can catch
            # is gather's own CancelledError (e.g. when the outer task is also
            # cancelled). Real BaseExceptions like MemoryError / SystemExit
            # are not swallowed — otherwise the outer raise would obscure the
            # original root cause when re-raising.
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(stream_task, cancel_task, return_exceptions=True)
            raise

        if cancel_task in done:
            # cancel_event arrived first — abort streaming (same-tick race
            # trade-off as the exec subprocess poll: cancel always wins)
            stream_task.cancel()
            # Narrow to CancelledError — a real exception in stream_task (e.g. a
            # chunk decode bug) should propagate (through outer BaseException
            # cleanup); don't widen to suppress(Exception) which would swallow
            # it together with cancel.
            with contextlib.suppress(asyncio.CancelledError):
                await stream_task
            cancel_task.cancel()
            # Notify frontend the turn was aborted — the SSE Cancelled event
            # resets turn-active state and drops the still-streaming partial
            # bubble. emit() is non-blocking (best-effort live view).
            ctx.event_publisher.emit(Cancelled(agent_id=agent_id).model_dump_json())
            # Clean discard: an interrupted generation is dropped whole, never
            # committed to history. Generating text has no side effects — the
            # honest record is "the user interrupted before I said anything",
            # so there is nothing to preserve. And because no complete
            # AIMessage is committed, there is no dangling tool_use owing a
            # tool_result: the discard leaves history API-valid with zero
            # repair messages. (Code execution is the opposite case — once a
            # tool_use is committed, exec_node emits a matching [cancelled by
            # user] tool_result on the soft-cancel path. A hard cancel
            # (SIGTERM/restart/stop -> asyncio.CancelledError) can still kill
            # the process before that write lands; the dangling-tool_use
            # repair (agent/hooks/repair.py) backfills the tool_result at the
            # next boot / before_llm pass.) Return halted rather than
            # raising CancelledError (a BaseException that made asyncio.run
            # silently exit the process; agent #45 incident). chunks
            # accumulated so far are intentionally ignored.
            logger.info(
                "[{label}] {body}",
                label="llm-cancelled",
                body="discarded partial generation",
                event="llm_cancelled",
            )
            return Command[LlmGoto](update={"halted": True}, goto=AFTER_EXEC)

        # Stream completed normally (stream_task entered done first)
        cancel_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_task
        try:
            stream_task.result()  # Propagate any internal stream exception
        except LLMStreamError as e:
            _record_consecutive_error(str(agent_id), e)
            raise
        except Exception as e:
            # Classify the provider exception (shared.lm.errors.classify_error) and
            # log the structured (error_class, provider, status) for the postmortem.
            # A PERMANENT class (400 context length / schema, 401/402/403 auth /
            # billing / forbidden, 404 unknown model, 422 schema) or a configured
            # fatal error type (e.g. engine_overloaded_error that survived the
            # non-streaming fallback in _consume_llm) becomes a FatalProviderError:
            # the retry policy skips it and the host settles the turn to idle
            # instead of exhausting the full backoff budget and dying into
            # terminated. A TRANSIENT / UNKNOWN class re-raises the original so the
            # RetryPolicy retries — unknown is never guessed into fail-fast.
            fatal = _classify_and_log_provider_error(e)
            if fatal is not None:
                raise fatal from e
            raise
        # finish: flush any remaining partial-JSON code increment + publish
        # LLMDone (frontend reload trigger). Not called on cancel path —
        # partial items wait for the next event reload; LLMDone would make
        # frontend pull state not yet committed, causing misalignment
        handler.finish()
        return None
