"""Agent host cases: rejected model config."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from base.config import settings
from base.lm.catalog import ModelCatalog
from base.lm.factory import validate_model_config
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedTurnResources
from services.agent_runner.agent_host import dispatcher, settlement
from services.agent_runner.agent_host.dispatcher import TurnScheduler
from services.agent_runner.agent_host.runtime import _config_fingerprint
from services.agent_runner.agent_host.tests.test_agent_host import _Build, _Model, _Row
from services.agent_runner.agent_host.tests.test_agent_host import (
    host_plugin as host_plugin,
)
from services.agent_runner.agent_host.tests.test_agent_host import (
    wired as wired,
)
from tests.components.base.poll_until import poll_until_async
from tests.fixtures.model_catalog import AddModels


class TestRejectedModelConfig:
    async def test_an_unknown_model_is_rejected_before_the_runtime_build(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real registry rejects an unknown model without a provider key."""
        import services.agent_runner.agent_host.host as host_mod
        import services.agent_runner.agent_host.runtime as runtime_mod

        monkeypatch.setattr(runtime_mod, "validate_model_config", validate_model_config)
        boot_calls: list[int] = []
        error_events: list[str] = []

        async def _record_boot(
            agent_id: int, llm_model: str, *_: object, **_kwargs: object
        ) -> tuple[_Model, None]:
            boot_calls.append(agent_id)
            return _Model(llm_model), None

        def _record_error(_message: str, *, event: str, **_details: object) -> None:
            error_events.append(event)

        monkeypatch.setattr(runtime_mod, "boot_agent_scope", _record_boot)
        monkeypatch.setattr(host_mod.logger, "error", _record_error)
        host, graph, _ = wired({1: _Row(overlay={"llm_model": "fable"})})

        await asyncio.wait_for(host.run_turn(1), 2)

        assert boot_calls == []
        assert graph.observations == []
        assert host.stats.config_rejected == 1
        assert host.stats.as_payload()["config_rejected"] == 1
        assert host.stats.turns_started == 0
        assert error_events == ["host_config_rejected"]

    async def test_a_fixed_model_config_builds_on_the_next_wake(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A valid replacement clears the rejection note and resumes the turn."""
        import services.agent_runner.agent_host.runtime as runtime_mod

        boot_calls: list[int] = []
        validated_models: list[str] = []

        def _validate_model(*, model: str, catalog: ModelCatalog, llm_override: str) -> None:
            validated_models.append(model)
            if model == "fable":
                raise ValueError("unknown model 'fable'")

        async def _record_boot(
            agent_id: int, llm_model: str, *_: object, **_kwargs: object
        ) -> tuple[_Model, None]:
            boot_calls.append(agent_id)
            return _Model(llm_model), None

        monkeypatch.setattr(runtime_mod, "validate_model_config", _validate_model)
        monkeypatch.setattr(runtime_mod, "boot_agent_scope", _record_boot)
        rows = {1: _Row(overlay={"llm_model": "fable"})}
        host, graph, _ = wired(rows)

        await asyncio.wait_for(host.run_turn(1), 2)
        assert boot_calls == []
        assert host._rejected_configs

        rows[1] = _Row(overlay={"llm_model": "gpt-5.6-sol"})
        await asyncio.wait_for(host.run_turn(1), 2)

        assert boot_calls == [1]
        assert len(graph.observations) == 1
        assert host.stats.turns_started == 1
        assert host.stats.config_rejected == 1
        assert host._rejected_configs == {}
        assert validated_models == ["fable", "gpt-5.6-sol"]

    async def test_the_same_rejected_config_logs_once_per_config_state(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeated pending wakes stay quiet until the stored config changes."""
        import services.agent_runner.agent_host.host as host_mod
        import services.agent_runner.agent_host.runtime as runtime_mod

        error_events: list[str] = []

        def _reject_model(*, model: str, catalog: ModelCatalog, llm_override: str) -> None:
            raise ValueError(f"unknown model '{model}'")

        def _record_error(_message: str, *, event: str, **_details: object) -> None:
            error_events.append(event)

        monkeypatch.setattr(runtime_mod, "validate_model_config", _reject_model)
        monkeypatch.setattr(host_mod.logger, "error", _record_error)
        rows = {1: _Row(overlay={"llm_model": "fable"})}
        host, _, _ = wired(rows)

        await asyncio.wait_for(host.run_turn(1), 2)
        await asyncio.wait_for(host.run_turn(1), 2)
        rows[1] = _Row(overlay={"llm_model": "fable-2"})
        await asyncio.wait_for(host.run_turn(1), 2)

        assert host.stats.config_rejected == 3
        assert error_events == ["host_config_rejected", "host_config_rejected"]


class TestNormalizedModelConfig:
    """A stored pin naming a model the registry has withdrawn is bound as its
    registered fallback at the wake, before any consumer reads it — the model
    the turn view, the usage attribution and the exec children re-emitted from
    these pins all see."""

    @pytest.fixture
    def withdrawn_model(
        self, add_models: AddModels, model_catalog: ModelCatalog
    ) -> tuple[str, ModelCatalog]:
        from dataclasses import replace

        model = "deepseek-retired-fixture"
        catalog = add_models(
            model_catalog,
            {
                model: replace(
                    model_catalog.models["deepseek-flash"],
                    spawnable=False,
                    unavailable_fallback="deepseek-flash",
                )
            },
        )
        return model, catalog

    @pytest.fixture(autouse=True)
    def _isolate_settlement_reconcile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """These tests lock config normalization and its exact warning list;
        the settlement reconcile (locked in `TestSettlementReconciles`) must
        not add its own events here."""
        monkeypatch.setattr(settlement, "reconcile_inbounds_after_turn", AsyncMock())

    async def test_a_withdrawn_birth_pin_is_normalized_before_the_turn_binds_it(
        self,
        wired: _Build,
        monkeypatch: pytest.MonkeyPatch,
        withdrawn_model: tuple[str, ModelCatalog],
    ) -> None:
        """A birth_config pin on a withdrawn model resolves to its
        registered fallback — the same resolution build_chat_model applies only
        at the final build."""
        model, catalog = withdrawn_model
        import services.agent_runner.agent_host.host as host_mod

        warnings: list[tuple[str, dict[str, object]]] = []

        def _record_warning(_message: str, *, event: str, **details: object) -> None:
            warnings.append((event, details))

        monkeypatch.setattr(host_mod.logger, "warning", _record_warning)
        host, graph, _ = wired({1: _Row(birth={"llm_model": model})}, catalog=catalog)

        await asyncio.wait_for(host.run_turn(1), 2)

        assert [observation.model for observation in graph.observations] == ["deepseek-flash"]
        assert [observation.llm.name for observation in graph.observations] == ["deepseek-flash"]
        assert host.stats.config_normalized == 1
        # The once-per-state warning carries the agent and both ids, so a usage
        # row attributed to the withdrawn pin can be reconciled against it.
        assert [event for event, _ in warnings] == ["host_config_normalized"]
        assert warnings[0][1]["agent_id"] == 1
        assert warnings[0][1]["requested"] == model
        assert warnings[0][1]["resolved"] == "deepseek-flash"

    async def test_a_withdrawn_pin_normalizes_and_an_available_pin_is_untouched(
        self,
        wired: _Build,
        monkeypatch: pytest.MonkeyPatch,
        withdrawn_model: tuple[str, ModelCatalog],
    ) -> None:
        """A withdrawn pin resolves; an available model passes through."""
        model, catalog = withdrawn_model
        import services.agent_runner.agent_host.host as host_mod

        warnings: list[str] = []

        def _record_warning(_message: str, *, event: str, **_details: object) -> None:
            warnings.append(event)

        monkeypatch.setattr(host_mod.logger, "warning", _record_warning)
        rows = {
            1: _Row(overlay={"llm_model": model}),
            2: _Row(overlay={"llm_model": "gemini-3.7-flash"}),
        }
        host, graph, _ = wired(rows, catalog=catalog)

        await asyncio.wait_for(host.run_turn(1), 2)
        await asyncio.wait_for(host.run_turn(2), 2)

        assert [observation.model for observation in graph.observations] == [
            "deepseek-flash",
            "gemini-3.7-flash",
        ]
        assert host.stats.config_normalized == 1
        assert warnings == ["host_config_normalized"]

    async def test_the_normalization_warns_once_per_stored_config_state(
        self,
        wired: _Build,
        monkeypatch: pytest.MonkeyPatch,
        withdrawn_model: tuple[str, ModelCatalog],
    ) -> None:
        """Repeated wakes on the same stale pin stay quiet until the stored
        config changes; the counter still sees every normalized wake."""
        model, catalog = withdrawn_model
        import services.agent_runner.agent_host.host as host_mod

        warnings: list[str] = []

        def _record_warning(_message: str, *, event: str, **_details: object) -> None:
            warnings.append(event)

        monkeypatch.setattr(host_mod.logger, "warning", _record_warning)
        rows = {1: _Row(overlay={"llm_model": model})}
        host, graph, _ = wired(rows, catalog=catalog)

        await asyncio.wait_for(host.run_turn(1), 2)
        await asyncio.wait_for(host.run_turn(1), 2)
        rows[1] = _Row(overlay={"llm_model": "deepseek-flash"})
        await asyncio.wait_for(host.run_turn(1), 2)

        assert warnings == ["host_config_normalized"]
        assert host.stats.config_normalized == 2
        assert [observation.model for observation in graph.observations] == ["deepseek-flash"] * 3

    async def test_an_unregistered_pin_is_rejected_not_normalized(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No silent rescue for an id the registry does not know: the wake keeps
        taking the reject path (the real validator), and no turn starts."""
        import services.agent_runner.agent_host.runtime as runtime_mod

        monkeypatch.setattr(runtime_mod, "validate_model_config", validate_model_config)
        host, graph, _ = wired({1: _Row(overlay={"llm_model": "no-such-model-xyz"})})

        await asyncio.wait_for(host.run_turn(1), 2)

        assert graph.observations == []
        assert host.stats.config_rejected == 1
        assert host.stats.config_normalized == 0


class TestBounds:
    async def test_disabled_turn_limit_admits_sixty_four_agents(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Waiting on another agent's model must not consume a global turn slot."""
        monkeypatch.setattr(settings.daemon, "host_max_concurrent_turns", 0)
        agents = range(1, 65)
        host, graph, _ = wired({agent_id: _Row() for agent_id in agents})
        for agent_id in agents:
            graph.gate(agent_id)

        # Exercise admission directly so a pre-fix Semaphore(0) can be
        # cancelled during cleanup without bypassing run_turn's resource shield.
        tasks = [
            asyncio.create_task(host._run_turn(agent_id, resources=HostedTurnResources()))
            for agent_id in agents
        ]
        try:
            await asyncio.wait_for(
                asyncio.gather(*(graph.arrival(agent_id).wait() for agent_id in agents)), 2
            )
            assert host._in_flight == set(agents)
        finally:
            for gate in graph.gates.values():
                gate.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_completed_burst_restores_the_warm_cache_bound(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Active runtimes survive a burst; completion releases excess warm entries."""
        monkeypatch.setattr(settings.daemon, "host_max_concurrent_turns", 64)
        monkeypatch.setattr(settings.daemon, "host_agent_cache_size", 32)
        agents = range(1, 65)
        host, graph, _ = wired({agent_id: _Row() for agent_id in agents})
        for agent_id in agents:
            graph.gate(agent_id)

        tasks = [asyncio.create_task(host.run_turn(agent_id)) for agent_id in agents]
        try:
            await asyncio.wait_for(
                asyncio.gather(*(graph.arrival(agent_id).wait() for agent_id in agents)), 2
            )
            assert set(host._runtimes) == set(agents)
        finally:
            for gate in graph.gates.values():
                gate.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 2)

        assert host._in_flight == set()
        assert len(host._runtimes) <= 32

    async def test_long_running_completion_refreshes_cache_recency(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Active time is not idle time; the last completion becomes most recent."""
        monkeypatch.setattr(settings.daemon, "host_agent_cache_size", 2)
        host, graph, _ = wired({agent_id: _Row() for agent_id in (1, 2, 3)})
        graph.gate(1)
        task = asyncio.create_task(host.run_turn(1))
        try:
            await asyncio.wait_for(graph.arrival(1).wait(), 2)
            host._runtimes[1].last_used -= settings.daemon.host_agent_idle_ttl_seconds + 1
            await asyncio.wait_for(host.run_turn(2), 2)
            assert 1 in host._runtimes, "an active runtime must survive idle eviction"
        finally:
            graph.gates[1].set()
            await asyncio.wait_for(task, 2)

        assert list(host._runtimes) == [2, 1], "completion must refresh idle time and LRU"
        await asyncio.wait_for(host.run_turn(3), 2)
        assert set(host._runtimes) == {1, 3}, "the earlier completion is evicted first"

    async def test_concurrent_turns_are_capped(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicitly configured admission bound still queues excess agents."""
        from base.config import settings

        monkeypatch.setattr(settings.daemon, "host_max_concurrent_turns", 2)
        rows = {i: _Row() for i in (1, 2, 3)}
        host, graph, _ = wired(rows)
        for i in (1, 2, 3):
            graph.gate(i)
            graph.arrival(i)

        tasks = [asyncio.create_task(host.run_turn(i)) for i in (1, 2, 3)]
        await asyncio.wait_for(graph.arrival(1).wait(), 2)
        await asyncio.wait_for(graph.arrival(2).wait(), 2)
        for _ in range(8):
            await asyncio.sleep(0)
        assert not graph.arrived[3].is_set(), "the third turn must wait for a slot"

        graph.gates[1].set()
        await asyncio.wait_for(graph.arrival(3).wait(), 2)
        graph.gates[2].set()
        graph.gates[3].set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert host.stats.turns_started == 3

    async def test_a_waiting_turn_holds_no_database_claim(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Admission precedes the runtime claim: an agent queued for a slot has
        touched no row, runtime or checkpoint — and the queue is visible while
        it waits (task #3584 review: lock the pre-claim property in)."""
        from base.config import settings

        monkeypatch.setattr(settings.daemon, "host_max_concurrent_turns", 1)
        host, graph, _ = wired({1: _Row(), 2: _Row()})
        graph.gate(1)
        graph.arrival(1)

        tasks: list[asyncio.Task[None]] = [asyncio.create_task(host.run_turn(1))]
        try:
            await asyncio.wait_for(graph.arrival(1).wait(), 2)
            tasks.append(asyncio.create_task(host.run_turn(2)))
            await poll_until_async(
                lambda: host.admission.waiting == 1,
                timeout=2,
                what="agent 2 queued at the admission gate",
            )

            assert not graph.arrival(2).is_set(), "a queued turn must not reach the graph"
            assert 2 not in host._in_flight, "a queued turn holds no runtime claim"
            assert host.stats.turns_started == 1, "only the admitted turn started"
        finally:
            graph.gates[1].set()
            await asyncio.wait_for(asyncio.gather(*tasks), 2)

        assert host.stats.turns_started == 2
        assert graph.arrived[2].is_set(), "the queued turn runs once a slot frees"

    async def test_the_lru_cap_evicts_the_least_recently_used(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from base.config import settings

        monkeypatch.setattr(settings.daemon, "host_agent_cache_size", 2)
        host, _, _ = wired({i: _Row() for i in (1, 2, 3)})
        for i in (1, 2):
            await asyncio.wait_for(host.run_turn(i), 2)
        await asyncio.wait_for(host.run_turn(1), 2)  # 1 is now the most recent
        await asyncio.wait_for(host.run_turn(3), 2)
        assert set(host._runtimes) == {1, 3}, "2 was least recently used"

    async def test_the_idle_ttl_drops_a_silent_agent(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The size cap alone keeps a long-silent agent warm forever on a
        lightly loaded runner; the TTL is the other half."""
        from base.config import settings

        host, _, _ = wired({i: _Row() for i in (1, 2)})
        await asyncio.wait_for(host.run_turn(1), 2)
        host._runtimes[1].last_used -= settings.daemon.host_agent_idle_ttl_seconds + 1
        await asyncio.wait_for(host.run_turn(2), 2)
        assert set(host._runtimes) == {2}, "1 aged out; 2 was just used"


class TestSchedulerIntegration:
    async def test_crash_event_carries_the_hosts_turn_config_fingerprint(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        overlay: dict[str, object] = {"llm_model": "broken-model-config"}
        host, _, _ = wired({1: _Row(overlay=overlay)})
        records: list[dict[str, object]] = []

        def _capture(_msg: str, **kw: object) -> None:
            records.append(kw)

        async def _explode(
            _agent_id: int, _fingerprint: str, _model: str, *, incarnation: RuntimeIncarnation
        ) -> None:
            raise ValueError("runtime build failed")

        monkeypatch.setattr(dispatcher.logger, "exception", _capture)
        monkeypatch.setattr(host, "_runtime_for", _explode)
        sched = TurnScheduler(host.run_turn, config_fingerprint=host.turn_fingerprints.get)

        sched.wake(1)
        for _ in range(8):
            await asyncio.sleep(0)

        report = next(r for r in records if r.get("event") == "host_turn_crashed")
        assert report["exception_type"] == "ValueError"
        assert report["config_fingerprint"] == _config_fingerprint(overlay, None)

    async def test_a_wake_race_during_a_hosted_turn_still_runs_the_agent(
        self, wired: _Build
    ) -> None:
        """The dispatcher's wake-pending flag and the host's turn loop have to
        compose: a wake landing while a turn is in flight must produce another
        turn, not a lost one. `test_turn_dispatcher.py` proves the flag against a
        stub `run_turn`; this proves it against the real one.
        """
        host, graph, _ = wired({1: _Row()})
        graph.gate(1)
        sched = TurnScheduler(host.run_turn)

        sched.wake(1)
        await asyncio.wait_for(graph.arrival(1).wait(), 2)
        graph.arrived[1].clear()
        # The wake lands while the turn is parked inside the graph.
        sched.wake(1)
        graph.gates[1].set()
        await asyncio.wait_for(graph.arrival(1).wait(), 2)
        graph.gates[1].set()
        for _ in range(8):
            await asyncio.sleep(0)
        assert len(graph.observations) >= 2, "the wake during the turn must be served"
        await sched.aclose()
