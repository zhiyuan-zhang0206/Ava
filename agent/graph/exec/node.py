"""exec node: agent-written code runs in a disposable subprocess.

Each execute_code call runs in a fresh child process (`agent/exec_child.py`),
so a stuck native call is SIGKILLable without touching the agent process
(issue #184). Each graph step executes one call and returns its delta to LangGraph.
Pending calls route back to exec after that commit; the completed batch routes to
after_exec, then claim, which decides whether to wait or continue the turn.

Core mechanisms:
  - Subprocess backend (`agent/graph/exec/_subprocess.py`): the parent spawns
    one `python -I -X utf8 -m agent.exec_child` per exec, polls every 50ms, streams
    output through the chunk pipeline. Cancel/timeout sends a signal then
    closes the process group after a grace period. Natural root exit also closes
    the domain, so an `os._exit`
    cannot strand an ordinary descendant holding stdout. Cancellation returns
    only after the direct child is reaped and the pipe reader gets its bounded
    join. The child rebuilds the state snapshot from the request envelope; the
    state-update delta (plugin fields, security findings) rides the result
    envelope back and is validated here.
  - Halt signal uses exception type rather than exit code: agent code raising
    `LifecycleExit` (AgentTermination / AgentRestart / SystemHalt) → captured
    in result_holder["lifecycle"] → exec_node decides halted + writes marker
    based on isinstance.
  - The child writes stdout/stderr line-buffered onto the pipe (both
    streams merge chronologically — same as what running Python in a
    terminal shows); the parent drains the pipe into a `StreamingTextIO`
    and pushes each new accumulated chunk to redis every 50ms poll
    (frontend streaming). Accumulation is bounded by
    `exec_output_accumulation_max_chars`: past it the middle is dropped as
    it streams and a `StreamCap` rides the result to the envelope, so a
    runaway print loop is truncated rather than left to OOM the parent —
    the run itself is not killed.
  - `_run_in_subprocess` returns the `_ExecResult` sum type
    (`_ExecDone | _ExecCancelled | _ExecTimedOut | _ExecLifecycle |
    _ExecCrashed`) plus the raw child envelope; exec_node dispatches via
    `match`, illegal state combinations are unrepresentable. Ordinary
    exception tracebacks are already in the stream output.

State type hint (`state: _state.AgentState`): the annotation is static only, the base schema, so
`state.messages` / `state.halted` type-check; plugin fields are accessed dynamically. The state a
node receives is the class `build_graph` registers it with (`input_schema=`, which carries the
plugins' channels), not what the annotation names.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal

from langchain_core.messages import AIMessage, AnyMessage, ToolCall, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command, Overwrite

from agent import state as _state
from agent.graph._attach_drain import build_attach_message
from agent.graph._attach_merge import merge_attachments
from agent.graph.agent_traceback import format_full_traceback
from agent.graph.exec._notes import merge_exec_notes
from agent.graph.interrupt import subscribe_interrupt
from agent.graph.node_log import node_lifecycle
from agent.graph.tool_calls import code_from_args, normalize_tool_calls
from agent.messages import exec_output_message
from agent.nodes import AFTER_EXEC, EXEC
from agent.state import AttachState, _validate_plugin_state_keys
from base.agents.context import AvaContext, agent_id_from_config
from base.agents.lifecycle import AgentImpersonation, AgentRestart, AgentTermination, SystemHalt
from base.config import settings
from base.events.live.projection import Cancelled, ExecOutput, ExecStart
from base.log import logger

from ._result import (
    _ExecCancelled,
    _ExecCrashed,
    _ExecDone,
    _ExecLifecycle,
    _ExecResult,
    _ExecTimedOut,
)
from ._stream import ExecOutputChunkPublisher
from ._subprocess import _run_in_subprocess
from .output import crashed_no_output_body, wrap_code_output
from .protocol import ResultPayload

# Each exec step commits one call; remaining calls return to EXEC before AFTER_EXEC.
ExecGoto = Literal["exec", "after_exec"]

# The `_ExecResult` sum type — 5 mutually exclusive variants + a shared output
# field — lives in `_result.py` (moved there so the exec-subprocess
# machinery can construct the same type without importing this module, which
# would close an import cycle). Re-exported here: exec_node's match dispatch
# and existing tests keep importing from agent.graph.exec.node.
#
# Lifecycle priority is implemented at the construction site
# (`_construct_exec_result`): if a lifecycle exc exists, `_ExecLifecycle` is
# constructed directly, skipping the cancelled/timed_out branches — the
# "lifecycle always wins" race decision moved from dispatch site to
# construction site; exec_node match no longer has to consider the race.


async def exec_node(
    state: _state.AgentState,
    runtime: Runtime[AvaContext],
    config: RunnableConfig,
) -> Command[ExecGoto]:
    """Run agent-written code in one disposable child process. See module docstring."""
    event_publisher = runtime.context.event_publisher
    assert event_publisher is not None, "exec_node requires ctx.event_publisher"  # noqa: S101
    async with node_lifecycle(
        "exec",
        messages=state.messages,
        ops_pool=runtime.context.ops_pool,
        event_publisher=event_publisher,
        agent_id=agent_id_from_config(config),
        turn_progress=runtime.context.turn_progress,
    ):
        return await _exec_node_impl(state, runtime, config)


async def _exec_with_node_shield(
    coro: Awaitable[tuple[_ExecResult, ResultPayload | None]], agent_id: int
) -> tuple[_ExecResult, ResultPayload | None]:
    """Graph-level exec node timeout — defense-in-depth above the per-code-block
    exec_timeout_seconds. If the inner deadline misses a cancellable framework
    hang, this outer shield requests cancellation and surfaces a timeout after
    the owned process-resource barrier finishes. It is not an independent hard
    bound on an OS close/reap call that itself wedges. (The interrupt
    subscription sits outside this wait_for and is bounded by its own watcher
    exit timeout.)"""
    try:
        return await asyncio.wait_for(coro, timeout=settings.sandbox.exec_node_timeout_seconds)
    except TimeoutError:
        logger.error(
            "[exec(node-timeout)] exec_node timed out after {timeout}s — "
            "inner code-exec timeout did not trigger; possible framework hang. "
            "Returning timeout ToolMessage so the LLM can react.",
            event="exec_node_timeout",
            timeout=settings.sandbox.exec_node_timeout_seconds,
            agent_id=agent_id,
        )
        return (
            _ExecTimedOut(
                output=(
                    f"[exec node timeout after {settings.sandbox.exec_node_timeout_seconds:.0f}s] "
                    "Execution was stopped by an internal safeguard and did not "
                    "complete. This does not necessarily mean your code was slow; "
                    "consider re-running it, or moving long-running work to a "
                    "persistent shell session."
                )
            ),
            None,
        )


async def _run_agent_code(
    state: _state.AgentState,
    ctx: AvaContext,
    agent_id: int,
    code: str,
    chunk_publisher: ExecOutputChunkPublisher,
) -> tuple[
    _ExecResult,
    dict[str, Any],
    int,
    list[dict[str, Any]] | None,
    list[dict[str, Any]] | None,
]:
    """Run the agent's code in one disposable child process.

    The parent does not touch the ava.state slot — the child rebuilds the
    snapshot from the request envelope (`agent/exec_child.py`), and the
    state-update delta (plugin fields, security findings) rides the result
    envelope back. Validation is fail-fast on a tampered slot, and the child
    receives the bound turn's config maps so its SDK calls
    resolve the same settings. Returns
    (result, plugin_state_update, exec_ms, attachments, sdk_calls)."""
    config_overlay = ctx.require_agent().overlay()
    exec_started = time.monotonic()
    async with subscribe_interrupt(ctx.ops_pool, agent_id) as cancel_event:
        outcome = await _exec_with_node_shield(
            _run_in_subprocess(
                ctx.require_db(),
                code,
                ctx,
                cancel_event,
                settings.sandbox.exec_timeout_seconds,
                chunk_publisher,
                state=state.model_dump(),
                config_overlay=config_overlay,
            ),
            agent_id,
        )
    # Wall-clock surfaced on the code_output item ("ran in 1.3s"); cancel /
    # timeout still report the honest time-before-stop.
    exec_ms = round((time.monotonic() - exec_started) * 1000)
    result, payload = outcome
    if isinstance(result, _ExecCancelled) and cancel_event.is_set():
        result = replace(result, reason=cancel_event.reason)
    if payload is not None and payload.state_update_error is not None:
        # The child reported a tampered slot (agent set ava.state_update to a
        # non-dict) — raise the TypeError the child would have raised.
        raise TypeError(payload.state_update_error)
    delta = payload.state_update if payload is not None else None
    plugin_state_update = _validate_plugin_state_keys(dict(delta), state.__class__) if delta else {}
    attachments = plugin_state_update.pop("attach", None)
    sdk_calls = payload.sdk_calls if payload is not None else None
    return result, plugin_state_update, exec_ms, attachments, sdk_calls


def _emit_exec_boot_failed(agent_id: int, exc: BaseException) -> None:
    """The `exec_child_boot_failed` signal: the child died before the agent's code ran."""
    logger.warning(
        "[exec-boot-failed] exec child crashed before running code: {exc_type}: {exc_msg}",
        event="exec_child_boot_failed",
        agent_id=agent_id,
        exc_type=getattr(exc, "exc_type", None) or type(exc).__name__,
        exc_msg=(getattr(exc, "exc_msg", None) or str(exc))[:200],
    )


