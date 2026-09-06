"""exec node: agent-written code runs in a disposable subprocess.

Each execute_code call runs in a fresh child process (`agent/exec_child.py`),
so a stuck native call is SIGKILLable without touching the agent process
(issue #184). All paths return Command(goto="after_exec") — under cycling
topology after_exec always routes to claim, which decides whether to wait or
continue multi-step based on pending inbound + state.halted.

Core mechanisms:
  - Subprocess backend (`agent/graph/_exec_subprocess.py`): the parent spawns
    one `python -I -X utf8 -m agent.exec_child` per exec, polls every 50ms, streams
    output through the chunk pipeline. POSIX cancel/timeout sends a signal then
    closes the process group after a grace period; Windows immediately closes
    the Job Object. Natural root exit also closes the domain, so an `os._exit`
    cannot strand an ordinary descendant holding stdout. Cancellation returns
    only after the direct child is reaped and the pipe reader gets its bounded
    join. The child rebuilds the state snapshot from the request envelope; the
    plugin state-update delta and drained security findings ride the result
    envelope back and are validated here.
  - Halt signal uses exception type rather than exit code: agent code raising
    `_LifecycleExit` (AgentTermination / AgentRestart / _SystemHalt) → captured
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

State type hint key design (`state: _state.AgentState` + `from __future__ import
annotations`): LangGraph 1.x narrows the state schema by the node function's
first parameter type hint — `from agent.state import AgentState` statically
captures the alias (at module load time = BaseAgentState); after build_graph,
`agent.state.AgentState` is rebound to the dynamic subclass with plugin
fields, but this module's `AgentState` name is already snapshotted, and
LangGraph sees BaseAgentState with only 2 channels and drops all plugin
fields. Changed to `from agent import state as _state` + use
`_state.AgentState` to do module-attribute dynamic lookup; combined with
future annotations to defer annotation resolution to get_type_hints()
evaluation time, by which build_agent_state has rebound and we get the
dynamic AgentState. Pyright statically still sees `_state.AgentState` as
`BaseAgentState` (`agent/state.py` end has `AgentState = BaseAgentState`
alias), so `state.messages` / `state.halted` still type-check; plugin fields
are accessed dynamically (consistent with existing convention).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent import state as _state
from agent.graph._attach_drain import build_attach_message
from agent.graph._attach_merge import merge_attachments
from agent.graph._exec_notes import merge_exec_notes
from agent.messages import exec_output_message
from agent.state import AttachState, _validate_plugin_state_keys
from ava.security import SecurityFindingEntry, take_findings
from shared.config import settings
from shared.config.turn_view import current_agent_config_pins
from shared.exit_codes import IDLE_EXIT_CODE, SYSTEM_HALT_EXIT_CODE
from shared.lifecycle import (
    AgentImpersonation,
    AgentRestart,
    AgentTermination,
    _SystemHalt,
)
from shared.live_events import (
    Cancelled,
    ExecOutput,
    ExecStart,
)
from shared.log import logger
from shared.plugin_config_view import current_agent_plugin_pins

from ._agent_traceback import format_full_traceback
from ._context import AvaContext, agent_id_from_config
from ._exec_output import wrap_code_output
from ._exec_protocol import ResultPayload
from ._exec_result import (
    _ExecCancelled,
    _ExecCrashed,
    _ExecDone,
    _ExecLifecycle,
    _ExecResult,
    _ExecTimedOut,
)
from ._exec_stream import ExecOutputChunkPublisher
from ._exec_subprocess import _run_in_subprocess
from ._interrupt import subscribe_interrupt
from ._node_log import node_lifecycle
from ._nodes import AFTER_EXEC
from ._tool_calls import merge_multiple_execute_code_tool_calls

# exec_node always goto AFTER_EXEC (under the cycling topology, halted is routed by after_exec)
ExecGoto = Literal["after_exec"]

# The `_ExecResult` sum type — 5 mutually exclusive variants + a shared output
# field — lives in `_exec_result.py` (moved there so the exec-subprocess
# machinery can construct the same type without importing this module, which
# would close an import cycle). Re-exported here: exec_node's match dispatch
# and existing tests keep importing from agent.graph._exec.
#
# Lifecycle priority is implemented at the construction site
# (`_construct_exec_result`): if a lifecycle exc exists, `_ExecLifecycle` is
# constructed directly, skipping the cancelled/timed_out branches — the
# "lifecycle always wins" race decision moved from dispatch site to
# construction site; exec_node match no longer has to consider the race.


@dataclass(frozen=True)
class _ExecCall:
    """Resolved execute_code invocation; `state_messages_update` carries the
    merged tool-call message (when the multi-call merge fired)."""

    code: str
    tool_call_id: str
    state_messages_update: list[AnyMessage]


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
    ):
        return await _exec_node_impl(state, runtime, config)


def _resolve_exec_call(state: _state.AgentState, agent_id: int) -> _ExecCall | Command[ExecGoto]:
    """Extract the execute_code tool call from the previous AIMessage.

    Single-tool wire format: the model must call Python via tool_calls[0]
    (bare content form is deprecated); tool_call_id pairs with the ToolMessage
    sent back. Returns an error Command (unknown-tool ToolMessage so the next
    round can retry), raises ValueError on the no-tool_calls path, else the
    resolved `_ExecCall`.
    """
    last = state.messages[-1]
    fixed_last = (
        merge_multiple_execute_code_tool_calls(
            last,
            agent_id=agent_id,
            location="exec_node",
        )
        if isinstance(last, AIMessage)
        else None
    )
    state_messages_update: list[AnyMessage] = []
    if fixed_last is not None:
        last = fixed_last
        state_messages_update.append(fixed_last)
    tool_calls = getattr(last, "tool_calls", None)
    if not tool_calls:
        raise ValueError(
            f"exec_node: previous AIMessage has no tool_calls (type={type(last).__name__}). "
            f"Model must call the execute_code tool; should not reach this path"
        )
    # Anthropic-compat providers (e.g. DeepSeek) don't grammar-constrain the
    # tool name to the registered set, so the model can hallucinate calling SDK
    # functions like `ava.files.edit` as tools. Surface as ToolMessage so the
    # next round can retry, instead of letting `["code"]` KeyError-crash exec.
    first = tool_calls[0]
    if first["name"] != "execute_code" or "code" not in first["args"]:
        err = exec_output_message(
            content=f"unknown tool {first['name']!r}; only `execute_code(code: str)` is registered",
            tool_call_id=first["id"],
            exit_code=0,
            created_at=datetime.now(UTC),
        )
        state_messages_update.append(err)
        return Command[ExecGoto](
            update={"messages": state_messages_update, "halted": False},
            goto=AFTER_EXEC,
        )
    return _ExecCall(
        code=first["args"]["code"],
        tool_call_id=first["id"],
        state_messages_update=state_messages_update,
    )


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
    list[SecurityFindingEntry],
    list[dict[str, Any]] | None,
]:
    """Run the agent's code in one disposable child process.

    The parent does not touch the ava.state slot — the child rebuilds the
    snapshot from the request envelope (`agent/exec_child.py`), and the
    plugin's state-update delta + drained security findings ride the result
    envelope back. Validation is fail-fast on a tampered slot, and the child
    receives the bound turn's config maps so its SDK calls
    resolve the same settings. Returns
    (result, plugin_state_update, exec_ms, findings, attachments)."""
    config_overlay = {**(current_agent_config_pins() or {}), **current_agent_plugin_pins()}
    exec_started = time.monotonic()
    async with subscribe_interrupt(ctx.ops_pool, agent_id) as cancel_event:
        outcome = await _exec_with_node_shield(
            _run_in_subprocess(
                code,
                int(agent_id),
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
    if payload is not None and payload.state_update_error is not None:
        # The child reported a tampered slot (agent set ava.state_update to a
        # non-dict) — raise the TypeError the child would have raised.
        raise TypeError(payload.state_update_error)
    delta = payload.state_update if payload is not None else None
    plugin_state_update = _validate_plugin_state_keys(dict(delta), state.__class__) if delta else {}
    findings = (
        [SecurityFindingEntry.model_validate(f) for f in payload.findings]
        if payload is not None and payload.findings
        else []
    )
    attachments = payload.attachments if payload is not None else None
    return result, plugin_state_update, exec_ms, findings, attachments


def _dispatch_exec_result(
    result: _ExecResult,
    ctx: AvaContext,
    agent_id: int,
    *,
    referenced_messages: Sequence[AnyMessage] = (),
) -> tuple[bool, str, int]:
    """Map the `_ExecResult` sum type to (halted, result_text, exit_code_for_msg).

    Lifecycle priority (lifecycle always wins the cancel/timeout race) is
    implemented at the construction site in `_construct_exec_result` (`_exec_result.py`); the match directly
    consumes the sum type. Exhaustiveness: pyright strict + match narrowing make
    a forgotten variant a static error (replaces the hand-written fallthrough).
    """
    # Present on every variant (see the sum-type definitions): when the
    # accumulation budget dropped the middle mid-run, the envelope needs it to
    # report the true produced length and to stop calling the archive complete.
    stream_cap = result.stream_cap
    match result:
        case _ExecLifecycle(output=output, exc=_SystemHalt()):
            # ava.self.compact already INSERTed compact_summary inbound; append
            # "[system halt]" at the end (agent's real output comes first).
            halted = True
            extra = "[system halt] You just called ava.self.compact; your context has been compacted and you will continue as the same agent\n"
            output = (output if not output or output.endswith("\n") else output + "\n") + extra
            result_text = wrap_code_output(
                output, stream_cap=stream_cap, referenced_messages=referenced_messages
            )
            exit_code_for_msg = SYSTEM_HALT_EXIT_CODE
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
            exit_code_for_msg = IDLE_EXIT_CODE
            logger.info("[{label}] {body}", label="exec", body=result_text)
            logger.info(
                "[{label}] {body}",
                label="halt",
                body=f"lifecycle {type(exc).__name__}",
            )
        case _ExecLifecycle(exc=other_exc):
            # Exhaustive fallthrough: future _LifecycleExit subclass not handled
            # in the two cases above falls here and raises — safer than silently
            # taking the "ordinary exception" halted=False path. Implements
            # CLAUDE.md "enum dispatch must be exhaustive".
            raise TypeError(
                f"Unrecognized _LifecycleExit subclass: {type(other_exc).__name__!r} — "
                f"dispatch ladder missed update"
            )
        case _ExecCancelled(output=output):
            halted = True
            result_text = wrap_code_output(
                output,
                cancelled=True,
                stream_cap=stream_cap,
                referenced_messages=referenced_messages,
            )
            exit_code_for_msg = -1
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
            exit_code_for_msg = -1
            logger.info(
                "[{label}] {body}", label="exec-timeout", body=result_text, event="exec_timeout"
            )
        case _ExecCrashed(output=output, exc=exc, full_traceback=child_traceback):
            # Ordinary exception: `output` carries the agent-facing (filtered)
            # traceback; the log gets the full unfiltered chain (framework/SDK
            # bugs invisible in the agent view stay diagnosable). INFO +
            # event=exec_failed — trial-and-error is the normal dev loop, not
            # an operator alert (metrics still aggregate by event name).
            # The child ships its formatted traceback in the envelope
            # (`child_traceback`); parent-side construction failures (spawn
            # error, unserializable state) format from `exc`.
            halted = False
            result_text = wrap_code_output(
                output, stream_cap=stream_cap, referenced_messages=referenced_messages
            )
            exit_code_for_msg = 0
            logger.info(
                "[{label}] {body}\n[full traceback]\n{full_traceback}",
                label="exec-failed",
                body=result_text,
                full_traceback=child_traceback or format_full_traceback(exc),
                event="exec_failed",
                exc_type=type(exc).__name__,
            )
        case _ExecDone(output=output):
            halted = False
            result_text = wrap_code_output(
                output, stream_cap=stream_cap, referenced_messages=referenced_messages
            )
            exit_code_for_msg = 0
            logger.info("[{label}] {body}", label="exec", body=result_text)
    return halted, result_text, exit_code_for_msg


def _attach_model(ctx: AvaContext) -> str:
    """The model name attachments are packed for (media capability gate).

    Same resolution as the claim fallback drain (`_attach_drain.py`): the live
    LLM's model name, else the configured turn model. ``ctx.llm`` can be None
    (tests / container edge), hence the getattr fallback.
    """
    from shared.config.turn_view import turn_settings

    return getattr(ctx.llm, "model_name", None) or turn_settings.lm.llm_model


async def _exec_node_impl(
    state: _state.AgentState,
    runtime: Runtime[AvaContext],
    config: RunnableConfig,
) -> Command[ExecGoto]:
    """Body of `exec_node`, extracted so `node_lifecycle` can wrap an enter/exit event."""
    ctx = runtime.context
    assert ctx.event_publisher is not None, (  # noqa: S101
        "_exec_node_impl requires ctx.event_publisher"
    )
    agent_id = agent_id_from_config(config)

    # exec_msg_idx = position the ToolMessage(exec_output) will land at — after
    # exec_node returns Command, LangGraph appends, so `len(state.messages)` is
    # that position. Computed before ExecStart so the frontend creates the
    # code_output placeholder as soon as exec begins.
    exec_msg_idx = len(state.messages)
    ctx.event_publisher.emit(
        ExecStart(agent_id=agent_id, item_id=f"{exec_msg_idx}.0").model_dump_json()
    )

    resolved = _resolve_exec_call(state, agent_id)
    if isinstance(resolved, Command):
        # Unknown-tool path: the error ToolMessage is already in the update list.
        return resolved
    state_messages_update = resolved.state_messages_update

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
        envelope_findings,
        envelope_attachments,
    ) = await _run_agent_code(state, ctx, agent_id, resolved.code, chunk_publisher)
    halted, result_text, exit_code_for_msg = _dispatch_exec_result(
        result, ctx, agent_id, referenced_messages=state.messages
    )

    # Pop the plugin's messages delta out of the state update — merged below
    # after the ToolMessage instead of riding the dict **spread (which would
    # clobber the ToolMessage). Popped + drained unconditionally so a compact
    # turn (REMOVE_ALL'd by claim) leaks nothing to later turns.
    plugin_messages = plugin_state_update.pop("messages", None)
    # Findings drained from this process's own buffer (scans outside the exec
    # turn — inbound injection checks — run in the parent) first, then the
    # child-drained ones from the result envelope.
    findings = take_findings() + envelope_findings

    # Compact path (_SystemHalt): write nothing back — claim REMOVE_ALLs the
    # whole history this turn, so ToolMessage/notes would be wiped anyway.
    compact_halt = isinstance(result, _ExecLifecycle) and isinstance(result.exc, _SystemHalt)
    if not compact_halt:
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
            tool_call_id=resolved.tool_call_id,
            exit_code=exit_code_for_msg,
            cancelled=isinstance(result, _ExecCancelled),
            timed_out=isinstance(result, _ExecTimedOut),
            exec_ms=exec_ms,
            created_at=datetime.now(UTC),
        )
        state_messages_update.append(msg)

        # In-memory system-note injection (user ruling 2026-08-11): security
        # findings + plugin context notes merge into this exec's delta after
        # the ToolMessage (ordering rationale: _exec_notes.py).
        state_messages_update = merge_exec_notes(state_messages_update, plugin_messages, findings)
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
    return Command[ExecGoto](update=update, goto=AFTER_EXEC)
