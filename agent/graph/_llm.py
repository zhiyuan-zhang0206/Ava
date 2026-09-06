"""llm node: invokes the LLM to stream Python code generation + cancel handling.

Normal path returns Command(goto="before_exec"); cancel path returns
Command(update={..., halted: True}, goto="after_exec") — under cycling
topology halted=True; after_exec routes back to claim to dispatch inbound
(cancel does not exit the process).

AIMessage text content (the agent's per-turn text output) takes two paths:
- Real-time: RedisStreamHandler streams ChatStart/ChatDelta to the UI
- Durable: AIMessage naturally lands in LangGraph state.messages; the timeline
  endpoint pulls the final text from state on refetch — no separate table
  persistence, avoiding dual-source timestamp drift.

Dependencies injected via `runtime.context: AvaContext`; agent_id read from
RunnableConfig (LangGraph checkpointer standard).

Interrupt uses `subscribe_interrupt` RAII: on node entry it watches for a
durable interrupt inbound (kind cancel/terminate) for this agent by polling
`inbound_messages` on a short cadence (`_INTERRUPT_POLL_S` = 2s, `agent/graph/_interrupt.py`),
deliberately NOT sharing the claim node's Redis pub/sub listener — sharing it
was the root cause of the 2026-08-02 lost-wake incident (agent 2476, 30.06s
pickup). The watcher sets an asyncio.Event the moment one is queued; inside the
node `asyncio.wait` races the streaming task vs `cancel_event.wait()`. On
context exit the watcher is cancelled. A missed signal is not lost — it stays a
pending row the claim node dispatches next pass.

State type hint key design (`state: _state.AgentState` + `from __future__ import
annotations`): see `agent/graph/_exec.py` module docstring last paragraph — in
short, LangGraph narrows channels by the node's first param type hint;
directly importing `AgentState` captures the BaseAgentState alias and drops
all plugin fields; using module attribute + deferred annotation evaluation
picks up the dynamic class rebound by build_agent_state.
Module layout (Task #1004 >800-line split): streaming consumption, the cancel
race, chunk assembly + final-message validation, and the error taxonomy /
consecutive-error tracking moved to the sibling modules ``_llm_stream.py`` /
``_llm_cancel.py`` / ``_llm_chunk.py`` / ``_llm_errors.py``; this module keeps
the node entry + turn dispatch (``llm_node``, ``_llm_node_impl``) and
re-exports the moved names for backward compatibility. ``_llm_cancel`` imports
``LlmGoto`` from here, so ``_race_stream_vs_cancel`` is imported lazily inside
``_llm_node_impl`` rather than at module top (keeps the import graph acyclic).
"""

from __future__ import annotations

import contextlib
import io
import time
from datetime import UTC, datetime
from typing import Any, Literal, NoReturn, cast

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command

# Import the SDK for system-prompt introspection. Graph nodes read the agent
# identity from RunnableConfig; SDK calls use the host's bound turn identity.
import ava
from agent import state as _state
from agent._turn_progress import mark_turn_progress
from agent.observe import log_llm_usage
from agent.state_channels import CircuitState
from shared.config import settings
from shared.config.turn_view import turn_settings
from shared.db_transaction import async_write_transaction
from shared.event_publisher import AgentEventPublisher
from shared.live_events import TokenUsage
from shared.lm.content import content_blocks
from shared.log import logger
from shared.message_kwargs import read_ava_kwargs

from ._callbacks import RedisStreamHandler
from ._context import AvaContext, agent_id_from_config

