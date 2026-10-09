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
`inbound_messages` on a short cadence (`_INTERRUPT_POLL_S` = 2s, `agent/graph/interrupt.py`),
deliberately NOT sharing the claim node's Redis pub/sub listener — sharing it
was the root cause of the 2026-08-02 lost-wake incident (agent 2476, 30.06s
pickup). The watcher sets an asyncio.Event the moment one is queued; inside the
node `asyncio.wait` races the streaming task vs `cancel_event.wait()`. On
context exit the watcher is cancelled. A missed signal is not lost — it stays a
pending row the claim node dispatches next pass.

State type hint (`state: _state.AgentState`): static only; see `agent/graph/exec/node.py` — the
graph build, not the annotation, decides which state class a node receives.
Module layout (Task #1004 >800-line split): streaming consumption, the cancel
race, chunk assembly + final-message validation, and the error taxonomy /
consecutive-error tracking live in the sibling modules ``_stream.py`` /
``_cancel.py`` / ``_chunk.py`` (all in this ``llm/`` package) plus the parent
package's ``llm_errors.py``; this module keeps the node entry + turn dispatch
(``llm_node``, ``_llm_node_impl``). ``_cancel`` imports ``LlmGoto`` from here,
so ``_race_stream_vs_cancel`` is imported lazily inside ``_llm_node_impl``
rather than at module top (keeps the import graph acyclic). The immutable base
system prompt + lazy SDK overview capture live in the parent package's
``_base_prompt.py``, imported lazily by ``system_prompt.build_system_prompt``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime
from typing import Any, Literal, NoReturn, cast

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphBubbleUp
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent import state as _state
from agent.graph._callbacks import RedisStreamHandler
from agent.graph.llm_errors import (
    FatalLLMStreamError,
    FatalProviderError,
    LlmLedger,
    LLMRetryBudgetExceededError,
    LLMStreamStallPairError,
)
from agent.graph.node_log import node_lifecycle
from agent.graph.tool_calls import code_from_args
from agent.hooks.compact import auto_compact_for_llm
from agent.hooks.understanding_chunks import due_chunk_update
from agent.llm.usage import log_llm_usage
from agent.nodes import AFTER_EXEC, BEFORE_EXEC
from agent.state_channels import CircuitState
from base.agents.context import AvaContext, agent_id_from_config
from base.agents.messages.kwargs import read_ava_kwargs
from base.config import settings
from base.db.transaction import async_write_transaction
from base.events.live.projection import TokenUsage
from base.events.live.publisher import AgentEventPublisher
from base.lm.content import content_blocks
from base.lm.usage import CACHE_MECHANISM_MIXED, CACHE_SCOPE_EXPLICIT_BLOCK
from base.log import logger

from ._chunk import _assemble_final_message
from ._retry import RETRY_REMAINING_ATTR, Attempt, retry_wait
from ._stream import _stream_with_cache_retry

# llm_node normal → BEFORE_EXEC; cancel / no-tool-call halt → AFTER_EXEC
# (halted=True makes after_exec route back to claim). Type narrow catches illegal goto.
LlmGoto = Literal["before_exec", "after_exec", "init_context", "claim"]


def _log_llm_retry_duration(
    attempt: Attempt,
    *,
    outcome: Literal["succeeded", "attempts_exhausted", "budget_exhausted"],
) -> None:
    """Emit the final wall-clock duration for a retried LLM node."""
    if attempt.number < 2:
        return
    logger.info(
        "LLM retry sequence {outcome} after {duration_seconds:.2f}s",
        event="llm_retry",
        outcome=outcome,
        duration_seconds=attempt.elapsed_seconds(),
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
    model: str,
) -> None:
    """Post-stream metadata finalization for one completed AIMessage turn.

    Three coupled side effects, all derived from the just-assembled final_msg:
    - persist the per-block thinking wall-clock onto the message
      (`ava_reasoning_ms_by_block`: {block_idx -> ms}) before it enters
      state.messages, so a timeline reload renders each thinking block's
      "thought for X seconds" from a real elapsed value rather than the
      timeline's own synthetic per-turn microsecond offset. Keyed alongside
      the other ava_* message tags; absent when no thinking streamed.
      base/agents/history/timeline.py reads it back.
    - log standardized token usage (`events.event_name='llm_usage'`).
    - emit the live TokenUsage SSE event. usage_metadata is accurate only after
      the stream completes (chunks carry only output_tokens increments;
      input_tokens lands once in the final), and the frontend uses input_tokens
      for the context-window gauge.
    """
    # Stamp the turn's real wall-clock onto the message so the timeline renders
    # the agent's reply / reasoning / code at their actual time, not the
    # synthetic anchor+offset fallback. base/agents/history/timeline.py prefers this ts.
    kw = read_ava_kwargs(final_msg)
    kw["ava_created_at"] = datetime.now(UTC).isoformat()
    reasoning_ms_by_block = handler.reasoning_ms_by_block
    if reasoning_ms_by_block:
        # str keys: additional_kwargs is checkpoint-serialized, JSON object
        # keys are strings; base/agents/history/timeline.py reads back with str(block_idx).
        kw["ava_reasoning_ms_by_block"] = {
            str(block_idx): ms for block_idx, ms in reasoning_ms_by_block.items()
        }
    code_ms_by_block = handler.code_ms_by_block
    if code_ms_by_block:
        kw["ava_code_ms_by_block"] = {
            str(block_idx): ms for block_idx, ms in code_ms_by_block.items()
        }
    log_llm_usage(
        final_msg,
        model=model,
        agent_id=agent_id,
        latency_ms=handler.llm_latency_ms,
        decode_ms=handler.llm_decode_ms,
        # Gemini + explicit cachedContent reports only the explicit block in
        # cache_read (implicit tail hits are billed but not reported), so the
        # event carries the honest provenance instead of a bare number.
        cache_mechanism=(CACHE_MECHANISM_MIXED if handler.used_explicit_cache else None),
        cache_scope=(CACHE_SCOPE_EXPLICIT_BLOCK if handler.used_explicit_cache else None),
    )
    usage = final_msg.usage_metadata or {}
    from base.lm.reasoning import extract_reasoning_tokens

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
    *,
    ledger: LlmLedger,
) -> Command[LlmGoto]:
    """Invoke the LLM, retrying a failed try on the schedule `_retry.retry_wait` decides.

    `ledger` is the graph's `LlmLedger`: what the node remembers per agent between tries and turns.

    The node retries itself rather than through a graph-level policy: the host's one graph
    serves every agent, and only the node knows whose model and id the schedule is for.
    """
    agent_id = agent_id_from_config(config)
    model = runtime.context.require_agent().brain.llm_model
    first_started_at = time.time()
    failed = 0
    while True:
        try:
            return await llm_attempt(
                state, runtime, config, Attempt(failed + 1, first_started_at), ledger
            )
        except GraphBubbleUp:
            raise
        except Exception as exc:
            failed += 1
            wait = retry_wait(exc, failed, model=model, agent_id=agent_id, ledger=ledger)
            if wait is None:
                raise
            await asyncio.sleep(wait)
            logger.info(
                "Retrying the llm node after {wait:.2f}s (failed try {failed}): {error}",
                wait=wait,
                failed=failed,
                error=f"{type(exc).__name__}: {exc}",
            )