def _dispatch_exec_result(
    result: _ExecResult,
    ctx: AvaContext,
    agent_id: int,
    *,
    referenced_messages: Sequence[AnyMessage] = (),
) -> tuple[bool, str]:
    """Map the `_ExecResult` sum type to (halted, result_text).

    Lifecycle priority (lifecycle always wins the cancel/timeout race) is
    implemented at the construction site in `_construct_exec_result` (`_result.py`); the match directly
    consumes the sum type. Exhaustiveness: pyright strict + match narrowing make
    a forgotten variant a static error (replaces the hand-written fallthrough).
    """
    # Present on every variant (see the sum-type definitions): when the
    # accumulation budget dropped the middle mid-run, the envelope needs it to
    # report the true produced length and to stop calling the archive complete.
    stream_cap = result.stream_cap
    match result:
        case _ExecLifecycle(output=output, exc=SystemHalt()):
            # ava.self.compact already INSERTed compact_summary inbound; append
            # "[system halt]" at the end (agent's real output comes first).
            halted = True
            extra = "[system halt] You just called ava.self.compact; your context has been compacted and you will continue as the same agent\n"
            output = (output if not output or output.endswith("\n") else output + "\n") + extra
            result_text = wrap_code_output(
                output, stream_cap=stream_cap, referenced_messages=referenced_messages
            )
            logger.info("[{label}] {body}", label="exec", body=result_text)
            logger.info("[{label}] {body}", label="halt", body="system_halt (compact)")
        case _ExecLifecycle(
            output=output, exc=AgentTermination() | AgentRestart() | AgentImpersonation() as exc
        ):
            # Restart/terminate enqueue lifecycle inbounds; impersonation
            # records consent in its lease. Their drivers resume after exec
            # cleanup, without adding a duplicate "[halt]" annotation here.
            halted = True
            result_text = wrap_code_output(
                output, stream_cap=stream_cap, referenced_messages=referenced_messages
            )
            logger.info("[{label}] {body}", label="exec", body=result_text)
            logger.info(
                "[{label}] {body}",
                label="halt",
                body=f"lifecycle {type(exc).__name__}",
            )
        case _ExecLifecycle(exc=other_exc):
            # Exhaustive fallthrough: future LifecycleExit subclass not handled
            # in the two cases above falls here and raises — safer than silently
            # taking the "ordinary exception" halted=False path. Implements
            # AGENTS.md "enum dispatch must be exhaustive".
            raise TypeError(
                f"Unrecognized LifecycleExit subclass: {type(other_exc).__name__!r} — "
                f"dispatch ladder missed update"
            )
        case _ExecCancelled(output=output, reason=reason):
            halted = True
            result_text = wrap_code_output(
                output,
                cancelled=True,
                cancel_reason=reason,
                stream_cap=stream_cap,
                referenced_messages=referenced_messages,
            )
            logger.info(
                "[{label}] {body}", label="exec-cancelled", body=result_text, event="exec_cancelled"
            )
            # Notify frontend of abort (symmetric with llm_node cancel path;
            # the timeout path does not send Cancelled — not a user cancel).
            assert ctx.event_publisher is not None  # noqa: S101 — asserted by caller; narrowed for the emit
            ctx.event_publisher.emit(Cancelled(agent_id=agent_id).model_dump_json())
        case _ExecTimedOut(output=output):
            # Timeout is ordinary feedback, not a stop-turn signal: the envelope
            # hints at long-running primitives; the next LLM round adapts.
            halted = False
            result_text = wrap_code_output(
                output,
                timed_out=True,
                stream_cap=stream_cap,
                referenced_messages=referenced_messages,
            )
            logger.info(
                "[{label}] {body}", label="exec-timeout", body=result_text, event="exec_timeout"
            )
        case _ExecCrashed(
            output=output, exc=exc, full_traceback=child_traceback, code_reached=code_reached
        ):
            # Ordinary exception: `output` carries the agent-facing (filtered)
            # traceback; the log gets the full unfiltered chain (framework/SDK
            # bugs invisible in the agent view stay diagnosable). INFO +
            # event=exec_failed — trial-and-error is the normal dev loop, not
            # an operator alert (metrics still aggregate by event name).
            # The child ships its formatted traceback in the envelope
            # (`child_traceback`); parent-side construction failures (spawn
            # error, unserializable state) format from `exc`.
            #
            # P0 #2100: an EMPTY output on a crash means nothing reached the
            # agent's stdout — never wrap it as "(no output)", which asserts
            # the code ran. Say what happened instead: with the child's
            # code_reached flag, "the code was NOT executed" (boot crash),
            # "ran, printed nothing" or "unknown".
            halted = False
            if not output:
                output = crashed_no_output_body(exc, code_reached=code_reached)
            result_text = wrap_code_output(
                output, stream_cap=stream_cap, referenced_messages=referenced_messages
            )
            logger.info(
                "[{label}] {body}\n[full traceback]\n{full_traceback}",
                label="exec-failed",
                body=result_text,
                full_traceback=child_traceback or format_full_traceback(exc),
                event="exec_failed",
                exc_type=type(exc).__name__,
            )
            if code_reached is False:
                # Bootstrap-class failure: the child died before the agent's
                # code ever ran — the class that used to vanish as "(no
                # output)". A distinct WARNING event is the operator signal
                # (P2 #2102); the alert rule reads it from the event stream.
                _emit_exec_boot_failed(agent_id, exc)
        case _ExecDone(output=output):
            halted = False
            result_text = wrap_code_output(
                output, stream_cap=stream_cap, referenced_messages=referenced_messages
            )
            logger.info("[{label}] {body}", label="exec", body=result_text)
    return halted, result_text