# Backward-compat re-exports — these names moved to the split modules
# (`_llm_errors` / `_llm_stream` / `_llm_chunk`) in the Task #1004 >800-line
# split but stay importable from `agent.graph._llm` so existing callers and
# tests keep working. New code should import from the owning module.
from ._llm_chunk import (
    _TOOL_CLAIMED_REASONS as _TOOL_CLAIMED_REASONS,
)
from ._llm_chunk import (
    _assemble_final_message,
)
from ._llm_chunk import (
    _sanitize_thinking_blocks as _sanitize_thinking_blocks,
)
from ._llm_chunk import (
    _validate_stop_reason as _validate_stop_reason,
)
from ._llm_errors import (
    FatalLLMStreamError as FatalLLMStreamError,
)
from ._llm_errors import (
    FatalProviderError as FatalProviderError,
)
from ._llm_errors import (
    LLMRetryBudgetExceededError as LLMRetryBudgetExceededError,
)
from ._llm_errors import (
    LLMStreamCorruptedError as LLMStreamCorruptedError,
)
from ._llm_errors import (
    LLMStreamError as LLMStreamError,
)
from ._llm_errors import (
    LLMStreamSilentIdleError as LLMStreamSilentIdleError,
)
from ._llm_errors import (
    LLMStreamStallTimeoutError as LLMStreamStallTimeoutError,
)
from ._llm_errors import (
    LLMStreamTruncatedError as LLMStreamTruncatedError,
)
from ._llm_errors import (
    LLMStreamUnexpectedStopReasonError as LLMStreamUnexpectedStopReasonError,
)
from ._llm_errors import (
    _check_consecutive_error_cap,
    _clear_consecutive_errors,
)
from ._llm_errors import (
    _classify_and_log_provider_error as _classify_and_log_provider_error,
)
from ._llm_errors import (
    _consecutive_errors as _consecutive_errors,
)
from ._llm_errors import (
    _is_fatal_provider_error_type as _is_fatal_provider_error_type,
)
from ._llm_errors import (
    _parse_provider_error_type as _parse_provider_error_type,
)
from ._llm_errors import (
    _record_consecutive_error as _record_consecutive_error,
)
from ._llm_stream import (
    _ainvoke_single_chunk as _ainvoke_single_chunk,
)
from ._llm_stream import (
    _consume_llm as _consume_llm,
)
from ._llm_stream import (
    _consume_stream_with_stall_timeout as _consume_stream_with_stall_timeout,
)
from ._llm_stream import (
    _stream_with_cache_retry as _stream_with_cache_retry,
)
from ._node_log import node_lifecycle
from ._nodes import AFTER_EXEC, BEFORE_EXEC
from ._tool_calls import code_from_args


def _capture_ava_overview() -> str:
    """Emit the `# ava` overview — the public SDK surface as a name + docstring index.

    `ava.help(ava)` renders ava's own docstring plus one entry per public
    top-level namespace — both the static ones (agents / monitor / self /
    schedule / memory / files / shell / skills / ...) and any a plugin
    registered at runtime — each as `from . import X` + that module's docstring.
    Underscore-private members don't appear. Registered namespaces are listed
    too on purpose: a plugin promotes its *members* in its own section, but the
    namespace itself must show here so it's discoverable even if the plugin adds
    no section — otherwise a top-level namespace could silently vanish. This is
    the natural "what's my SDK" index; full per-namespace detail (function
    signatures) stays on demand via `ava.help(ava.X)`.

    Scope is driven by `AVA_SDK_DISABLE`: a disabled namespace is removed from
    `ava` entirely, so it simply doesn't appear here — e.g. SWE-Bench disables
    monitor / schedule / self / agents / skills and the overview narrows to
    match, no framework change needed.

    Duplication note: namespaces a plugin promotes in detail via its own
    `register_system_prompt_section` (ava_code → cwd / files / shell) still
    appear here at index level (name + docstring); the plugin section adds the
    function stubs *without* repeating the docstring (help(ava.X) drops a
    submodule target's own docstring — see ava._format_module_stub).
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ava.help(ava)
    return buf.getvalue()


# At module top-level execution time, ava plugins are not yet loaded (order: import
# _llm → module top → main() → build_graph() → _load_extensions()). So capture is
# deferred to the build_system_prompt() call site — by then plugins are loaded
# and each plugin's `register_system_prompt_section` has already registered.
#
# No cache: build_system_prompt() is called only once in an agent's lifetime
# when the first _claim sees state.messages empty; afterward SystemMessage is
# persisted into state.messages[0] and reused across restart — cache hit rate
# is 1/1, no point.
def _get_ava_overview() -> str:
    return _capture_ava_overview()


# _BASE_SYSTEM_PROMPT is the immutable core of the system prompt.
# The {_AVA_OVERVIEW} placeholder is filled by _get_ava_overview() lazy capture
# the first time build_system_prompt() is called — by then _load_extensions() has
# run and all plugin namespaces are visible.
# Plugins inject extension content via register_system_prompt_section().
_BASE_SYSTEM_PROMPT = """\
You are Ava, an agent that acts by writing Python code — call the
`execute_code(code: str)` tool — each call runs in an ephemeral interpreter. To idle, do not output any
tool calls.

