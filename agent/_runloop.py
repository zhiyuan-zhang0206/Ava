"""Agent graph run loop — the per-turn ainvoke driver.

The self-cycling LangGraph is invoked once per TURN: the claim node ends each
invocation at the turn boundary (goto END with `exit_requested=False`) and
this loop re-invokes on the same checkpointer thread, until claim requests
process exit (`exit_requested=True`: terminate / restart winner, or a lost
lifecycle CAS). Each invocation runs under its own per-turn root span
(`shared.trace.turn_span`), so one trace = one turn. Split out of
`agent/loop.py` (which keeps `main()` orchestration + the `run()` entry).
This module owns:

- `_invoke_graph_with_lifecycle_logging`: the per-turn ainvoke loop with the
  fail-loud / fatal-LLM / DB-outage branches;
- `_graph_config`: the LangGraph invoke config (thread_id, infinite recursion
  limit, trace fields);
- the fatal-LLM turn-abort handling (`_handle_fatal_llm_error`,
  `_emit_error_event`);
- the DB-outage pause-and-recover machinery (`_recover_from_db_outage`,
  `_wait_for_db_recovery`, `_probe_db_reachable`).
"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import cast

import psycopg
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg_pool import PoolTimeout

from shared.audit_events import insert_event_log_async
from shared.config import settings
from shared.config.turn_view import turn_settings
from shared.db import PG_KEEPALIVE_KWARGS
from shared.live_events import Error
from shared.log import logger

from . import _trace_checkpoint
from .graph._context import AvaContext
from .graph._llm import FatalLLMStreamError, FatalProviderError
from .graph._node_log import flush_node_exit_aggregate
from .hooks.compact import CompactionFailedError
from .startup import (
    _reconcile_claimed_inbounds_at_startup,
    _repair_dangling_tool_use_at_startup,
)
from .state import BaseAgentState
from .state_channels import (
    CIRCUIT_REASON_AUTH,
    CIRCUIT_REASON_BAD_REQUEST,
    CIRCUIT_REASON_BILLING,
    CIRCUIT_REASON_CONTEXT_OVERFLOW,
    CIRCUIT_REASON_FORBIDDEN,
    CIRCUIT_REASON_MODEL_NOT_FOUND,
    CIRCUIT_REASON_PERMANENT,
    CIRCUIT_REASON_SCHEMA,
    CircuitState,
)

# LangGraph recursion_limit defaults to 25 — far too low for this graph even
# per-turn: one invocation is one TURN, and a turn is a whole work bout (the
# claim → llm → exec cycle repeats until the agent halts), which can run for
# days. `int` has no inf; 2**31-1 keeps the rail effectively unbounded. The
# spec calls this `recursion_limit=∞` ("widest possible runaway protection
# rail").
_RECURSION_LIMIT_INF = 2**31 - 1

# DB-outage pause backoff (the laptop-sleep / network-change / private-network
# black-hole case). When the graph raises a PoolTimeout / OperationalError, the
# process backs off and re-probes the cluster DB until it answers rather than
# dying (a mid-turn death is not auto-resurrected). Exponential from 1s to a 30s
# cap: each failed probe is already bounded by connect_timeout (PG_KEEPALIVE_KWARGS,
# 5s), so a long outage settles into a probe every ~35s — fast enough to resume
# within tens of seconds of the DB coming back, without a tight reconnect loop.
_DB_RECOVERY_BACKOFF_INITIAL_S = 1.0
_DB_RECOVERY_BACKOFF_CAP_S = 30.0


async def _probe_db_reachable(url: str) -> bool:
    """Open a fresh, bounded connection to `url` and `SELECT 1` — True iff the DB
    answered. Deliberately independent of the agent pool (which may be holding
    dead conns after a sleep) and bounded by connect_timeout (PG_KEEPALIVE_KWARGS)
    so a black-holed socket fails fast instead of hanging on the OS TCP timeout.
    Any connection-level failure is a negative probe, not an error to propagate;
    CancelledError (a real SIGTERM/restart) is NOT caught and wins over the wait."""
    try:
        conn = await psycopg.AsyncConnection.connect(url, **PG_KEEPALIVE_KWARGS)
        try:
            await conn.execute("SELECT 1")
        finally:
            await conn.close()
        return True
    except (psycopg.OperationalError, PoolTimeout, OSError):
        return False


async def _wait_for_db_recovery(agent_id: int) -> None:
    """Back off and probe the cluster DB until it answers again.

    The pause half of the DB-outage branch: probe-first (a transient blip
    recovers instantly), then sleep with exponential backoff (capped) between
    failed probes. Propagates CancelledError — a SIGTERM/restart arriving during
    the wait must win, so the process can exit cleanly instead of blocking on a
    DB that may never come back on this network."""
    url = settings.data_plane.db_url
    backoff = _DB_RECOVERY_BACKOFF_INITIAL_S
    attempt = 0
    while True:
        if await _probe_db_reachable(url):
            if attempt:
                logger.info(
                    "db reachable again after {n} probe(s) — resuming",
                    event="db_recovered",
                    agent_id=agent_id,
                    n=attempt,
                )
            return
        attempt += 1
        logger.warning(
            "db still unreachable (probe {n}); backing off {b:.0f}s before retry",
            event="db_outage_wait",
            agent_id=agent_id,
            n=attempt,
            b=backoff,
        )
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, _DB_RECOVERY_BACKOFF_CAP_S)


def _emit_error_event(ctx: AvaContext, agent_id: int, content: str) -> None:
    """Best-effort single Error event to the frontend SSE channel.

    emit() is a non-blocking best-effort enqueue; the suppress guards the
    assert so a missing publisher can't mask the original exception.
    """
    with suppress(Exception):
        assert ctx.event_publisher is not None  # noqa: S101
        ctx.event_publisher.emit(Error(agent_id=agent_id, content=content).model_dump_json())


def _circuit_reason(exc: FatalProviderError) -> str:
    """Map a permanent provider rejection to its circuit-breaker reason.

    The one reason that matters for behaviour is `context_overflow` — the
    self-rescuable case the forced-compact arm keys on. Every other rejection
    (auth / billing / schema / ...) just stops heartbeat re-fires until a
    real wake succeeds; they map to a stable label for the breaker event,
    with a generic `permanent` fallback for anything unmapped."""
    if exc.context_overflow:
        return CIRCUIT_REASON_CONTEXT_OVERFLOW
    status = exc.status
    if status is None:
        return CIRCUIT_REASON_PERMANENT
    return {
        401: CIRCUIT_REASON_AUTH,
        402: CIRCUIT_REASON_BILLING,
        403: CIRCUIT_REASON_FORBIDDEN,
        404: CIRCUIT_REASON_MODEL_NOT_FOUND,
        422: CIRCUIT_REASON_SCHEMA,
        400: CIRCUIT_REASON_BAD_REQUEST,
    }.get(status, CIRCUIT_REASON_PERMANENT)


async def _handle_fatal_llm_error(
    exc: FatalLLMStreamError | FatalProviderError,
    ctx: AvaContext,
    agent_id: int,
    circuit_reader: Callable[[], Awaitable[CircuitState | None]] | None = None,
) -> dict[str, object]:
    """Fatal LLM turn abort: log + one Error event, return the fresh-run input.

    The retry cap fired, or the provider permanently rejected the request
    (402/401/403) — abort this turn but keep the agent alive. One Error event
    to the frontend, traceback into the file sink, then re-invoke with a fresh
    run: claim node idles for the next inbound so the user can see the error
    and the agent recovers on its next wake-up once the cause clears (e.g.
    balance topped up) without a manual revive.

    A `FatalProviderError` additionally OPENS the heartbeat circuit breaker:
    the next heartbeat nudge would re-fire the same doomed call (the 3962
    incident — 80 heartbeat cycles against a permanent context-overflow 400).
    The breaker state rides the fresh-run input (`circuit` channel); while it
    is open the claim consumes heartbeat check-ins without routing to the
    LLM, and for the `context_overflow` reason routes any wake into a forced
    compaction instead. It closes on the first successful LLM call (llm_node).
    """
    logger.opt(exception=True).error(
        "LLM turn aborted (retry cap exhausted or provider rejected) — "
        "agent stays alive and idles for next inbound",
        event="llm_turn_aborted",
        # FatalProviderError carries the classifier's structured triple;
        # FatalLLMStreamError does not (getattr -> None) — keeps the abort
        # event queryable by error_class / provider / status either way.
        error_class=getattr(exc, "error_class", None),
        provider=getattr(exc, "provider", None),
        status=getattr(exc, "status", None),
        context_overflow=getattr(exc, "context_overflow", None),
    )
    _emit_error_event(
        ctx,
        agent_id,
        f"{type(exc).__name__}: {exc} The turn was aborted; the agent "
        "is still alive and idling. It retries on the next message or "
        "wake-up once the underlying cause is resolved.",
    )
    # halted=True so the claim node actually waits for the next inbound
    # instead of re-entering the LLM with the same messages and hitting the
    # same fatal error in an infinite loop (agent #1581 incident — DeepSeek
    # stream stall with 616 messages).
    input_update: dict[str, object] = {"halted": True}
    if isinstance(exc, FatalProviderError):
        reason = _circuit_reason(exc)
        already_open = False
        if circuit_reader is not None:
            try:
                current = await circuit_reader()
            except Exception:
                current = None
            already_open = current is not None and current.open and current.reason == reason
        if already_open:
            # The breaker is still open for the same reason from an earlier
            # failed turn — re-opening is idempotent and re-emitting the open
            # event per failed wake is noise (QA #903 nit). Skip both; the
            # original opened_at is preserved.
            logger.info(
                "heartbeat circuit breaker already open (reason={reason}) — "
                "skipping duplicate open",
                event="circuit_breaker_open",
                agent_id=agent_id,
                reason=reason,
                status=exc.status,
            )
            return input_update
        input_update["circuit"] = CircuitState(
            open=True,
            reason=reason,
            opened_at=datetime.now(UTC).isoformat(),
        )
        logger.warning(
            "heartbeat circuit breaker OPEN — reason={reason} status={status}",
            event="circuit_breaker_open",
            agent_id=agent_id,
            reason=reason,
            status=exc.status,
        )
        # Record the breaker-open event in event_log (best-effort: a failure
        # only loses the audit row, never the breaker state itself).
        if ctx.ops_pool is not None:
            try:
                await insert_event_log_async(
                    event_type="circuit_breaker",
                    agent_id=agent_id,
                    source="system",
                    payload={
                        "action": "open",
                        "reason": reason,
                        "status": exc.status,
                        "error_class": exc.error_class,
                    },
                )
            except Exception as exc_log:
                logger.warning(
                    "failed to record circuit_breaker event: {exc!r}",
                    agent_id=agent_id,
                    exc=exc_log,
                )
    return input_update


async def _recover_from_db_outage(
    graph: CompiledStateGraph[BaseAgentState, AvaContext, BaseAgentState, BaseAgentState],
    ctx: AvaContext,
    agent_id: int,
) -> None:
    """Wait for the DB, then re-run the same startup reconciliation a fresh
    process runs: resolve 'claimed' inbound rows the interrupted turn left
    behind (committed → done, else → pending) and repair a dangling tool_use.
    ainvoke has returned, so this process is the sole writer — the
    no-concurrent-claim invariant holds. `graph.checkpointer` is the
    AsyncPostgresSaver the graph runs with; ops_pool is always set on the
    real-agent path (asserted for the type narrower).

    Reconcile itself touches the DB, so a flap between the probe succeeding
    and reconcile completing would otherwise kill the agent — the very death
    this branch exists to prevent. Keep reconcile inside the wait/retry loop.
    """
    assert ctx.ops_pool is not None  # noqa: S101
    checkpointer = cast(AsyncPostgresSaver, graph.checkpointer)  # pyright: ignore[reportUnknownMemberType]
    while True:
        await _wait_for_db_recovery(agent_id)
        try:
            await _reconcile_claimed_inbounds_at_startup(ctx.ops_pool, checkpointer, agent_id)
            await _repair_dangling_tool_use_at_startup(graph, agent_id)
            break
        except (PoolTimeout, psycopg.OperationalError):
            logger.opt(exception=True).warning(
                "db flapped during recovery reconcile — parking again",
                event="db_outage_reconcile_retry",
                agent_id=agent_id,
            )
            continue


def _graph_config(agent_id: int, tags: list[str], metadata: dict[str, object]) -> RunnableConfig:
    """LangGraph invoke config: thread_id + infinite recursion limit + the
    trace fields (run_name / metadata / tags) for backend filtering."""
    return {
        "configurable": {"thread_id": str(agent_id)},
        "recursion_limit": _RECURSION_LIMIT_INF,
        "run_name": f"ava-agent-{agent_id}",
        "metadata": metadata,
        "tags": tags,
    }


async def _invoke_graph_with_lifecycle_logging(
    graph: CompiledStateGraph[BaseAgentState, AvaContext, BaseAgentState, BaseAgentState],
    agent_id: int,
    ctx: AvaContext,
) -> None:
    """Run graph.ainvoke once per TURN in a loop, until claim requests process exit.

    One invocation = one turn: the claim node ends the invocation (goto END)
    when the turn's work is done and the next thing to do is block for
    inbound; this loop then re-invokes on the same checkpointer thread
    (thread_id), so state carries across turns and the fresh invocation's
    claim does the long wait. Each invocation is wrapped in its own per-turn
    root span (`turn_span`), so a trace = a turn and the root span exports
    when the turn ends. The loop exits only when the returned state has
    `exit_requested=True` — claim's terminate/restart winner or a lost
    lifecycle CAS — which is what "process exits normally" means now; the
    turn-boundary END leaves it False. Both flags are reset in every
    invocation's input, so a stale checkpointed True (a resurrect onto the
    same thread) cannot kill the new process.

    fail-loud guard — all ainvoke exit paths leave a traceback in the file
    sink, avoiding session teardown dropping stderr and leaving
    ~/.ava/logs/agent-{N}.log blank (agent #45 incident):

    - asyncio.CancelledError: BaseException subclass, original except
      Exception missed it. opt(exception=True) writes the stack into the
      file sink, then re-raises so asyncio.run exits on its normal path.
    - FatalLLMStreamError / FatalProviderError: the LLM consecutive-error retry
      cap fired, or the provider permanently rejected the request (402 out of
      balance / 401 bad key / 403 forbidden). NOT an exit path — log + emit one
      Error event, then re-invoke the graph with a fresh-run input: claim node
      goes back to waiting for the next inbound, the agent stays alive so the
      user can see the error and decide the next action (and so a transient
      cause like a topped-up balance recovers on the next wake-up without a
      manual revive; the cap tracker was already cleared at the raise site).
    - CompactionFailedError: compaction could not produce a usable summary
      after its retries (auto-compact hook or compact_request arm). Also NOT
      an exit path — same halted re-entry as the fatal-LLM branch, so the
      agent stays alive and the Error event explains the abort instead of the
      process dying into a non-resurrectable 'exit' (2026-08-08 audit P1-1).
    - PoolTimeout / psycopg.OperationalError: the cluster DB went unreachable
      (laptop asleep / network change / private-network black-hole). NOT an exit path
      either — historically this bubbled to `except Exception` and killed the
      process (a mid-turn death is not auto-resurrected). Instead PAUSE: back off
      + probe until the DB answers, re-run the startup reconciliation in-process
      (the same two-phase claimed-inbound + dangling-tool_use repair a fresh
      process runs — orphaned rows the interrupted turn left behind), then
      re-invoke the graph. The process stays alive the whole time, and the
      gateway view is unchanged (row stays running/idling). The lease can still
      expire mid-outage (the renewer cannot reach the DB either), but the
      restarter daemon arms a post-outage grace window before its lease-zombie
      pass runs (services/restarter/daemon.py — 2026-08-08 audit P1-2), so the
      reaper does not mistake this paused process for a corpse: it renews within
      one renew interval of the DB returning, well inside the grace. One branch
      collects all three DB death shapes: idle recheck, mid-turn node-boundary,
      LLM-envelope exhaustion.
    - Exception: same as historically; logger.exception writes traceback
      into the file sink, then re-raise.
    - SystemExit / KeyboardInterrupt: not caught (user-initiated, exit as-is).
    """
    tags = ["ava", f"agent-{agent_id}"]
    metadata: dict[str, object] = {
        "agent_id": agent_id,
        "model": turn_settings.lm.llm_model,
    }
    # Generic trace tag passthrough: caller (bench / eval / experiment / any
    # periphery) sets settings.observability.trace_tags (env AVA_TRACE_TAGS); we append to
    # the LangGraph trace config so the LangChain auto-instrumentation picks
    # them up as span tags. Empty = no-op.
    if settings.observability.trace_tags:
        tags.extend(t.strip() for t in settings.observability.trace_tags.split(",") if t.strip())

    # Wrap each invocation (= one turn) in its own root span tagged with
    # session_id + the turn counter, so all LangGraph node + Anthropic SDK
    # child spans of that turn inherit the OTel context and the trace exports
    # the moment the turn ends; session.id groups an agent's turns on the viewer.
    from shared.trace import turn_span

    # Track the input update to pass to graph.ainvoke. Starts empty; after
    # a FatalLLMStreamError / FatalProviderError we set halted=True so the claim
    # node actually waits for the next inbound instead of re-entering the LLM
    # with the same messages and hitting the same fatal error in an infinite loop
    # (agent #1581 incident — DeepSeek stream stall with 616 messages).
    input_update: dict[str, object] = {}
    checkpointer = cast(AsyncPostgresSaver, graph.checkpointer)  # pyright: ignore[reportUnknownMemberType]
    turn = 0
    while True:
        turn += 1
        try:
            with turn_span(name=f"ava-agent-{agent_id}", session_id=str(agent_id), turn=turn):
                # The input is a state UPDATE, not a full state — same semantics
                # as a node's Command(update=...): each key present is folded into
                # its channel (messages through its add_messages reducer, plain
                # fields by last-value replacement); keys absent are untouched,
                # the state itself is loaded from the thread_id's checkpoint.
                # Every invocation resets the two turn/exit flags (see the
                # docstring); beyond that we have no update to apply at process
                # start — any non-None input kicks a fresh run (None means
                # "resume pending work only" and no-ops on a completed thread).
                # After a FatalLLMStreamError / FatalProviderError we pass
                # halted=True so the claim node blocks waiting for the next
                # inbound instead of immediately re-entering the LLM with the
                # same messages that just failed — preventing the infinite
                # stall→retry→fatal→re-enter loop.
                # A full default AgentState() here would be an update resetting
                # `halted`, the built-in compact/memory sub-states, and every
                # plugin state field (ava_code cwd, ...) to defaults on each
                # respawn. On a fresh thread, unwritten channels read as the
                # schema defaults.
                # Dict-update input form is runtime-valid but absent from the input type.
                result: dict[str, object] = await graph.ainvoke(  # pyright: ignore[reportUnknownMemberType, reportAssignmentType]
                    # turn_idle is reset with the other two: a cluster rolled
                    # back from hosted to process mode replays threads a hosted
                    # run checkpointed, and a stale True must not be mistaken
                    # for this turn's answer.
                    {  # pyright: ignore[reportArgumentType, reportUnknownMemberType]
                        "turn_active": False,
                        "exit_requested": False,
                        "turn_idle": False,
                        **input_update,
                    },
                    config=_graph_config(agent_id, tags, metadata),
                    context=ctx,
                )
                await _trace_checkpoint.attach_trace_checkpoint_ref(graph, ctx, agent_id)
                # The optional N-step flush is an instance monkey-patch, not a
                # saver class method. Avoid triggering dynamic __getattr__
                # implementations on alternate saver objects.
                if "_ava_nstep_flush" in checkpointer.__dict__:
                    flush = cast(
                        Callable[[], Awaitable[None]], checkpointer.__dict__["_ava_nstep_flush"]
                    )
                    await flush()
            # exit_requested is guaranteed present: this invocation's input
            # wrote the channel. [] not .get() — a missing key is a bug.
            # Flush the aggregated node_exit buffer on EVERY invocation return:
            # a terminate/restart exit (goto END with exit_requested=True) never
            # reaches another claim enter, so the runloop is the only flush
            # point that covers all exit paths (review #654-1). The claim-enter
            # flush in claim_node remains as the cover for direct graph
            # invocations that bypass this loop (tests/evals).
            flush_node_exit_aggregate(agent_id)
            if result["exit_requested"]:
                return
            # Turn over — re-invoke on the same thread; the fresh invocation's
            # claim blocks for the next inbound.
            input_update = {}
            continue
        except (FatalLLMStreamError, FatalProviderError) as exc:
            # Retry cap fired, or the provider permanently rejected the request
            # (402/401/403) — abort this turn but keep the agent alive. The
            # fresh-run input halts the claim node until the next inbound;
            # for a FatalProviderError it also opens the heartbeat circuit
            # breaker (see _handle_fatal_llm_error).
            async def _read_current_circuit() -> CircuitState | None:
                snapshot = await graph.aget_state(_graph_config(agent_id, [], {}))
                current = snapshot.values.get("circuit")
                return current if isinstance(current, CircuitState) else None

            input_update = await _handle_fatal_llm_error(
                exc, ctx, agent_id, circuit_reader=_read_current_circuit
            )
            continue
        except CompactionFailedError as exc:
            # Compaction could not produce a usable summary (model ignoring the
            # template, provider failing) — NOT an exit path either. Same shape
            # as the fatal-LLM abort: one Error event, then re-invoke with a
            # halted fresh run so the claim node idles for the next inbound.
            # Killing the process here would stamp a non-resurrectable 'exit'
            # and crash again on every subsequent message while the cause
            # persists (2026-08-08 audit, P1-1).
            logger.warning(
                "compaction failed — aborting turn, agent stays alive and idles "
                "for the next inbound",
                event="compact_turn_aborted",
                agent_id=agent_id,
            )
            _emit_error_event(
                ctx,
                agent_id,
                f"CompactionFailedError: {exc} The turn was aborted; the agent "
                "is still alive and idling. It retries compaction on the next "
                "message once the underlying cause is resolved.",
            )
            input_update = {"halted": True}
            continue
        except asyncio.CancelledError:
            logger.opt(exception=True).info(
                "graph.ainvoke received CancelledError — agent process exiting (hibernate/restart/terminate)"
            )
            raise
        except (PoolTimeout, psycopg.OperationalError) as exc:
            # DB unreachable (laptop asleep / network change / private-network
            # black-hole): a borrowed-conn PoolTimeout, or a mid-flight
            # OperationalError on a half-dead socket. Pause instead of dying —
            # the process must outlive the outage so no work is lost and the
            # reaper never mistakes it for a corpse. Log loudly, tell the UI once,
            # then wait for the DB.
            logger.opt(exception=True).warning(
                "db unreachable — pausing until it recovers (agent stays alive)",
                event="db_outage_pause",
                agent_id=agent_id,
            )
            _emit_error_event(
                ctx,
                agent_id,
                f"{type(exc).__name__}: {exc} The database is unreachable "
                "(likely a sleep / network change); the agent is paused and "
                "will resume automatically once the connection recovers.",
            )
            await _recover_from_db_outage(graph, ctx, agent_id)
            # Fresh run: claim node re-claims any pending work (its 30s SELECT
            # recheck also picks up whatever queued during the wait) or idles.
            # Not halted=True — that is the fatal-LLM guard against re-entering a
            # failing turn; a DB outage leaves the turn resumable.
            input_update = {}
            continue
        except Exception as exc:
            logger.exception("graph.ainvoke crashed — agent process dying")
            # Emit one Error event to the frontend SSE — only when langgraph retry
            # is exhausted + the exception bubbles all the way here, distinct from
            # llm_node's internal "publish on every attempt". Cancelled is handled
            # by _llm_node_impl itself (the previous CancelledError branch doesn't
            # reach here either). SystemExit / KeyboardInterrupt are not Exception
            # subclasses, so they don't enter this branch naturally.
            _emit_error_event(ctx, agent_id, f"{type(exc).__name__}: {exc}")
            raise