def _attach_model(ctx: AvaContext) -> str:
    """The model name attachments are packed for (media capability gate).

    Same resolution as the claim fallback drain (`_attach_drain.py`): the live
    LLM's model name, else the configured turn model. ``ctx.llm`` can be None
    (tests / container edge), hence the getattr fallback.
    """
    return getattr(ctx.llm, "model_name", None) or ctx.require_agent().brain.llm_model


async def _exec_single_call(
    state: _state.AgentState,
    ctx: AvaContext,
    agent_id: int,
    call: ToolCall,
    exec_msg_idx: int,
) -> tuple[dict[str, Any], bool]:
    """Execute one invocation; return its state delta and whether it compacted."""
    assert ctx.event_publisher is not None, (  # noqa: S101
        "_exec_node_impl requires ctx.event_publisher"
    )
    # Deferring notes/media keeps result positions consecutive. Streaming and
    # checkpoint reconstruction must use those same positions.
    ctx.event_publisher.emit(
        ExecStart(agent_id=agent_id, item_id=f"{exec_msg_idx}.0").model_dump_json()
    )

    state_messages_update: list[AnyMessage] = []
    if call["name"] != "execute_code" or "code" not in call["args"]:
        error = exec_output_message(
            content=f"unknown tool {call['name']!r}; only `execute_code(code: str)` is registered",
            tool_call_id=call["id"] or "",
            created_at=datetime.now(UTC),
        )
        ctx.event_publisher.emit(
            ExecOutput(
                agent_id=agent_id, item_id=f"{exec_msg_idx}.0", content=str(error.content)
            ).model_dump_json()
        )
        return {"messages": [error], "halted": False}, False

    # Streaming chunks and the final ExecOutput share the same item_id
    # computed above; the frontend uses it to append chunks to the same
    # code_output item; on completion, ExecOutput upserts at the same id,
    # replacing with wrap_code_output envelope version.
    chunk_publisher = ExecOutputChunkPublisher(
        ctx.event_publisher,
        agent_id,
        item_id=f"{exec_msg_idx}.0",
    )

    (
        result,
        plugin_state_update,
        exec_ms,
        envelope_attachments,
        envelope_sdk_calls,
    ) = await _run_agent_code(
        state,
        ctx,
        agent_id,
        code_from_args(call["args"], source=f"tool_call {call['id']!r}"),
        chunk_publisher,
    )
    halted, result_text = _dispatch_exec_result(
        result, ctx, agent_id, referenced_messages=state.messages
    )

    # Pop the plugin's messages delta out of the state update — merged below
    # after the ToolMessage instead of riding the dict **spread (which would
    # clobber the ToolMessage). Popped + drained unconditionally so a compact
    # turn (REMOVE_ALL'd by claim) leaks nothing to later turns.
    plugin_messages = plugin_state_update.pop("messages", None)

    # Compact path (SystemHalt): write nothing back — claim REMOVE_ALLs the
    # whole history this turn, so ToolMessage/notes would be wiped anyway.
    compact_halt = isinstance(result, _ExecLifecycle) and isinstance(result.exc, SystemHalt)
    if compact_halt:
        # Findings annotate the history claim is about to wipe; the rest of the delta (plugin
        # fields) is written back as before, and the findings otherwise ride the spread below
        # into `state.security_findings` for the after_exec hook.
        plugin_state_update.pop("security_findings", None)
    else:
        # The UI shows exactly what the agent sees in exec output — same blob
        # fed back to the LLM below (ExecOutput shares item_id with the chunk).
        ctx.event_publisher.emit(
            ExecOutput(
                agent_id=agent_id,
                item_id=f"{exec_msg_idx}.0",
                content=result_text,
            ).model_dump_json()
        )

        msg = exec_output_message(
            content=result_text,
            tool_call_id=call["id"] or "",
            exec_ms=exec_ms,
            sdk_calls=envelope_sdk_calls,
            created_at=datetime.now(UTC),
        )
        state_messages_update.append(msg)

        # In-memory system-note injection (user ruling 2026-08-11): plugin
        # context notes merge into this exec's delta after the ToolMessage
        # (ordering rationale: _notes.py).
        state_messages_update = merge_exec_notes(state_messages_update, plugin_messages)
    update: dict[str, Any] = {
        "messages": state_messages_update,
        "halted": halted,
        **plugin_state_update,
    }
    if not compact_halt:
        # Attachments registered during this execute_code call are packed into
        # a media HumanMessage appended right after the exec output — the model
        # sees the attached files on its very next step of the SAME turn (user
        # ruling 2026-08-26). The claim-node drain (_attach_drain.py) remains
        # as the fallback for edge paths that skip this update (compact halt).
        merged_attach = merge_attachments(state.attach, envelope_attachments)
        update["attach"] = merged_attach
        attach_msg = build_attach_message(merged_attach, _attach_model(ctx))
        if attach_msg is not None:
            state_messages_update.append(attach_msg)
            update["attach"] = AttachState()
    return update, compact_halt