Before using any `ava.*` function, you must explicitly `import ava` in your code.

{_AVA_OVERVIEW}"""


# llm_node normal → BEFORE_EXEC; cancel / no-tool-call halt → AFTER_EXEC
# (halted=True makes after_exec route back to claim). Type narrow catches illegal goto.
LlmGoto = Literal["before_exec", "after_exec"]


_silent_idle_output_tokens: dict[str, int] = {}
"""thread_id -> output-token budget units across consecutive silent-idle turns.

Separate from _consecutive_errors because a silent idle is a successful stream.
Every silent idle consumes at least one unit, so a provider that reports
reasoning content with zero output tokens cannot bypass the bound. The budget
resets on the first turn that produces text or a tool call; process lifecycle
resets it naturally on restart.
"""
_RETRY_REMAINING_ATTR = "_ava_retry_budget_remaining_seconds"


def _retry_elapsed_seconds(runtime: Runtime[AvaContext]) -> tuple[int, float] | None:
    """Return this node's retry attempt and cumulative elapsed time when retried."""
    execution_info = runtime.execution_info
    if execution_info is None or execution_info.node_first_attempt_time is None:
        return None
    return (
        execution_info.node_attempt,
        max(0.0, time.time() - execution_info.node_first_attempt_time),
    )


def _log_llm_retry_duration(
    runtime: Runtime[AvaContext],
    *,
    outcome: Literal["succeeded", "attempts_exhausted", "budget_exhausted"],
) -> None:
    """Emit the final wall-clock duration for a retried LLM node."""
    timing = _retry_elapsed_seconds(runtime)
    if timing is None:
        return
    attempt, duration_seconds = timing
    if attempt < 2:
        return
    logger.info(
        "LLM retry sequence {outcome} after {duration_seconds:.2f}s",
        event="llm_retry",
        outcome=outcome,
        duration_seconds=duration_seconds,
    )


def _raise_retry_budget_exhausted(attempt: int) -> NoReturn:
    """End an LLM node before its expired retry budget permits another call."""
    raise LLMRetryBudgetExceededError(
        "LLM retry wall-clock budget "
        f"({settings.lm.llm_retry_max_total_seconds:.0f}s) exhausted before attempt {attempt}"
    )


