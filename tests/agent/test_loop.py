"""Unit tests for helper functions in agent/loop.py for startup/shutdown.

Running `main()` end-to-end requires LangGraph + Redis + LLM mocks which are too heavy; only test the three extracted helpers:

- `claim_agent_row`: DB operation fail-fast behavior; `_notify_exit`: notify gateway finalize
  (race-safe process start / terminate path correctly reflects in agents_meta table)
- `_invoke_graph_with_lifecycle_logging`: on ainvoke cancel/exception, write traceback
  to file sink then re-raise — regression guard for agent #45 incident
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from psycopg_pool import PoolTimeout

from agent._runloop import (
    _invoke_graph_with_lifecycle_logging,
    _probe_db_reachable,
    _wait_for_db_recovery,
)
from agent._starting import claim_agent_row
from agent.graph._context import AvaContext
from agent.graph._llm import FatalLLMStreamError, FatalProviderError
from agent.hooks.compact import CompactionFailedError
from agent.loop import (
    _exit_reason,
    _install_lifecycle_signal_handlers,
    _mcp_socket_path,
    _MCPDaemon,
    _notify_exit,
    _route_process_end_notify,
    run,
)
from shared import boot_timing
from tests.conftest import spawn_agent


def _agent_status(db: psycopg.Connection, agent_id: int) -> tuple[str, int | None]:
    with db.cursor() as cur:
        cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0], row[1]


class TestClaimAgentRow:
    def test_unclaimed_idling_to_running_with_pid(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Normal path: unclaimed idling → running with ownership columns filled."""
        agent_id = spawn_agent()
        claim_agent_row(agent_id)
        status, pid = _agent_status(db_conn, agent_id)
        assert status == "running"
        assert pid == os.getpid()

    def test_nonexistent_id_raises(self, db_conn: psycopg.Connection) -> None:
        # PR-0 (multi-machine) added agents_meta SELECT before UPDATE to get machine validation;
        # nonexistent id raises "agents_meta row does not exist" at SELECT stage, never reaching
        # the failed idling-claim UPDATE path.
        with pytest.raises(RuntimeError, match="agents_meta row does not exist"):
            claim_agent_row(9999)

    def test_second_claim_raises(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A claimed row cannot be claimed again by a second process."""
        agent_id = spawn_agent()
        claim_agent_row(agent_id)
        with pytest.raises(RuntimeError, match="rowcount=0"):
            claim_agent_row(agent_id)

    def test_terminated_row_raises(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A terminated row must go through resurrection before it can be claimed."""
        agent_id = spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        with pytest.raises(RuntimeError, match="rowcount=0"):
            claim_agent_row(agent_id)


class TestNotifyExit:
    """`_notify_exit` only notifies the gateway (`POST /api/agents/{id}/exited`);
    the guarded status flip + page close now happen server-side (covered by
    tests/gateway/test_agents_internals.py:TestExitedEndpoint). The agent side
    just fires the call and swallows failures so a dying process never raises
    out of its finally block — the gateway's zombie-reaping is the backstop."""

    def test_calls_gateway_exited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called: list[int] = []
        monkeypatch.setattr("ava._gateway_client.exited", called.append)
        _notify_exit(42)
        assert called == [42]

    def test_swallows_gateway_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Gateway unreachable during a silent death → logged, not raised."""

        def _boom(_aid: int) -> None:
            raise RuntimeError("gateway down")

        monkeypatch.setattr("ava._gateway_client.exited", _boom)
        _notify_exit(42)  # must not raise


def _fake_ctx() -> AvaContext:
    return AvaContext(
        ops_pool=MagicMock(),
        llm=MagicMock(),
        event_publisher=MagicMock(),
    )


class TestInvokeGraphLifecycleLogging:
    """fail-loud safety net — for any exit path of ainvoke, write traceback to file sink.
    Prevent regression of agent #45 incident (process silent death + ~/.ava/logs/agent-{N}.log empty).
    """

    async def test_cancelled_logs_info_and_reraises(self, loguru_records) -> None:
        """ainvoke raises CancelledError → opt(exception=True).info leaves traceback + raise.

        CancelledError on this path is a NORMAL exit (restart / terminate cancel
        the in-flight ainvoke), so it must NOT be logged as ERROR —
        the crash/ERROR path is reserved for unexpected failures.
        """
        graph = MagicMock()
        graph.ainvoke = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await _invoke_graph_with_lifecycle_logging(graph, agent_id=42, ctx=_fake_ctx())

        # INFO level (not ERROR) with characteristic string — normal exit, not an error
        info_records = [r for r in loguru_records if r["level"].name == "INFO"]  # pyright: ignore[reportUnknownMemberType]
        assert any("graph.ainvoke received CancelledError" in r["message"] for r in info_records), (
            f"missing CancelledError log; got {[r['message'] for r in loguru_records]}"
        )
        assert not any(
            "graph.ainvoke received CancelledError" in r["message"]
            for r in loguru_records
            if r["level"].name == "ERROR"  # pyright: ignore[reportUnknownMemberType]
        ), "CancelledError must not be logged as ERROR (normal exit path)"
        # opt(exception=True) should attach the exception object to record["exception"]
        # — this is key: verify traceback really captured (unlike stdlib exc_info=True
        # that is dropped in loguru)
        assert any(r["exception"] is not None for r in info_records), (
            "logger.opt(exception=True) should attach exception object — cannot be dropped like exc_info=True"
        )

    async def test_generic_exception_logs_and_reraises(self, loguru_records) -> None:
        """ainvoke raises ordinary Exception → logger.exception leaves traceback + raise."""
        graph = MagicMock()
        graph.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            await _invoke_graph_with_lifecycle_logging(graph, agent_id=42, ctx=_fake_ctx())

        error_records = [r for r in loguru_records if r["level"].name == "ERROR"]  # pyright: ignore[reportUnknownMemberType]
        assert any("graph.ainvoke crashed" in r["message"] for r in error_records)
        assert any(r["exception"] is not None for r in error_records)

    async def test_generic_exception_publishes_error_event(self) -> None:
        """ainvoke raises Exception → wrapper should emit Error event to frontend SSE,
        so frontend can actually receive it. This is the entry when all retries exhausted — llm_node internal except does not emit
        to avoid sending "error" to frontend on every failed attempt, but when retries exhausted and reach the wrapper,
        must emit once otherwise frontend only sees process exit silently."""
        graph = MagicMock()
        graph.ainvoke = AsyncMock(side_effect=RuntimeError("boom-after-retries"))
        pub = MagicMock()
        ctx = AvaContext(
            ops_pool=MagicMock(),
            llm=MagicMock(),
            event_publisher=pub,
        )

        with pytest.raises(RuntimeError, match="boom-after-retries"):
            await _invoke_graph_with_lifecycle_logging(graph, agent_id=42, ctx=ctx)

        error_emits = [call for call in pub.emit.call_args_list if '"role":"error"' in call.args[0]]
        assert len(error_emits) == 1, (
            f"expected exactly 1 Error emit on Exception path, got {len(error_emits)}"
        )
        payload = error_emits[0].args[0]
        assert "RuntimeError" in payload
        assert "boom-after-retries" in payload
        assert '"agent_id":42' in payload

    async def test_fatal_llm_stream_error_aborts_turn_and_stays_alive(self, loguru_records) -> None:
        """FatalLLMStreamError (LLM retry cap fired) is NOT an exit path: the
        wrapper emits exactly one Error event, logs the traceback, then
        re-invokes the graph with a fresh run — the agent stays alive idling
        instead of dying to status='terminated' (which the restarter never
        respawns)."""
        graph = MagicMock()
        graph.ainvoke = AsyncMock(
            side_effect=[FatalLLMStreamError("retry cap (3) exhausted"), {"exit_requested": True}]
        )
        pub = MagicMock()
        ctx = AvaContext(
            ops_pool=MagicMock(),
            llm=MagicMock(),
            event_publisher=pub,
        )

        # Must return normally (second ainvoke completes) — no raise.
        await _invoke_graph_with_lifecycle_logging(graph, agent_id=42, ctx=ctx)

        assert graph.ainvoke.call_count == 2, (
            "wrapper must re-invoke the graph (fresh run -> idle) after aborting the turn"
        )
        error_emits = [call for call in pub.emit.call_args_list if '"role":"error"' in call.args[0]]
        assert len(error_emits) == 1, (
            f"expected exactly 1 Error emit on Fatal path, got {len(error_emits)}"
        )
        payload = error_emits[0].args[0]
        assert "FatalLLMStreamError" in payload
        assert "still alive" in payload
        error_records = [r for r in loguru_records if r["level"].name == "ERROR"]  # pyright: ignore[reportUnknownMemberType]
        assert any("retry cap exhausted" in r["message"] for r in error_records)
        assert any(r["exception"] is not None for r in error_records)

    async def test_fatal_provider_error_aborts_turn_and_stays_alive(self, loguru_records) -> None:
        """FatalProviderError (provider permanently rejected, e.g. 402 out of
        balance) is NOT an exit path either: same as the retry-cap case, the
        wrapper emits one Error event, logs, then re-invokes the graph so the
        agent idles instead of dying to status='terminated'. This is what lets a
        whole fleet recover on its own once the balance is topped up, rather than
        every agent needing a manual revive."""
        graph = MagicMock()
        graph.ainvoke = AsyncMock(
            side_effect=[
                FatalProviderError("provider permanently rejected (HTTP 402)"),
                {"exit_requested": True},
            ]
        )
        pub = MagicMock()
        ctx = AvaContext(
            ops_pool=MagicMock(),
            llm=MagicMock(),
            event_publisher=pub,
        )

        # Must return normally (second ainvoke completes) — no raise, no exit.
        await _invoke_graph_with_lifecycle_logging(graph, agent_id=42, ctx=ctx)

        assert graph.ainvoke.call_count == 2, (
            "wrapper must re-invoke the graph (fresh run -> idle) after a provider rejection"
        )
        error_emits = [call for call in pub.emit.call_args_list if '"role":"error"' in call.args[0]]
        assert len(error_emits) == 1, (
            f"expected exactly 1 Error emit on FatalProviderError path, got {len(error_emits)}"
        )
        payload = error_emits[0].args[0]
        assert "FatalProviderError" in payload
        assert "still alive" in payload
        error_records = [r for r in loguru_records if r["level"].name == "ERROR"]  # pyright: ignore[reportUnknownMemberType]
        assert any("provider rejected" in r["message"] for r in error_records)
        assert any(r["exception"] is not None for r in error_records)

    async def test_compaction_failed_aborts_turn_and_stays_alive(self, loguru_records) -> None:
        """CompactionFailedError (no usable summary after retries) is NOT an exit
        path either: same halted re-entry as the fatal-LLM branch — one Error
        event, then re-invoke with a fresh run so the agent idles. Regression
        lock for the 2026-08-08 audit P1-1: a compact failure used to bubble to
        `except Exception` and kill the process into a non-resurrectable 'exit'
        (one crash per incoming message while the cause persisted)."""
        graph = MagicMock()
        graph.ainvoke = AsyncMock(
            side_effect=[
                CompactionFailedError("no usable summary across 3 attempts"),
                {"exit_requested": True},
            ]
        )
        pub = MagicMock()
        ctx = AvaContext(
            ops_pool=MagicMock(),
            llm=MagicMock(),
            event_publisher=pub,
        )

        # Must return normally (second ainvoke completes) — no raise, no exit.
        await _invoke_graph_with_lifecycle_logging(graph, agent_id=42, ctx=ctx)

        assert graph.ainvoke.call_count == 2, (
            "wrapper must re-invoke the graph (fresh run -> idle) after a compaction failure"
        )
        error_emits = [call for call in pub.emit.call_args_list if '"role":"error"' in call.args[0]]
        assert len(error_emits) == 1, (
            f"expected exactly 1 Error emit on CompactionFailedError path, got {len(error_emits)}"
        )
        payload = error_emits[0].args[0]
        assert "CompactionFailedError" in payload
        assert "still alive" in payload
        warn_records = [r for r in loguru_records if r["level"].name == "WARNING"]  # pyright: ignore[reportUnknownMemberType]
        assert any("compaction failed" in r["message"] for r in warn_records)

    @pytest.mark.parametrize(
        "outage_exc",
        [
            psycopg.OperationalError("server closed the connection unexpectedly"),
            PoolTimeout("couldn't get a connection after 30.00 sec"),
        ],
        ids=["operational_error", "pool_timeout"],
    )
    async def test_db_outage_pauses_reconciles_and_resumes(
        self, monkeypatch: pytest.MonkeyPatch, loguru_records, outage_exc: Exception
    ) -> None:
        """A PoolTimeout / OperationalError from ainvoke (the laptop-sleep DB
        outage) is NOT an exit path: the wrapper emits one Error event, waits for
        the DB, re-runs the startup reconciliation (claimed-inbound + dangling
        tool_use) in-process, then re-invokes the graph. The agent pauses and
        resumes instead of dying — a mid-turn death is not auto-resurrected."""
        graph = MagicMock()
        graph.ainvoke = AsyncMock(side_effect=[outage_exc, {"exit_requested": True}])
        wait = AsyncMock(name="_wait_for_db_recovery")
        reconcile = AsyncMock(name="_reconcile_claimed_inbounds_at_startup")
        repair = AsyncMock(name="_repair_dangling_tool_use_at_startup")
        monkeypatch.setattr("agent._runloop._wait_for_db_recovery", wait)
        monkeypatch.setattr("agent._runloop._reconcile_claimed_inbounds_at_startup", reconcile)
        monkeypatch.setattr("agent._runloop._repair_dangling_tool_use_at_startup", repair)
        pub = MagicMock()
        ctx = AvaContext(ops_pool=MagicMock(), llm=MagicMock(), event_publisher=pub)

        # Must return normally (second ainvoke completes) — no raise, no exit.
        await _invoke_graph_with_lifecycle_logging(graph, agent_id=42, ctx=ctx)

        assert graph.ainvoke.call_count == 2, (
            "wrapper must re-invoke the graph after the DB recovers, not die"
        )
        wait.assert_awaited_once()
        # Reconciliation re-runs in-process before the re-invoke: the same
        # two-phase claim/repair a fresh process runs.
        reconcile.assert_awaited_once()
        repair.assert_awaited_once()
        # The re-invoke is a fresh run (not halted=True — that is the fatal-LLM
        # guard against re-entering a failing turn; a DB outage leaves it
        # resumable), carrying only the per-invoke turn/exit/idle flag resets.
        assert graph.ainvoke.await_args.args[0] == {
            "turn_active": False,
            "exit_requested": False,
            "turn_idle": False,
            "restart_requested": False,
        }
        # Exactly one Error event, naming the recoverable-pause semantics.
        error_emits = [c for c in pub.emit.call_args_list if '"role":"error"' in c.args[0]]
        assert len(error_emits) == 1, (
            f"expected exactly 1 Error emit on DB-outage path, got {len(error_emits)}"
        )
        assert "paused" in error_emits[0].args[0]
        assert '"agent_id":42' in error_emits[0].args[0]
        # Loud WARNING with a traceback for ops (not a silent pause).
        warn_records = [r for r in loguru_records if r["level"].name == "WARNING"]  # pyright: ignore[reportUnknownMemberType]
        assert any("db unreachable" in r["message"] for r in warn_records)
        assert any(r["exception"] is not None for r in warn_records)

    async def test_db_outage_survives_repeated_flap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two outages back to back (the DB flaps): each is paused and reconciled;
        the wrapper never dies and only returns on the eventual clean run."""
        graph = MagicMock()
        graph.ainvoke = AsyncMock(
            side_effect=[
                psycopg.OperationalError("gone"),
                psycopg.OperationalError("gone again"),
                {"exit_requested": True},
            ]
        )
        monkeypatch.setattr("agent._runloop._wait_for_db_recovery", AsyncMock())
        reconcile = AsyncMock()
        monkeypatch.setattr("agent._runloop._reconcile_claimed_inbounds_at_startup", reconcile)
        monkeypatch.setattr("agent._runloop._repair_dangling_tool_use_at_startup", AsyncMock())
        ctx = AvaContext(ops_pool=MagicMock(), llm=MagicMock(), event_publisher=MagicMock())

        await _invoke_graph_with_lifecycle_logging(graph, agent_id=42, ctx=ctx)

        assert graph.ainvoke.call_count == 3
        assert reconcile.await_count == 2  # one reconcile per recovered outage

    async def test_db_flap_during_reconcile_retries_not_die(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A DB flap DURING recovery reconcile (the probe passed, then the DB
        dropped again before reconcile finished) must NOT kill the agent — the
        reconcile is inside the wait/retry loop, so another outage there just
        parks again. This is the death the branch exists to prevent, so the
        reconcile step itself must not be the one exit that reintroduces it."""
        graph = MagicMock()
        graph.ainvoke = AsyncMock(
            side_effect=[psycopg.OperationalError("outage"), {"exit_requested": True}]
        )
        wait = AsyncMock()
        monkeypatch.setattr("agent._runloop._wait_for_db_recovery", wait)
        # reconcile hits another outage on the first try, then succeeds.
        reconcile = AsyncMock(side_effect=[psycopg.OperationalError("flap"), None])
        monkeypatch.setattr("agent._runloop._reconcile_claimed_inbounds_at_startup", reconcile)
        repair = AsyncMock()
        monkeypatch.setattr("agent._runloop._repair_dangling_tool_use_at_startup", repair)
        ctx = AvaContext(ops_pool=MagicMock(), llm=MagicMock(), event_publisher=MagicMock())

        await _invoke_graph_with_lifecycle_logging(graph, agent_id=42, ctx=ctx)

        assert graph.ainvoke.call_count == 2, "never died; resumed after the reconcile flap"
        assert wait.await_count == 2, "flap re-parked: wait once per reconcile attempt"
        assert reconcile.await_count == 2, "reconcile retried after the flap"
        assert repair.await_count == 1, "repair runs only once reconcile finally succeeds"

    async def test_cancelled_error_does_not_publish_error_event(self) -> None:
        """ainvoke raises CancelledError → wrapper **does not** emit Error event.
        Cancel path already emits Cancelled event by _llm_node_impl itself, emitting Error here
        would cause frontend to show "cancelled" and "error" simultaneously, which is contradictory. Lock the contract that "CancelledError does not enter Error
        emit branch"."""
        graph = MagicMock()
        graph.ainvoke = AsyncMock(side_effect=asyncio.CancelledError())
        pub = MagicMock()
        ctx = AvaContext(
            ops_pool=MagicMock(),
            llm=MagicMock(),
            event_publisher=pub,
        )

        with pytest.raises(asyncio.CancelledError):
            await _invoke_graph_with_lifecycle_logging(graph, agent_id=42, ctx=ctx)

        error_emits = [call for call in pub.emit.call_args_list if '"role":"error"' in call.args[0]]
        assert len(error_emits) == 0, (
            f"CancelledError path must not emit Error; got {error_emits!r}"
        )

    async def test_normal_completion_no_log(self, loguru_records) -> None:
        """ainvoke returns normally → helper does not log error (only ERROR level traces represent exception paths)."""
        graph = MagicMock()
        graph.ainvoke = AsyncMock(return_value={"exit_requested": True})

        await _invoke_graph_with_lifecycle_logging(graph, agent_id=42, ctx=_fake_ctx())

        error_records = [r for r in loguru_records if r["level"].name == "ERROR"]  # pyright: ignore[reportUnknownMemberType]
        assert not error_records, f"Normal path should not log ERROR; got {error_records}"

    async def test_normal_completion_flushes_nstep_checkpoint_tail(self) -> None:
        graph = MagicMock()
        graph.ainvoke = AsyncMock(return_value={"exit_requested": True})
        flush = AsyncMock()
        graph.checkpointer._ava_nstep_flush = flush

        await _invoke_graph_with_lifecycle_logging(graph, agent_id=42, ctx=_fake_ctx())

        flush.assert_awaited_once()

    async def test_ainvoke_input_is_empty_state_update(self) -> None:
        """ainvoke's input is a state update (semantically same as a Command update
        returned by a node: merges keys into channels, messages use add_messages reducer, regular
        fields use last-value replacement; keys not present are untouched), not the full state — the state itself
        is provided by the thread_id checkpoint. At process startup there is no update to deliver, so pass
        empty update {} (non-None input still kicks off a new run; None means "resume only unfinished
        work" semantics). Passing a full AgentState() is equivalent to an "all-fields reset to defaults"
        update; each respawn would wipe halted + all plugin state fields
        (ava_code cwd / built-in compact/memory sub-states). The only keys each
        invocation carries are the per-invoke turn/exit/idle flag resets."""
        graph = MagicMock()
        graph.ainvoke = AsyncMock(return_value={"exit_requested": True})

        await _invoke_graph_with_lifecycle_logging(graph, agent_id=42, ctx=_fake_ctx())

        assert graph.ainvoke.call_args.args[0] == {
            "turn_active": False,
            "exit_requested": False,
            "turn_idle": False,
            "restart_requested": False,
        }

    async def test_turn_boundary_reinvokes_until_exit_requested(self) -> None:
        """One ainvoke = one TURN: a turn-boundary END (exit_requested=False in
        the returned state) re-invokes the graph on the same thread instead of
        exiting; only exit_requested=True (claim's terminate/restart winner or
        a lost lifecycle CAS) ends the loop. Every invocation's input resets
        all three flags, so a stale checkpointed True — a resurrect onto the
        same thread, or a rollback from hosted mode replaying a thread that
        checkpointed turn_idle=True — cannot kill or short-circuit the new
        process."""
        graph = MagicMock()
        graph.ainvoke = AsyncMock(
            side_effect=[
                {"exit_requested": False},
                {"exit_requested": False},
                {"exit_requested": True},
            ]
        )

        await _invoke_graph_with_lifecycle_logging(graph, agent_id=42, ctx=_fake_ctx())

        assert graph.ainvoke.call_count == 3, (
            "loop must re-invoke on each turn-boundary END and stop on exit_requested=True"
        )
        for call in graph.ainvoke.call_args_list:
            assert call.args[0] == {
                "turn_active": False,
                "exit_requested": False,
                "turn_idle": False,
                "restart_requested": False,
            }, "every invocation's input must reset the per-invoke turn/exit/idle/restart flags"

    async def test_exit_requested_flushes_node_exit_aggregate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A terminate/restart exit (goto END with exit_requested=True) never
        reaches another claim enter, so the runloop must flush the aggregated
        node_exit buffer before returning — otherwise a single-turn worker's
        node_exit events are silently lost and inspect activity reads zero
        (review #654-1)."""
        flushed: list[int] = []
        monkeypatch.setattr("agent._runloop.flush_node_exit_aggregate", flushed.append)
        graph = MagicMock()
        graph.ainvoke = AsyncMock(return_value={"exit_requested": True})

        await _invoke_graph_with_lifecycle_logging(graph, agent_id=42, ctx=_fake_ctx())

        assert flushed == [42]

    async def test_turn_boundary_reinvoke_flushes_before_continue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every invocation return flushes — the turn-boundary re-invoke path
        too, not only the final exit (the last claim exit of each invocation is
        in the buffer and must land before the next invocation starts)."""
        flushed: list[int] = []
        monkeypatch.setattr("agent._runloop.flush_node_exit_aggregate", flushed.append)
        graph = MagicMock()
        graph.ainvoke = AsyncMock(side_effect=[{"exit_requested": False}, {"exit_requested": True}])

        await _invoke_graph_with_lifecycle_logging(graph, agent_id=42, ctx=_fake_ctx())

        assert flushed == [42, 42]


class TestDbRecoveryWait:
    """`_wait_for_db_recovery` / `_probe_db_reachable` — the pause half of the
    DB-outage branch: probe-first, exponential backoff (capped) between failed
    probes, and CancelledError wins over the wait. The real-Postgres bounce is
    exercised in tests/agent/test_db_outage_recovery.py."""

    async def test_returns_immediately_when_reachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Probe-first: an instantly-reachable DB returns without ever sleeping."""
        probe = AsyncMock(return_value=True)
        monkeypatch.setattr("agent._runloop._probe_db_reachable", probe)
        slept: list[float] = []
        monkeypatch.setattr("agent._runloop.asyncio.sleep", AsyncMock(side_effect=slept.append))

        await _wait_for_db_recovery(agent_id=1)

        probe.assert_awaited_once()
        assert slept == [], "a reachable DB must not back off"

    async def test_loops_until_reachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two failed probes then success → three probes, two backoffs, returns."""
        probe = AsyncMock(side_effect=[False, False, True])
        monkeypatch.setattr("agent._runloop._probe_db_reachable", probe)
        monkeypatch.setattr("agent._runloop.asyncio.sleep", AsyncMock())

        await _wait_for_db_recovery(agent_id=1)

        assert probe.await_count == 3

    async def test_backoff_grows_and_caps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Backoff doubles from the initial 1s to the 30s cap and holds there —
        so a long outage settles into a steady re-probe, no tight reconnect loop."""
        probe = AsyncMock(side_effect=[False, False, False, False, False, False, True])
        monkeypatch.setattr("agent._runloop._probe_db_reachable", probe)
        slept: list[float] = []
        monkeypatch.setattr("agent._runloop.asyncio.sleep", AsyncMock(side_effect=slept.append))

        await _wait_for_db_recovery(agent_id=1)

        assert slept == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]

    async def test_propagates_cancelled_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A SIGTERM/restart during the wait (CancelledError) is NOT swallowed —
        it wins so the process can exit cleanly instead of blocking on a dead DB."""

        async def _probe(_url: str) -> bool:
            raise asyncio.CancelledError

        monkeypatch.setattr("agent._runloop._probe_db_reachable", _probe)

        with pytest.raises(asyncio.CancelledError):
            await _wait_for_db_recovery(agent_id=1)

    async def test_probe_false_on_connection_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A refused / dead connection is a negative probe, never an error the
        caller has to handle — the wait loop just backs off and retries."""

        async def _boom(*_a: object, **_k: object) -> object:
            raise psycopg.OperationalError("connection refused")

        monkeypatch.setattr("agent._runloop.psycopg.AsyncConnection.connect", _boom)

        assert await _probe_db_reachable("postgresql://ava@127.0.0.1:1/none") is False


# ─── exit reason / signal handler unit tests ───
#
# 159 / 160 silent death incident: session close SIGHUP the whole process group →
# Python default handler immediate kill → main()'s finally can't run → agents.status
# stuck at 'running'. Fix: install SIGHUP / SIGTERM handler to convert to SystemExit so finally runs,
# and in finally emit `process_exit` event with reason tag (normal /
# signal:NAME / exception:Type).


class TestExitReason:
    """`_exit_reason()` deduces process_exit reason label from sys.exc_info().
    Called in main process finally — different exit paths should give ops different diagnostic signals."""

    def test_no_exception_returns_normal(self) -> None:
        """graph.ainvoke returns normally (terminate path) → reason='normal'."""
        assert _exit_reason() == "normal"

    def test_signal_systemexit_preserves_signal_name(self) -> None:
        """signal handler raises SystemExit('signal:SIGHUP') → reason restores signal name.
        Test must raise inside try to set sys.exc_info() for _exit_reason() to see."""
        try:
            raise SystemExit("signal:SIGHUP")  # noqa: TRY301 — set sys.exc_info()
        except SystemExit:
            assert _exit_reason() == "signal:SIGHUP"

    def test_plain_systemexit_returns_system_exit(self) -> None:
        """sys.exit() / sys.exit(1) (no signal: prefix) → 'system_exit'."""
        try:
            raise SystemExit(1)  # noqa: TRY301 — set sys.exc_info()
        except SystemExit:
            assert _exit_reason() == "system_exit"

        try:
            raise SystemExit  # noqa: TRY301 — set sys.exc_info()
        except SystemExit:
            assert _exit_reason() == "system_exit"

    def test_generic_exception_returns_type_name(self) -> None:
        """Other exceptions → 'exception:{ClassName}', together with traceback field auto-filled by _postgres_sink
        is enough for diagnosis, no need to put message in reason."""
        try:
            raise RuntimeError("boom")  # noqa: TRY301 — set sys.exc_info()
        except RuntimeError:
            assert _exit_reason() == "exception:RuntimeError"

        try:
            raise asyncio.CancelledError  # noqa: TRY301 — set sys.exc_info()
        except asyncio.CancelledError:
            assert _exit_reason() == "exception:CancelledError"


class TestInstallLifecycleSignalHandlers:
    """SIGHUP / SIGTERM converted to SystemExit, letting main() finally run — regression guard for 159 / 160
    silent death. Tests use signal.getsignal as side-effect spy, do not actually send signals
    (sending real SIGHUP / SIGTERM to pytest runner would kill the test process)."""

    def test_installs_sighup_and_sigterm_handlers(self) -> None:
        import signal

        prev_sighup = signal.getsignal(signal.SIGHUP)
        prev_sigterm = signal.getsignal(signal.SIGTERM)
        try:
            _install_lifecycle_signal_handlers()
            new_sighup = signal.getsignal(signal.SIGHUP)
            new_sigterm = signal.getsignal(signal.SIGTERM)
            assert callable(new_sighup) and new_sighup is not prev_sighup
            assert callable(new_sigterm) and new_sigterm is not prev_sigterm
        finally:
            signal.signal(signal.SIGHUP, prev_sighup)
            signal.signal(signal.SIGTERM, prev_sigterm)

    def test_handler_raises_systemexit_with_signal_prefix(self) -> None:
        """Call handler directly to verify it raises SystemExit('signal:NAME') —
        _exit_reason can restore signal name from message."""
        import signal

        prev = signal.getsignal(signal.SIGHUP)
        try:
            _install_lifecycle_signal_handlers()
            handler = signal.getsignal(signal.SIGHUP)
            assert callable(handler)
            with pytest.raises(SystemExit, match=r"^signal:SIGHUP$"):
                handler(int(signal.SIGHUP), None)
        finally:
            signal.signal(signal.SIGHUP, prev)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)


# ─── MCP daemon subprocess manager ───
#
# `_MCPDaemon` interface (start / stop) — don't actually spawn ava._mcps_daemon
# (slow + couples with OS socket). The shared-daemon mode makes every method a
# no-op; these tests pin that contract so a refactor cannot quietly resurrect
# the 12MB-per-agent child.


class TestMcpSocketPath:
    def test_socket_path_is_shared(self) -> None:
        """Socket filename has NO agent_id — one shared daemon serves every agent.

        The per-machine shared daemon (ops roster session "mcp-daemon") replaces
        the old one-daemon-per-agent naming; the path is a fixed per-machine
        socket, and session isolation happens per client connection inside the
        daemon, not per socket.
        """
        path = _mcp_socket_path()
        assert path.endswith("mcp_daemon.sock")
        assert "42" not in path.rsplit("/", 1)[-1]


class TestMCPDaemonInit:
    def test_initial_state_is_not_started(self) -> None:
        """After construction daemon has not started; _started=False, socket_path immediately readable."""
        daemon = _MCPDaemon(42)
        assert daemon.started is False
        assert daemon.socket_path.endswith("mcp_daemon.sock")


class TestMCPDaemonStart:
    """Shared-daemon mode: `start()`/`spawn()`/`await_ready()` are no-ops.

    The daemon is a per-machine watchdog-managed service; the agent must NOT
    spawn (or wait for) its own child. These tests pin the no-op contract so a
    future refactor cannot quietly resurrect the 12MB-per-agent child.
    """

    async def test_start_is_noop_and_never_spawns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """start() does not create a subprocess and leaves started=False."""
        create = AsyncMock(side_effect=AssertionError("must not spawn"))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

        daemon = _MCPDaemon(99)
        await daemon.start()
        assert daemon.started is False
        assert daemon._proc is None
        create.assert_not_called()

    async def test_spawn_and_await_ready_are_noop(self) -> None:
        """spawn() + await_ready() keep the interface but do nothing."""
        daemon = _MCPDaemon(99)
        await daemon.spawn()
        await daemon.await_ready()
        assert daemon.started is False
        assert daemon._proc is None


class TestMCPDaemonStop:
    """`stop()` must NOT terminate the shared daemon (it serves every agent on
    the machine; the watchdog owns its lifecycle). No-op contract."""

    async def test_stop_is_noop(self) -> None:
        daemon = _MCPDaemon(42)
        daemon._proc = MagicMock(spec=asyncio.subprocess.Process)
        await daemon.stop()
        # the shared daemon's process handle must never be touched
        assert daemon._proc is None


# ─── run() entry point ───


class TestRunEntry:
    """`run()` is the argparse + asyncio.run entry for `python -m agent --agent-id N`.
    This test does not actually run the main loop (requires LangGraph + Redis + real LLM), only verifies:
    - missing --agent-id raises SystemExit (argparse default behavior)
    - with --agent-id goes through assert_schema_current + signal handlers install + asyncio.run(main(id))
    """

    def test_missing_agent_id_raises_system_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """argparse required=True, missing argument → SystemExit(2)."""
        monkeypatch.setattr("sys.argv", ["python -m agent"])
        with pytest.raises(SystemExit):
            run()

    def test_run_accepts_what_boot_flag_consumption_leaves_behind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The seam between `agent/__main__.py` and this parser, end to end.

        This parser is strict and it runs AFTER the row is claimed, so an argument
        it does not recognise is a `SystemExit(2)` for every agent on the box, at
        the one moment the row says 'running' under a pid that is leaving. The
        launcher passes `--boot-stall-seconds` on every launch, and nothing here
        declares it — `agent/_boot_deadline.consume_stall_flag` strips it first.
        This test runs that exact sequence over the argv the launcher really
        builds, because either half alone looks fine.
        """
        from agent import _boot_deadline

        seen: list[int] = []

        async def _coro() -> None:
            pass

        def _fake_main(agent_id: int, config_overlay=None, birth_config=None):
            seen.append(agent_id)
            return _coro()

        monkeypatch.setattr("shared.migrations.assert_schema_current", lambda _url: None)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr("agent.loop._install_lifecycle_signal_handlers", lambda: None)
        monkeypatch.setattr("agent.loop.main", _fake_main)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr("agent.loop.asyncio.run", lambda coro: coro.close())  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        # exactly what `_launch_agent_process` builds, minus the interpreter path
        monkeypatch.setattr(
            "sys.argv",
            [
                "python -m agent",
                "--agent-id",
                "777",
                "--boot-stall-seconds",
                str(boot_timing.BOOT_STALL_SEC),
                "--boot-budget-seconds",
                str(boot_timing.BOOT_BUDGET_SEC),
            ],
        )

        windows = _boot_deadline.consume_flags(sys.argv)  # what __main__ does first
        run()  # must not SystemExit

        assert windows == (
            boot_timing.BOOT_STALL_SEC,
            boot_timing.BOOT_BUDGET_SEC,
        )
        assert seen == [777]

    def test_with_agent_id_invokes_main(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With --agent-id: assert_schema_current + install signal handlers +
        asyncio.run(main(id)) three steps called in order. Fake the whole chain, not actually run event loop."""
        calls: list[
            tuple
        ] = []  # heterogeneous: ('assert', _), ('install', None), ('main', id, overlay)
        captured_coro: list[object] = []

        def _fake_assert_schema(url: object) -> None:
            calls.append(("assert_schema_current", url))  # pyright: ignore[reportUnknownMemberType]

        def _fake_install_handlers() -> None:
            calls.append(("install_handlers", None))  # pyright: ignore[reportUnknownMemberType]

        # _fake_main is a sync function returning coroutine placeholder — run() calls
        # asyncio.run(main(id)), we replace main with sync that takes agent_id, then return
        # fake coroutine so asyncio.run accepts it; asyncio.run is also mocked to capture-only.
        async def _coro_placeholder() -> None:
            pass

        def _fake_main_sync(agent_id: int, config_overlay=None, birth_config=None):
            calls.append(("main", agent_id, config_overlay, birth_config))  # pyright: ignore[reportUnknownMemberType]
            return _coro_placeholder()

        def _fake_run(coro: object) -> None:
            captured_coro.append(coro)
            # close coroutine so GC doesn't warn
            import contextlib

            with contextlib.suppress(Exception):
                coro.close()  # type: ignore[attr-defined]

        monkeypatch.setattr("shared.migrations.assert_schema_current", _fake_assert_schema)
        monkeypatch.setattr("agent.loop._install_lifecycle_signal_handlers", _fake_install_handlers)
        monkeypatch.setattr("agent.loop.main", _fake_main_sync)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr("agent.loop.asyncio.run", _fake_run)
        monkeypatch.setattr("sys.argv", ["python -m agent", "--agent-id", "777"])

        run()

        names = [c[0] for c in calls]
        assert names.index("assert_schema_current") < names.index("install_handlers"), (  # pyright: ignore[reportUnknownMemberType]
            "schema check must precede signal handler installation — wrong-version db should blow up earlier "
            "before handlers are installed, so that handler isn't set up only to see SQL error in claim_agent_row"
        )
        main_calls = [c for c in calls if c[0] == "main"]
        assert main_calls and main_calls[0][1] == 777
        # asyncio.run received the coroutine returned by main(777)
        assert len(captured_coro) == 1

    def test_swallows_gateway_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Gateway unreachable during a silent death → logged, not raised."""

        def _boom(_aid: int) -> None:
            raise RuntimeError("gateway down")

        monkeypatch.setattr("ava._gateway_client.exited", _boom)
        _notify_exit(42)  # must not raise


class TestRouteProcessEndNotify:
    """The process-exit hook notifies the gateway AND releases the agent's
    browser page (structural piece of the dead-localhost-tab fix); the
    release is best-effort and must never raise out of the exit finally."""

    async def test_notifies_then_releases_browser_page(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []

        def _record_exit(aid: int) -> None:
            order.append(f"exit:{aid}")

        monkeypatch.setattr("agent.lifecycle._notify_exit", _record_exit)

        async def _release(aid: int) -> None:
            order.append(f"release:{aid}")

        monkeypatch.setattr("agent.lifecycle._release_agent_browser_page", _release)
        await _route_process_end_notify(42, "normal")
        assert order == ["exit:42", "release:42"]

    async def test_release_failure_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _noop(_aid: int) -> None:
            return None

        monkeypatch.setattr("agent.lifecycle._notify_exit", _noop)

        async def _boom(_aid: int) -> bool:
            raise RuntimeError("browser service down")

        monkeypatch.setattr("ava._mcp_browser.release_agent_chrome_pages", _boom)
        await _route_process_end_notify(42, "normal")  # must not raise