def _remaining_exec_calls(
    messages: Sequence[AnyMessage],
) -> tuple[list[AnyMessage], list[ToolCall]]:
    """Derive progress from committed result IDs, without a separate cursor."""
    completed: set[str] = set()
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            completed.add(message.tool_call_id)
        elif isinstance(message, AIMessage):
            normalized = normalize_tool_calls(message)
            calls = (normalized or message).tool_calls
            if not calls:
                raise ValueError("exec_node: previous AIMessage has no tool_calls")
            return (
                [normalized] if normalized is not None else [],
                [call for call in calls if (call["id"] or "") not in completed],
            )
    raise ValueError("exec_node: no preceding AIMessage")


def _skipped_call_results(
    calls: Sequence[ToolCall], ctx: AvaContext, agent_id: int, start_index: int
) -> list[ToolMessage]:
    """Pair calls left unexecuted by a lifecycle halt or cancellation."""
    assert ctx.event_publisher is not None  # noqa: S101
    results: list[ToolMessage] = []
    for ordinal, call in enumerate(calls):
        message = exec_output_message(
            content="Not executed: an earlier tool call halted or cancelled this turn.",
            tool_call_id=call["id"] or "",
            created_at=datetime.now(UTC),
        )
        results.append(message)
        ctx.event_publisher.emit(
            ExecOutput(
                agent_id=agent_id,
                item_id=f"{start_index + ordinal}.0",
                content=str(message.content),
            ).model_dump_json()
        )
    return results