def _finalize_turn_observability(
    publisher: AgentEventPublisher,
    agent_id: int,
    final_msg: AIMessage,
    handler: RedisStreamHandler,
    task_id: int | None,
) -> None:
    """Post-stream metadata finalization for one completed AIMessage turn.

    Three coupled side effects, all derived from the just-assembled final_msg:
    - persist the per-block thinking wall-clock onto the message
      (`ava_reasoning_ms_by_block`: {block_idx -> ms}) before it enters
      state.messages, so a timeline reload renders each thinking block's
      "thought for X seconds" from a real elapsed value rather than the
      timeline's own synthetic per-turn microsecond offset. Keyed alongside
      the other ava_* message tags; absent when no thinking streamed.
      shared/timeline.py reads it back.
    - log standardized token usage (`events.event_name='llm_usage'`).
    - emit the live TokenUsage SSE event. usage_metadata is accurate only after
      the stream completes (chunks carry only output_tokens increments;
      input_tokens lands once in the final), and the frontend uses input_tokens
      for the context-window gauge.
    """
    # Stamp the turn's real wall-clock onto the message so the timeline renders
    # the agent's reply / reasoning / code at their actual time, not the
    # synthetic anchor+offset fallback. shared/timeline.py prefers this ts.
    kw = read_ava_kwargs(final_msg)
    kw["ava_created_at"] = datetime.now(UTC).isoformat()
    reasoning_ms_by_block = handler.reasoning_ms_by_block
    if reasoning_ms_by_block:
        # str keys: additional_kwargs is checkpoint-serialized, JSON object
        # keys are strings; shared/timeline.py reads back with str(block_idx).
        kw["ava_reasoning_ms_by_block"] = {
            str(block_idx): ms for block_idx, ms in reasoning_ms_by_block.items()
        }
    code_ms_by_block = handler.code_ms_by_block
    if code_ms_by_block:
        kw["ava_code_ms_by_block"] = {
            str(block_idx): ms for block_idx, ms in code_ms_by_block.items()
        }
    usage_tally = log_llm_usage(
        final_msg,
        model=turn_settings.lm.llm_model,
        latency_ms=handler.llm_latency_ms,
        decode_ms=handler.llm_decode_ms,
        task_id=task_id,
    )
    if task_id is not None and usage_tally is not None:
        try:
            from ava_builtins.plugins.ava_fleet.task_registry import record_task_usage

            record_task_usage(task_id, token_count=usage_tally[0], cost_usd=usage_tally[1])
        except Exception:
            logger.warning(
                "[{label}] {body}",
                label="task-usage",
                body=f"failed to record usage for task {task_id}",
            )
    usage = final_msg.usage_metadata or {}
    from shared.lm.reasoning import extract_reasoning_tokens

    reasoning_tokens = extract_reasoning_tokens(
        final_msg.usage_metadata,
        total_reasoning_chars=handler.total_reasoning_chars,
    )
    publisher.emit(
        TokenUsage(
            agent_id=agent_id,
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            reasoning_tokens=reasoning_tokens,
        ).model_dump_json()
    )


async def llm_node(
    state: _state.AgentState,
    runtime: Runtime[AvaContext],
    config: RunnableConfig,
) -> Command[LlmGoto]:
    """Invoke LLM + streaming token publish + RAII cancel. See module docstring for details.

    `try / except / else` pattern (vs previous try/finally): finally under
    async + langgraph retry boundary may have sys.exc_info() already handled
    by the retry runner, so it cannot get the active exception → traceback in
    events.payload becomes "NoneType: None\\n" (167/168 latent bug). Inside
    the except block, sys.exc_info() is 100% the currently raising exception;
    `logger.opt(exception=True)` is required to actually capture traceback
    into the record.
    """
    turn_start = time.monotonic()
    event_publisher = runtime.context.event_publisher
    assert event_publisher is not None, "llm_node requires ctx.event_publisher"  # noqa: S101
    async with node_lifecycle(
        "llm",
        messages=state.messages,
        ops_pool=runtime.context.ops_pool,
        event_publisher=event_publisher,
        agent_id=agent_id_from_config(config),
    ):
        try:
            timing = _retry_elapsed_seconds(runtime)
            if timing is not None and timing[1] >= settings.lm.llm_retry_max_total_seconds:
                _log_llm_retry_duration(runtime, outcome="budget_exhausted")
                _raise_retry_budget_exhausted(timing[0])
            result = await _llm_node_impl(state, runtime, config)
        except BaseException as exc:
            timing = _retry_elapsed_seconds(runtime)
            if timing is not None and isinstance(exc, Exception):
                attempt, duration_seconds = timing
                remaining_seconds = settings.lm.llm_retry_max_total_seconds - duration_seconds
                if remaining_seconds <= 0.0 and not isinstance(exc, LLMRetryBudgetExceededError):
                    _log_llm_retry_duration(runtime, outcome="budget_exhausted")
                elif not isinstance(exc, (FatalLLMStreamError, FatalProviderError)):
                    from shared.lm.registry import resolve_setting

                    max_attempts = resolve_setting(
                        "llm_retry_max_attempts", model=turn_settings.lm.llm_model
                    )
                    if attempt >= max_attempts:
                        _log_llm_retry_duration(runtime, outcome="attempts_exhausted")
                # `_build._build_llm_retry` consumes this transient attribute
                # synchronously before it decides and schedules the next retry.
                with contextlib.suppress(Exception):
                    setattr(exc, _RETRY_REMAINING_ATTR, remaining_seconds)
            logger.opt(exception=True).warning(
                "turn ended in {duration_seconds:.2f}s ok=False — _llm_node_impl raised",
                event="turn_end",
                duration_seconds=time.monotonic() - turn_start,
                ok=False,
            )
            # Do not publish Error here — this except is wrapped by langgraph retry
            # and runs on every failed attempt; sending the frontend N "errors" then
            # succeeding on retry would contradict. The Error event is published
            # once by outer `agent/_runloop.py:_invoke_graph_with_lifecycle_logging`
            # after retries are exhausted (Cancelled is still published by
            # _llm_node_impl itself).
            raise
        else:
            timing = _retry_elapsed_seconds(runtime)
            if timing is not None and timing[0] >= 2:
                _log_llm_retry_duration(runtime, outcome="succeeded")
            logger.info(
                "turn ended in {duration_seconds:.2f}s ok=True",
                event="turn_end",
                duration_seconds=time.monotonic() - turn_start,
                ok=True,
            )
            # A successful LLM call is the circuit-healed signal: the provider
            # accepted a request again, so whatever permanently rejected the
            # last turn (overflow compacted away, balance topped up, key
            # fixed) has cleared. Close the breaker so heartbeats resume
            # routing normally. result is the fresh Command _llm_node_impl
            # just built, so mutating its update dict is safe. The cancel
            # path is the one success-shaped return that carries NO
            # `messages` — no stream completed there (the partial generation
            # was discarded), so it must not close the breaker.
            update = result.update
            if state.circuit.open and update is not None and "messages" in update:
                update["circuit"] = CircuitState()
                logger.info(
                    "heartbeat circuit breaker CLOSED — LLM call accepted again",
                    event="circuit_breaker_closed",
                    agent_id=agent_id_from_config(config),
                )
            return result