def _enforce_retry_budget(attempt: Attempt, agent_id: int, ledger: LlmLedger) -> None:
    if (
        attempt.elapsed_seconds() >= settings.lm.llm_retry_max_total_seconds
        # The transient-retry budget is sized for seconds-scale
        # backoffs; while a delayed stall sequence is active its own
        # schedule (streak cap) owns the bound — see _retry.
        and not ledger.stall_pair_streak_active(str(agent_id))
    ):
        _log_llm_retry_duration(attempt, outcome="budget_exhausted")
        _raise_retry_budget_exhausted(attempt.number)


def _note_failed_attempt(
    exc: BaseException, attempt: Attempt, runtime: Runtime[AvaContext], agent_id: int
) -> None:
    """Mark the settled attempt as progress and record the retry budget left on `exc`."""
    runtime.context.turn_progress.mark(agent_id)
    if isinstance(exc, Exception) and not isinstance(exc, LLMStreamStallPairError):
        remaining_seconds = settings.lm.llm_retry_max_total_seconds - attempt.elapsed_seconds()
        if remaining_seconds <= 0.0 and not isinstance(exc, LLMRetryBudgetExceededError):
            _log_llm_retry_duration(attempt, outcome="budget_exhausted")
        elif not isinstance(exc, (FatalLLMStreamError, FatalProviderError)):
            from base.lm.registry import resolve_setting

            max_attempts = resolve_setting(
                "llm_retry_max_attempts",
                model=runtime.context.require_agent().brain.llm_model,
            )
            if attempt.number >= max_attempts:
                _log_llm_retry_duration(attempt, outcome="attempts_exhausted")
        # `_retry.retry_wait` reads this attribute to clip the next wait to the budget.
        with contextlib.suppress(AttributeError):  # an exception type that refuses new attributes
            setattr(exc, RETRY_REMAINING_ATTR, remaining_seconds)