async def _exec_node_impl(
    state: _state.AgentState,
    runtime: Runtime[AvaContext],
    config: RunnableConfig,
) -> Command[ExecGoto]:
    """Run one call; LangGraph owns reducer application and the next snapshot."""
    replacements, calls = _remaining_exec_calls(state.messages)
    if not calls:
        # Recovery may already have paired the interrupted remainder.
        return Command[ExecGoto](
            update={"messages": state.pending_exec_notes, "pending_exec_notes": []},
            goto=AFTER_EXEC,
        )
    agent_id = agent_id_from_config(config)
    delta, compacted = await _exec_single_call(
        state, runtime.context, agent_id, calls[0], len(state.messages)
    )
    if compacted:
        delta["pending_exec_notes"] = []
        # Findings raised by earlier calls of this batch annotate the history claim wipes.
        delta["security_findings"] = Overwrite([])
        return Command[ExecGoto](update=delta, goto=AFTER_EXEC)

    results = [message for message in delta["messages"] if isinstance(message, ToolMessage)]
    notes = merge_exec_notes(
        state.pending_exec_notes,
        [message for message in delta["messages"] if not isinstance(message, ToolMessage)],
    )
    if delta["halted"]:
        results.extend(
            _skipped_call_results(
                calls[1:], runtime.context, agent_id, len(state.messages) + len(results)
            )
        )
    elif len(calls) > 1:
        delta["messages"] = replacements + results
        delta["pending_exec_notes"] = notes
        return Command[ExecGoto](update=delta, goto=EXEC)

    delta["messages"] = replacements + results + notes
    delta["pending_exec_notes"] = []
    return Command[ExecGoto](update=delta, goto=AFTER_EXEC)
