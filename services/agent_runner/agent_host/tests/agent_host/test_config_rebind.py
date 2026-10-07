"""Agent host cases: config rebind."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from psycopg_pool import AsyncConnectionPool

from agent.ownership.corpse_reap import ReapedCorpse
from base.events.live.bus import EventBus
from services.agent_runner.agent_host.runtime import _config_fingerprint
from services.agent_runner.agent_host.tests.test_agent_host import (
    _Build,
    _GatedGraph,
    _Row,
    _stub_host_transitions,
)
from services.agent_runner.agent_host.tests.test_agent_host import (
    host_plugin as host_plugin,
)
from services.agent_runner.agent_host.tests.test_agent_host import (
    wired as wired,
)
from tests.base.poll_until import poll_until_async


class TestConfigRebind:
    async def test_a_changed_overlay_rebuilds_the_runtime_and_the_turn_sees_it(
        self, wired: _Build
    ) -> None:
        """Write overlay -> wake -> the turn runs on the new value.

        The second turn must NOT reuse the cached runtime: it was built from the
        old config, so reusing it would keep the agent on its old model while
        `agents_meta` says otherwise.
        """
        rows = {1: _Row(overlay={"llm_model": "before"})}
        host, graph, _ = wired(rows)

        await asyncio.wait_for(host.run_turn(1), 2)
        assert graph.observations[-1].model == "before"
        assert host.stats.cache_misses == 1

        # The overlay write, as `ava.self.restart(config_overlay)` performs it.
        rows[1] = _Row(overlay={"llm_model": "after"})
        await asyncio.wait_for(host.run_turn(1), 2)

        assert graph.observations[-1].model == "after"
        assert graph.observations[-1].llm.name == "after", (
            "the model must be REBUILT, not just re-read from settings"
        )
        assert host.stats.cache_misses == 2, "a changed config must miss the cache"
        assert host.stats.cache_hits == 0

    async def test_an_unchanged_overlay_hits_the_cache(self, wired: _Build) -> None:
        """The other half: re-reading the same row must not look like a change,
        or every turn would pay a cold build."""
        host, _, _ = wired({1: _Row(overlay={"llm_model": "steady"})})
        await asyncio.wait_for(host.run_turn(1), 2)
        await asyncio.wait_for(host.run_turn(1), 2)
        assert (host.stats.cache_misses, host.stats.cache_hits) == (1, 1)

    def test_the_fingerprint_ignores_key_order(self) -> None:
        """JSONB gives no key-order guarantee, so a fingerprint that depended on
        it would rebuild at random."""
        a = _config_fingerprint({"x": 1, "y": 2}, None)
        b = _config_fingerprint({"y": 2, "x": 1}, None)
        assert a == b
        assert a != _config_fingerprint({"x": 1, "y": 3}, None)

    def test_the_fingerprint_separates_the_two_maps(self) -> None:
        """`birth_config` and `config_overlay` are different columns with
        different provenance; a fingerprint that flattened them would call two
        genuinely different states equal."""
        assert _config_fingerprint({"x": 1}, None) != _config_fingerprint(None, {"x": 1})

    async def test_the_row_is_re_read_every_turn(self, wired: _Build) -> None:
        """The rebind only works if the read is per turn — caching the row with
        the runtime would make an overlay land only after an eviction."""
        host, _, pool = wired({1: _Row()})
        await asyncio.wait_for(host.run_turn(1), 2)
        await asyncio.wait_for(host.run_turn(1), 2)
        assert pool.reads == 2


class TestTurnLoop:
    async def test_host_builds_a_turn_scoped_nstep_saver(self) -> None:
        """The shared host saver must install the interval wrapper at boot."""
        from services.agent_runner.agent_host.daemon import _build_checkpointer

        checkpointer = await _build_checkpointer(cast(AsyncConnectionPool[Any], object()))

        assert "_ava_nstep_flush" in checkpointer.__dict__

    async def test_completed_turn_flushes_its_own_nstep_checkpoint_tail(
        self, wired: _Build
    ) -> None:
        """Hosted turns must persist a skipped tail before the task returns."""
        host, _, _ = wired({1: _Row()})
        flush = AsyncMock()
        saver: Any = type("_NstepSaver", (), {})()
        saver._ava_nstep_flush = flush
        host._checkpointer = saver

        await host.run_turn(1)

        flush.assert_awaited_once_with("1")

    async def test_a_turn_flips_status_running_then_idling(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        flips: list[tuple[int, str, str]] = []

        async def _flip(_pool: object, agent_id: int, to: str, *, expected_from: str) -> bool:
            flips.append((agent_id, to, expected_from))
            return True

        _stub_host_transitions(monkeypatch, _flip)
        host, _, _ = wired({1: _Row(status="idling")})

        await asyncio.wait_for(host.run_turn(1), 2)

        assert flips == [(1, "running", "idling"), (1, "idling", "running")]

    async def test_a_crashing_turn_restores_idling(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        flips: list[tuple[int, str, str]] = []

        async def _flip(_pool: object, agent_id: int, to: str, *, expected_from: str) -> bool:
            flips.append((agent_id, to, expected_from))
            return True

        async def _boom(*_args: object, **_kwargs: object) -> dict[str, Any]:
            raise RuntimeError("turn exploded")

        stamps = _stub_host_transitions(monkeypatch, _flip)
        host, graph, _ = wired({1: _Row(status="idling")})
        graph.ainvoke = _boom

        with pytest.raises(RuntimeError, match="turn exploded"):
            await asyncio.wait_for(host.run_turn(1), 2)

        assert flips[-1] == (1, "idling", "running")
        # The crash is a corpse: the settle is parked (keeps any marker) and
        # the fatal stamp lands even though the turn raised.
        assert stamps == [1]

    async def test_a_normal_idle_turn_is_not_stamped_as_a_corpse(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        flips: list[tuple[int, str, str]] = []

        async def _flip(_pool: object, agent_id: int, to: str, *, expected_from: str) -> bool:
            flips.append((agent_id, to, expected_from))
            return True

        stamps = _stub_host_transitions(monkeypatch, _flip)
        host, _, _ = wired(
            {1: _Row(status="idling")},
            {1: [{"exit_requested": False, "turn_idle": True, "restart_requested": False}]},
        )

        await asyncio.wait_for(host.run_turn(1), 2)

        assert flips[-1] == (1, "idling", "running")
        assert stamps == []

    async def test_a_cancelled_turn_is_not_stamped_as_a_corpse(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        flips: list[tuple[int, str, str]] = []

        async def _flip(_pool: object, agent_id: int, to: str, *, expected_from: str) -> bool:
            flips.append((agent_id, to, expected_from))
            return True

        async def _cancel(*_args: object, **_kwargs: object) -> dict[str, Any]:
            raise asyncio.CancelledError

        stamps = _stub_host_transitions(monkeypatch, _flip)
        host, graph, _ = wired({1: _Row(status="idling")})
        graph.ainvoke = _cancel

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(host.run_turn(1), 2)

        assert stamps == []

    async def test_renew_ownership_reaps_after_renewing(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Renewal first, reap second: a reap failure must not starve leases.
        The reap publishes each corpse snapshot itself (agent/ownership/corpse_reap.py),
        and the reaped corpses' recovery attempt rides right after the reap."""
        import services.agent_runner.agent_host.host as host_mod

        calls: list[str] = []

        async def _renew(pool: object, machine: str, owner: UUID) -> None:
            calls.append("renew")

        async def _reap(
            pool: object, machine: str, owner: UUID, *, bus: object
        ) -> list[ReapedCorpse]:
            calls.append("reap")
            return [ReapedCorpse(7, 101), ReapedCorpse(9, None)]

        recovered: list[list[ReapedCorpse]] = []

        async def _recover(_db: object, _bus: EventBus, reaped: list[ReapedCorpse]) -> None:
            calls.append("recover")
            recovered.append(list(reaped))

        monkeypatch.setattr(host_mod, "renew_hosted_owner", _renew)
        monkeypatch.setattr(host_mod, "reap_crash_corpses", _reap)
        monkeypatch.setattr(host_mod, "recover_reaped_corpses", _recover)

        host, _, _ = wired({1: _Row(status="idling")})
        await host.renew_ownership()

        assert calls == ["renew", "reap", "recover"]
        assert recovered == [[ReapedCorpse(7, 101), ReapedCorpse(9, None)]]

    async def test_renew_ownership_survives_a_reap_failure(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The beat must keep renewing healthy leases when the reap explodes."""
        import services.agent_runner.agent_host.host as host_mod

        renewed: list[str] = []

        async def _renew(pool: object, machine: str, owner: UUID) -> None:
            renewed.append("renew")

        async def _reap(pool: object, machine: str, owner: UUID, *, bus: object) -> list[int]:
            raise RuntimeError("reap exploded")

        monkeypatch.setattr(host_mod, "renew_hosted_owner", _renew)
        monkeypatch.setattr(host_mod, "reap_crash_corpses", _reap)

        host, _, _ = wired({1: _Row(status="idling")})
        await host.renew_ownership()  # must not raise

        assert renewed == ["renew"]

    async def test_exit_requested_does_not_restore_idling(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        flips: list[tuple[int, str, str]] = []

        async def _flip(_pool: object, agent_id: int, to: str, *, expected_from: str) -> bool:
            flips.append((agent_id, to, expected_from))
            return True

        _stub_host_transitions(monkeypatch, _flip)
        host, _, _ = wired(
            {1: _Row(status="idling")},
            {1: [{"exit_requested": True, "turn_idle": False, "restart_requested": False}]},
        )

        await asyncio.wait_for(host.run_turn(1), 2)

        assert flips == [(1, "running", "idling")]

    async def test_a_losing_start_flip_skips_the_turn(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        flips: list[tuple[int, str, str]] = []

        async def _flip(_pool: object, agent_id: int, to: str, *, expected_from: str) -> bool:
            flips.append((agent_id, to, expected_from))
            return False

        _stub_host_transitions(monkeypatch, _flip)
        host, graph, _ = wired({1: _Row(status="idling")})

        await asyncio.wait_for(host.run_turn(1), 2)

        assert flips == [(1, "running", "idling")]
        assert graph.observations == []
        assert host._runtimes == {}
        assert host.stats.cache_misses == 0

    async def test_restart_requested_restores_idling(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import services.agent_runner.agent_host.host as host_mod
        from base.native_process.runtime_incarnation import RuntimeIncarnation

        flips: list[tuple[int, str, str]] = []

        async def _flip(_pool: object, agent_id: int, to: str, *, expected_from: str) -> bool:
            flips.append((agent_id, to, expected_from))
            return True

        _stub_host_transitions(monkeypatch, _flip)
        host, _, _ = wired(
            {1: _Row(status="idling")},
            {1: [{"exit_requested": False, "turn_idle": False, "restart_requested": True}]},
        )

        async def apply(pool: object, incarnation: RuntimeIncarnation, **_kwargs: object) -> str:
            await _flip(pool, incarnation.agent_id, "idling", expected_from="running")
            return "restart"

        monkeypatch.setattr(host_mod, "apply_hosted_lifecycle", apply)

        await asyncio.wait_for(host.run_turn(1), 2)

        assert flips[-1] == (1, "idling", "running")

    async def test_turn_idle_ends_the_task(self, wired: _Build) -> None:
        host, graph, _ = wired(
            {1: _Row()},
            {1: [{"exit_requested": False, "turn_idle": True, "restart_requested": False}]},
        )
        await asyncio.wait_for(host.run_turn(1), 2)
        assert len(graph.observations) == 1

    async def test_a_turn_boundary_re_invokes_on_the_same_thread(self, wired: _Build) -> None:
        """Neither flag set means "turn over, more may be pending" — the host
        goes round again rather than ending the task, so a burst drains in one
        task instead of needing a wake per turn."""
        host, graph, _ = wired(
            {1: _Row()},
            {
                1: [
                    {"exit_requested": False, "turn_idle": False, "restart_requested": False},
                    {"exit_requested": False, "turn_idle": False, "restart_requested": False},
                    {"exit_requested": False, "turn_idle": True, "restart_requested": False},
                ]
            },
        )
        await asyncio.wait_for(host.run_turn(1), 2)
        assert len(graph.observations) == 3

    async def test_exit_requested_applies_to_admitted_incarnation(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hosted exit uses the admitted owner's durable apply, not process exit RPC."""
        import services.agent_runner.agent_host.host as host_mod
        from base.native_process.runtime_incarnation import RuntimeIncarnation

        notified: list[int] = []

        async def apply(_pool: object, incarnation: RuntimeIncarnation, **_kwargs: object) -> str:
            notified.append(incarnation.agent_id)
            return "terminate"

        monkeypatch.setattr(host_mod, "apply_hosted_lifecycle", apply)
        host, graph, _ = wired(
            {1: _Row()},
            {1: [{"exit_requested": True, "turn_idle": False, "restart_requested": False}]},
        )
        await asyncio.wait_for(host.run_turn(1), 2)
        assert len(graph.observations) == 1
        assert notified == [1]

    async def test_exit_drops_the_cached_runtime(self, wired: _Build) -> None:
        """The next wake must start clean — the fresh-process half of a restart."""
        host, _, _ = wired(
            {1: _Row()},
            {1: [{"exit_requested": True, "turn_idle": False, "restart_requested": False}]},
        )
        await asyncio.wait_for(host.run_turn(1), 2)
        assert 1 not in host._runtimes

    async def test_restart_requested_applies_after_continuation_and_cache_release(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A returned graph releases its cache before the owner-fenced application."""
        import services.agent_runner.agent_host.host as host_mod
        from base.native_process.runtime_incarnation import RuntimeIncarnation

        notified: list[int] = []

        async def apply(_pool: object, incarnation: RuntimeIncarnation, **_kwargs: object) -> str:
            assert incarnation.agent_id not in host._runtimes
            assert len(graph.observations) == 1
            notified.append(incarnation.agent_id)
            return "restart"

        monkeypatch.setattr(host_mod, "apply_hosted_lifecycle", apply)
        host, graph, _ = wired(
            {1: _Row()},
            {1: [{"exit_requested": False, "turn_idle": False, "restart_requested": True}]},
        )
        await asyncio.wait_for(host.run_turn(1), 2)
        assert len(graph.observations) == 1
        assert notified == [1]
        assert 1 not in host._runtimes

    async def test_a_cancelled_turn_drops_the_cached_runtime(self, wired: _Build) -> None:
        """A cancelled turn must not keep its runtime: the abandoned turn left
        claimed inbounds behind, and the next wake's runtime build re-runs the
        startup reconcile — the hosted equivalent of a fresh process's boot.
        The dispatcher's stale-turn scan depends on exactly this drop."""
        host, graph, _ = wired({11: _Row(overlay={"llm_model": "model-for-11"})})
        graph.gate(11)
        task = asyncio.create_task(host.run_turn(11))
        await asyncio.wait_for(graph.arrival(11).wait(), 2)
        assert 11 in host._runtimes
        task.cancel()
        # The turn-level runner SHIELDS its in-flight invocation, so an outer
        # cancel waits for the turn to settle before it raises: the uncancellable
        # turn contract (Task #2436) keeps the owned resources from being torn
        # down under a live invocation. Release the gate so the faked invocation
        # returns like a bounded real one would, then the wrapper can unwind.
        graph.gate(11).set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert 11 not in host._runtimes

    async def test_a_turn_starts_a_fresh_progress_window(self, wired: _Build) -> None:
        """`run_turn` must reset the per-agent turn-progress clock on entry:
        otherwise a long-idle agent's stale entry reads as "stalled" during the
        next turn's cold build and the dispatcher's turn-level scan cancels the
        recovery turn it just scheduled."""
        import time as _time

        host, _, _ = wired({11: _Row(overlay={"llm_model": "model-for-11"})})
        # A stale entry as a long-ago turn would leave behind...
        host.turn_progress._marks[11] = [_time.monotonic() - 99999.0]
        await asyncio.wait_for(host.run_turn(11), 2)
        age = host.turn_progress.age_s(11)
        assert age is not None and age < 5.0, f"clock not reset, age={age}"

    async def test_a_turn_runs_with_the_hosts_own_shared_state(self, wired: _Build) -> None:
        """The graph's writers and the host's readers must hold the same objects: a context
        built with private ones would leave the stall guard reading an empty clock."""
        host, _, _ = wired({11: _Row(overlay={"llm_model": "model-for-11"})})
        graph = _GatedGraph()
        host._graph = graph  # type: ignore[assignment]
        task = asyncio.create_task(host.run_turn(11))
        await poll_until_async(graph.entered.is_set)
        graph.release.set()
        await task
        [ctx] = graph.contexts
        assert ctx.turn_progress is host.turn_progress
        assert ctx.relays is host.relays

    async def test_a_skipped_wake_still_starts_a_fresh_progress_window(self, wired: _Build) -> None:
        """The reset must precede every await in `_run_turn`.

        A task created between two dispatcher scans — a resurrect wake for a
        terminated agent — is otherwise judged on its predecessor incarnation's
        clock, and the dispatcher's stale-turn cancel lands before the new turn
        can open its window (2026-09-11: the bounded unwind was refused in
        teardown and the host exited). A non-runnable wake skips admission but
        is still a fresh task and must reset the clock.
        """
        import time as _time

        host, _, _ = wired({11: _Row(status="terminated")})
        host.turn_progress._marks[11] = [_time.monotonic() - 99999.0]
        await asyncio.wait_for(host.run_turn(11), 2)
        age = host.turn_progress.age_s(11)
        assert age is not None and age < 5.0, f"clock not reset, age={age}"

    async def test_a_crashing_turn_drops_the_runtime(self, wired: _Build) -> None:
        """A crash is the hosted equivalent of a process dying mid-turn, and a
        respawn re-runs the startup reconcile. Keeping the cached runtime would
        skip it on the retry and leave `claimed` rows unresolved."""
        host, graph, _ = wired({1: _Row()})

        async def _boom(*_a: object, **_k: object) -> dict[str, Any]:
            raise RuntimeError("turn exploded")

        await asyncio.wait_for(host.run_turn(1), 2)
        assert 1 in host._runtimes
        graph.ainvoke = _boom
        with pytest.raises(RuntimeError, match="turn exploded"):
            await asyncio.wait_for(host.run_turn(1), 2)
        assert 1 not in host._runtimes


class TestTurnStallGuard:
    """Task #2417, half 2: the no-progress stall guard around graph.ainvoke.

    A turn that stops making ANY progress (no node enter, no completed LLM
    step) past ``AVA_HOST_TURN_NO_PROGRESS_TIMEOUT_SECONDS`` is aborted, the
    error lands (one Error event), and the turn task ends; a turn that keeps
    stepping — the days-long autonomous loop the design allows — is never
    touched. All timing is shrunk: the tests run tiny intervals and let the
    guard's own poll do the work.
    """

    async def _stall_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from base.config import settings

        monkeypatch.setattr(settings.daemon, "host_turn_progress_scan_interval_seconds", 0.01)
        monkeypatch.setattr(settings.daemon, "host_turn_no_progress_timeout_seconds", 0.05)

    async def test_a_stalled_turn_is_aborted_and_its_error_lands(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import services.agent_runner.agent_host.stall_guard as guard_mod
        from services.agent_runner.agent_host.dispatcher import TurnStallTimeoutError

        await self._stall_settings(monkeypatch)
        host, _, _ = wired({11: _Row(overlay={"llm_model": "model-for-11"})})
        graph = _GatedGraph()
        host._graph = graph  # type: ignore[assignment]
        errors: list[str] = []

        def _record_error(_ctx: object, _agent_id: int, content: str) -> None:
            errors.append(content)

        monkeypatch.setattr(guard_mod, "emit_error_event", _record_error)

        with pytest.raises(TurnStallTimeoutError):
            await asyncio.wait_for(host.run_turn(11), timeout=30.0)

        assert graph.cancel_seen
        assert 11 not in host._runtimes, "the runtime must be dropped for the reconcile"
        assert len(errors) == 1
        assert "no progress" in errors[0]

    async def test_a_turn_that_keeps_marking_progress_is_never_aborted(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import services.agent_runner.agent_host.stall_guard as guard_mod

        await self._stall_settings(monkeypatch)
        host, _, _ = wired({11: _Row(overlay={"llm_model": "model-for-11"})})
        graph = _GatedGraph()
        host._graph = graph  # type: ignore[assignment]
        errors: list[str] = []

        def _record_error(_ctx: object, _agent_id: int, content: str) -> None:
            errors.append(content)

        monkeypatch.setattr(guard_mod, "emit_error_event", _record_error)

        async def _keep_stepping() -> None:
            while not graph.release.is_set():
                host.turn_progress.mark(11)
                await asyncio.sleep(0.004)

        keeper = asyncio.create_task(_keep_stepping())
        task = asyncio.create_task(host.run_turn(11))
        await poll_until_async(graph.entered.is_set)
        # Let the timeout pass several times over; the steady marks must keep
        # the turn alive.
        observation_ends_at = asyncio.get_running_loop().time() + 0.2
        await poll_until_async(lambda: asyncio.get_running_loop().time() >= observation_ends_at)
        assert not task.done(), "a progressing turn must not be aborted"
        graph.release.set()
        await poll_until_async(task.done)
        await task
        keeper.cancel()
        assert errors == []
        assert 11 in host._runtimes

    async def test_an_external_cancel_still_unwinds_through_the_guard(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An external cancel must not block the guard's bounded unwind.

        The turn-level runner SHIELDS its invocation (the durable-command
        contract, Task #2436), so an outer cancellation flags the turn and
        waits for it to settle rather than interrupting the graph directly.
        For a no-progress turn the guard is what actually stops the work; the
        external cancel must not wedge that abort — the still-unwinds outcome
        is the stall abort (TurnStallTimeoutError), never a stuck task.
        """
        from services.agent_runner.agent_host.dispatcher import TurnStallTimeoutError

        await self._stall_settings(monkeypatch)
        host, _, _ = wired({11: _Row(overlay={"llm_model": "model-for-11"})})
        graph = _GatedGraph()
        host._graph = graph  # type: ignore[assignment]

        task = asyncio.create_task(host.run_turn(11))
        await poll_until_async(graph.entered.is_set)
        task.cancel()
        await poll_until_async(task.done)
        with pytest.raises(TurnStallTimeoutError):
            await task

        assert graph.cancel_seen
        assert 11 not in host._runtimes, "the runtime must be dropped for the reconcile"

    async def test_a_turn_that_refuses_to_unwind_escalates_to_a_host_restart(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from services.agent_runner.agent_host.dispatcher import HostRestartRequiredError

        await self._stall_settings(monkeypatch)
        import services.agent_runner.agent_host.stall_guard as guard_mod

        monkeypatch.setattr(guard_mod, "CANCEL_UNWIND_TIMEOUT_S", 0.05)
        host, _, _ = wired({11: _Row(overlay={"llm_model": "model-for-11"})})
        graph = _GatedGraph(refuse_cancel=True)
        host._graph = graph  # type: ignore[assignment]

        with pytest.raises(HostRestartRequiredError, match="did not unwind"):
            await asyncio.wait_for(host.run_turn(11), timeout=30.0)

        assert graph.cancel_seen
        # Release the stuck invocation so the event loop is not left with a
        # pending task at teardown.
        graph.release.set()


class TestRunnability:
    async def test_another_machines_agent_is_never_run(self, wired: _Build) -> None:
        """The dispatcher's PSUBSCRIBE is cluster-wide, so on a multi-runner
        cluster every runner sees every wake. Nothing but this check keeps two
        runners from both claiming one agent's inbound."""
        host, graph, _ = wired({1: _Row(machine="some-other-box")})
        await asyncio.wait_for(host.run_turn(1), 2)
        assert graph.observations == []
        assert host.stats.wakes_skipped == 1
        assert host.stats.turns_started == 0

    async def test_a_terminated_agents_wake_is_skipped(self, wired: _Build) -> None:
        """A terminated agent's wake belongs to the delivery watchdog's resurrect
        path, so someone else owns this row right now."""
        host, graph, _ = wired({1: _Row(status="terminated")})
        await asyncio.wait_for(host.run_turn(1), 2)
        assert graph.observations == []
        assert host.stats.wakes_skipped == 1

    async def test_a_missing_row_is_skipped(self, wired: _Build) -> None:
        host, graph, _ = wired({})
        await asyncio.wait_for(host.run_turn(99), 2)
        assert graph.observations == []
        assert host.stats.wakes_skipped == 1

    async def test_a_skipped_wake_builds_no_runtime(self, wired: _Build) -> None:
        """The rejection must come BEFORE the cold build, or a burst of foreign
        wakes would evict every local agent's runtime."""
        host, _, _ = wired({1: _Row(machine="elsewhere")})
        await asyncio.wait_for(host.run_turn(1), 2)
        assert host._runtimes == {}