async def llm_attempt(
    state: _state.AgentState,
    runtime: Runtime[AvaContext],
    config: RunnableConfig,
    attempt: Attempt,
    ledger: LlmLedger,
) -> Command[LlmGoto]:
    """One try of the llm node: invoke LLM + streaming token publish + RAII cancel. See module
    docstring for details.

    `try / except / else` pattern (vs previous try/finally): finally under an async retry
    boundary may have sys.exc_info() already handled by the retry runner, so it cannot get the
    active exception → traceback in events.payload becomes "NoneType: None\\n" (167/168 latent
    bug). Inside the except block, sys.exc_info() is 100% the currently raising exception;
    `logger.opt(exception=True)` is required to actually capture traceback into the record.
    """
    turn_start = time.monotonic()
    agent_id = agent_id_from_config(config)
    event_publisher = runtime.context.event_publisher
    assert event_publisher is not None, "llm_node requires ctx.event_publisher"  # noqa: S101
    async with node_lifecycle(
        "llm",
        messages=state.messages,
        ops_pool=runtime.context.ops_pool,
        event_publisher=event_publisher,
        agent_id=agent_id,
        turn_progress=runtime.context.turn_progress,
    ):
        try:
            _enforce_retry_budget(attempt, agent_id, ledger)
            result = await _llm_node_impl(state, runtime, config, ledger)
        except BaseException as exc:
            # A settled (failed) attempt is real activity: mark the turn clock
            # now so the following retry sleep is the ONLY silence the hosted
            # no-progress stall guard sees. The delayed stall schedule may
            # sleep for up to `llm_stall_retry_max_interval_seconds` (default
            # 1800s, jittered, under the guard's 2400s) — without this mark
            # the silence would include the whole stalled attempt on top of
            # the sleep and could cross the guard's bound.
            _note_failed_attempt(exc, attempt, runtime, agent_id)
            logger.opt(exception=True).warning(
                "turn ended in {duration_seconds:.2f}s ok=False — _llm_node_impl raised",
                event="turn_end",
                duration_seconds=time.monotonic() - turn_start,
                ok=False,
            )
            # Do not publish Error here — this except runs on every failed attempt; sending the frontend N "errors" then
            # succeeding on retry would contradict. The Error event is published
            # once by outer `agent/turn/runloop.py:_invoke_graph_with_lifecycle_logging`
            # after retries are exhausted (Cancelled is still published by
            # _llm_node_impl itself).
            raise
        else:
            _log_llm_retry_duration(attempt, outcome="succeeded")
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