def _is_silent_idle(final_msg: AIMessage) -> bool:
    """Silent idle: model produced reasoning (thinking blocks / output tokens /
    reasoning_content) but emitted no text and no tool_call — the agent appears
    stuck at reasoning.

    Truly empty (no reasoning at all) is NOT a silent idle — looping a
    deterministic empty output only wastes API credits.
    """
    if final_msg.tool_calls or final_msg.text:
        return False
    output_tokens = (final_msg.usage_metadata or {}).get("output_tokens", 0)  # pyright: ignore[reportUnknownMemberType]
    _content: Any = final_msg.content  # pyright: ignore[reportUnknownMemberType]
    has_thinking_block = isinstance(_content, list) and any(
        isinstance(b, dict) and cast(dict[str, Any], b).get("type") == "thinking"
        for b in content_blocks(cast(list[Any], _content))
    )
    return (
        has_thinking_block
        or bool(final_msg.additional_kwargs.get("reasoning_content"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        or output_tokens > 0
    )


def _silent_idle_command(final_msg: AIMessage, agent_id: int) -> Command[LlmGoto] | None:
    """Continue-loop vs guard-halt decision for a silent-idle turn.

    Keeps the reasoning in context and loops straight back to the LLM
    (halted=False -> claim's multi-step continue path) so the ava_silent_idle
    plugin can inject a Continue nudge before the next turn. A per-process
    output-token budget bounds a model that habitually reasons without acting:
    each silent idle consumes at least one unit, even when its provider reports
    zero output tokens. At the cap, halt to idle instead of spending another
    model call. The budget resets on the first non-silent-idle turn (the caller
    pops it). Returns None when the turn is not a silent idle.
    """
    if not _is_silent_idle(final_msg):
        return None
    tid = str(agent_id)
    usage = final_msg.usage_metadata or {}
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    budget_tokens = max(output_tokens, 1)
    cumulative_output_tokens = _silent_idle_output_tokens.get(tid, 0) + budget_tokens
    cap = settings.lm.llm_silent_idle_max_output_tokens
    from shared.lm.pricing import quote

    priced = quote(turn_settings.lm.llm_model, 0, output_tokens, 0)
    estimated_cost_usd = priced.cost_usd if priced is not None else None
    if cap > 0 and cumulative_output_tokens >= cap:
        _silent_idle_output_tokens.pop(tid, None)
        logger.warning(
            "[{label}] {body}",
            label="silent-idle",
            event="silent_idle",
            body=(
                f"reasoning-only output reached {cumulative_output_tokens} budget tokens (cap {cap}) — "
                "halting to idle instead of looping"
            ),
            output_tokens=output_tokens,
            cumulative_output_tokens=cumulative_output_tokens,
            estimated_cost_usd=estimated_cost_usd,
            halted=True,
        )
        return Command[LlmGoto](
            update={"messages": [final_msg], "halted": True},
            goto=AFTER_EXEC,
        )
    _silent_idle_output_tokens[tid] = cumulative_output_tokens
    logger.info(
        "[{label}] {body}",
        label="silent-idle",
        event="silent_idle",
        body=(
            f"reasoning-only output {output_tokens} tokens "
            f"(budget {budget_tokens}; cumulative {cumulative_output_tokens}/{cap or 'inf'}) "
            "— continue-loop with nudge"
        ),
        output_tokens=output_tokens,
        cumulative_output_tokens=cumulative_output_tokens,
        estimated_cost_usd=estimated_cost_usd,
        halted=False,
    )
    return Command[LlmGoto](
        update={"messages": [final_msg], "halted": False},
        goto=AFTER_EXEC,
    )


async def _persist_last_active(ctx: AvaContext, agent_id: int, text: str) -> None:
    """Persist two things every completed turn:
    - last_active_at = now() ALWAYS: this is the agent's real-activity clock
      the heartbeat daemon reads for idle timing. A completed LLM turn is the
      definition of "the agent did real work" — including a tool-only turn with
      no text. It is deliberately NOT written by the ops lifecycle (rollout
      pause / restart / update), and for an idle agent that whole cycle
      runs no LLM turn, so an ops restart cannot reset the idle clock.
    - last_message_text = the AI text WHEN this turn produced any: it survives
      compact (which replaces the whole checkpoint but not this column), read
      back by get_last_message.
    """
    # A completed LLM step is turn progress regardless of the DB outcome
    # below — the hosted stall clock must not age just because the persist
    # write itself failed.
    mark_turn_progress(agent_id)
    if ctx.ops_pool is None:
        return
    try:
        async with async_write_transaction(ctx.ops_pool) as conn, conn.cursor() as cur:
            if text:
                await cur.execute(
                    "UPDATE agents_meta SET last_active_at = now(), last_message_text = %s "
                    "WHERE id = %s",
                    (text, agent_id),
                )
            else:
                await cur.execute(
                    "UPDATE agents_meta SET last_active_at = now() WHERE id = %s",
                    (agent_id,),
                )
    except Exception:
        logger.warning(
            "[{label}] {body}",
            label="last-msg",
            event="last_msg",
            body=f"failed to persist last_active_at / last_message_text for agent {agent_id}",
        )


async def _llm_node_impl(
    state: _state.AgentState,
    runtime: Runtime[AvaContext],
    config: RunnableConfig,
) -> Command[LlmGoto]:
    """Stream the LLM turn: race streaming vs cancel, assemble + validate the
    final message, persist activity, then dispatch the post-stream command."""
    ctx = runtime.context
    assert ctx.event_publisher is not None, (  # noqa: S101
        "_llm_node_impl requires ctx.event_publisher"
    )
    assert ctx.llm is not None, "_llm_node_impl requires ctx.llm"  # noqa: S101
    llm = ctx.llm  # narrowed local — the assert can't reach nested helpers
    agent_id = agent_id_from_config(config)

    # Consecutive same-error retry cap: if the same LLMStreamError has occurred
    # N times across retries, fail fast with FatalLLMStreamError instead of
    # wasting another 30-480s retry cycle on a deterministic error.
    _check_consecutive_error_cap(str(agent_id))

    # Streaming forwarding (chat / reasoning / code) is isolated in
    # RedisStreamHandler — process_chunk is called in the chunk loop; after
    # stream completion, finish() publishes LLMDone (frontend timeline reload
    # trigger). See _callbacks.py for details.
    #
    # msg_idx = `len(state.messages)` = position of this LLM-produced AIMessage
    # (index after LangGraph add_messages reducer appends). Streaming event
    # item_id uses this + block index, matching the id the gateway timeline
    # endpoint computes for the same committed AIMessage, so the frontend
    # merge directly uses stable key matching instead of ts heuristic.
    handler = RedisStreamHandler(
        ctx.event_publisher,
        agent_id,
        msg_idx=len(state.messages),
    )

    # chunks is a list — cancel race path: streaming task and outer coroutine
    # share the same list; on task cancel, whatever has been accumulated is
    # what's in the list. AIMessageChunk addition merges content +
    # usage_metadata; `message_chunk_to_message` converts to AIMessage.
    chunks: list[AIMessageChunk] = []

    # Lazy import: `_llm_cancel` imports `LlmGoto` from this module at its top
    # level, so a top-level `from ._llm_cancel import ...` here would be a
    # circular import (Task #1004 split). sys.modules serves it after the
    # first turn — no per-turn cost.
    from ._llm_cancel import _race_stream_vs_cancel

    cancelled_cmd = await _race_stream_vs_cancel(
        ctx,
        agent_id,
        _stream_with_cache_retry(llm, list(state.messages), chunks=chunks, handler=handler),
        handler,
    )
    if cancelled_cmd is not None:
        return cancelled_cmd

    # Stream succeeded -- reset the consecutive-error tracker so a future
    # transient error (different type) starts from 1, not accumulated.
    _clear_consecutive_errors(str(agent_id))

    if not chunks:
        # LLM returned empty — extremely rare; return empty code per historical
        # behavior (exec_node will run exec(""))
        return Command[LlmGoto](update={"messages": [AIMessage(content="")]}, goto=BEFORE_EXEC)
    final_msg = _assemble_final_message(chunks)

    # Single-tool wire format: code is in tool_calls[0]["args"]["code"], not content.
    # content is the model's text output (e.g. "OK, let me compute fib(10)"),
    # printed separately from code
    text = final_msg.text  # langchain-normalized text extraction (handles str + list-of-blocks)
    if text:
        logger.info("[{label}] {body}", label="text", body=text)
    await _persist_last_active(ctx, agent_id, text)
    _finalize_turn_observability(
        ctx.event_publisher, agent_id, final_msg, handler, state.active_task_id
    )

    silent_idle_cmd = _silent_idle_command(final_msg, agent_id)
    if silent_idle_cmd is not None:
        return silent_idle_cmd

    # Any non-silent-idle turn ends the streak — a single real action clears it.
    _silent_idle_output_tokens.pop(str(agent_id), None)

    if not final_msg.tool_calls:
        # No tool_call = stop turn — halted=True makes after_exec route back
        # to claim to wait for the next inbound. Process stays alive.
        if not text:
            # Truly empty: no tool_call, no text, AND no reasoning — the model
            # produced nothing at all (no tokens spent). A reasoning-only turn
            # took the silent_idle continue-loop above and never reaches here.
            # Distinct WARNING so these turns are countable and the UI symptom
            # maps to a log line.
            logger.warning(
                "[{label}] {body}",
                label="halt",
                body="no tool_call and EMPTY text — empty turn, user sees no reply",
            )
        else:
            logger.info("[{label}] {body}", label="halt", body="no tool_call (idle)")
        return Command[LlmGoto](
            update={"messages": [final_msg], "halted": True},
            goto=AFTER_EXEC,
        )
    logger.info(
        "[{label}] {body}",
        label="code",
        body=code_from_args(final_msg.tool_calls[0]["args"], source="llm final_msg tool_call"),
    )
    # CodeStart + CodeDelta have already been incrementally published by
    # RedisStreamHandler in _stream() + finish() fallback (see _callbacks.py
    # module docstring)
    return Command[LlmGoto](update={"messages": [final_msg]}, goto=BEFORE_EXEC)