def _silent_idle_command(
    final_msg: AIMessage, agent_id: int, model: str, ledger: LlmLedger
) -> Command[LlmGoto] | None:
    """Continue-loop vs guard-halt decision for a silent-idle turn.

    Keeps the reasoning in context and loops straight back to the LLM
    (halted=False -> claim's multi-step continue path) so the ava_silent_idle
    plugin can inject a Continue nudge before the next turn. The ledger's
    output-token budget bounds a model that habitually reasons without acting:
    each silent idle consumes at least one unit, even when its provider reports
    zero output tokens. At the cap, halt to idle instead of spending another
    model call. The budget resets on the first non-silent-idle turn (the caller
    resets it). Returns None when the turn is not a silent idle.
    """
    if not _is_silent_idle(final_msg):
        return None
    tid = str(agent_id)
    usage = final_msg.usage_metadata or {}
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    budget_tokens = max(output_tokens, 1)
    cumulative_output_tokens = ledger.silent_idle_output_tokens(tid) + budget_tokens
    cap = settings.lm.llm_silent_idle_max_output_tokens
    from base.lm.pricing import quote

    priced = quote(model, 0, output_tokens, 0)
    estimated_cost_usd = priced.cost_usd if priced is not None else None
    if cap > 0 and cumulative_output_tokens >= cap:
        ledger.reset_silent_idle(tid)
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
    ledger.record_silent_idle_output_tokens(tid, cumulative_output_tokens)
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
    - last_turn_fatal_at = NULL in the same statement: a completed turn is the
      recovery signal that clears the corpse marker (the heartbeat circuit
      breaker's "first successful LLM call closes the breaker" moment).
    - permanent_reject_streak = 0 in the same statement: a completed turn is
      also the recovery signal that closes the recovery circuit breaker
      (`base/agents/recovery/breaker.py`) — the only reset, so two consecutive
      permanent rejections with no success between them keep it >= the halt
      threshold. `last_permanent_reject_reason` is cleared with it — the reason
      class belongs to the streak generation.
    - last_message_text = the AI text WHEN this turn produced any: it survives
      compact (which replaces the whole checkpoint but not this column), read
      back by get_last_message.
    """
    # A completed LLM step is turn progress regardless of the DB outcome
    # below — the hosted stall clock must not age just because the persist
    # write itself failed.
    ctx.turn_progress.mark(agent_id)
    if ctx.ops_pool is None:
        return
    try:
        async with async_write_transaction(ctx.ops_pool) as conn, conn.cursor() as cur:
            if text:
                await cur.execute(
                    "UPDATE agents_meta SET last_active_at = now(), last_message_text = %s, "
                    "last_turn_fatal_at = NULL, permanent_reject_streak = 0, last_permanent_reject_reason = NULL "
                    "WHERE id = %s",
                    (text, agent_id),
                )
            else:
                await cur.execute(
                    "UPDATE agents_meta SET last_active_at = now(), last_turn_fatal_at = NULL, "
                    "permanent_reject_streak = 0, last_permanent_reject_reason = NULL "
                    "WHERE id = %s",
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
    ledger: LlmLedger,
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

    compacted = await auto_compact_for_llm(state, runtime, config)
    if compacted is not None:
        goto = compacted.pop("goto")
        return Command[LlmGoto](update=compacted, goto=goto)

    # Consecutive same-error retry cap: if the same LLMStreamError has occurred
    # N times across retries, fail fast with FatalLLMStreamError instead of
    # wasting another 30-480s retry cycle on a deterministic error.
    ledger.check_consecutive_error_cap(str(agent_id))
    # Stall-pair cap: a spent delayed stall-retry streak (default 4 pairs) ends
    # the turn here as a fatal abort — the next attempt would only burn another
    # stalled pair while the provider is still degraded.
    ledger.check_stall_pair_cap(str(agent_id))

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
        turn_progress=ctx.turn_progress,
    )

    # chunks is a list — cancel race path: streaming task and outer coroutine
    # share the same list; on task cancel, whatever has been accumulated is
    # what's in the list. AIMessageChunk addition merges content +
    # usage_metadata; `message_chunk_to_message` converts to AIMessage.
    chunks: list[AIMessageChunk] = []

    # Lazy import: `_cancel` imports `LlmGoto` from this module at its top
    # level, so a top-level `from ._cancel import ...` here would be a
    # circular import (Task #1004 split). sys.modules serves it after the
    # first turn — no per-turn cost.
    from ._cancel import _race_stream_vs_cancel

    cancelled_cmd = await _race_stream_vs_cancel(
        ctx,
        agent_id,
        _stream_with_cache_retry(
            llm,
            list(state.messages),
            chunks=chunks,
            handler=handler,
            agent=ctx.require_agent(),
            binding=ctx.llm_binding,
        ),
        handler,
        ledger,
    )
    if cancelled_cmd is not None:
        return cancelled_cmd

    # Stream succeeded -- reset the consecutive-error tracker so a future
    # transient error (different type) starts from 1, not accumulated. The
    # stall-pair streak resets with it: a served request proves the provider
    # recovered, so a later stall pair starts a fresh delayed schedule.
    ledger.clear_consecutive_errors(str(agent_id))
    ledger.reset_stall_pair_streak(str(agent_id))

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
        ctx.event_publisher,
        agent_id,
        final_msg,
        handler,
        ctx.require_agent().brain.llm_model,
    )

    # Understanding chunk cut: an enqueue past the token threshold moves the
    # segment's cut, carried on whichever command ends this turn.
    cut_update = await due_chunk_update(
        state.compact,
        list(state.messages),
        final_msg,
        pool=ctx.ops_pool,
        agent_id=agent_id,
        model=ctx.require_agent().brain.llm_model,
        overrides=ctx.require_agent().overrides,
    )

    silent_idle_cmd = _silent_idle_command(
        final_msg, agent_id, ctx.require_agent().brain.llm_model, ledger
    )
    if silent_idle_cmd is not None:
        return Command[LlmGoto](
            update={**cast("dict[str, Any]", silent_idle_cmd.update), **cut_update},
            goto=silent_idle_cmd.goto,  # pyright: ignore[reportArgumentType]
        )

    # Any non-silent-idle turn ends the streak — a single real action clears it.
    ledger.reset_silent_idle(str(agent_id))

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
            update={"messages": [final_msg], "halted": True, **cut_update},
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
    return Command[LlmGoto](update={"messages": [final_msg], **cut_update}, goto=BEFORE_EXEC)
