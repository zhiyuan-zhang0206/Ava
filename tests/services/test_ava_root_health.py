"""services.ava_root.health: one probe round — counting, policy, deferral.

Policy tests drive the monitor with a stub supervisor and a probe whose verdict
the test flips; two integration tests drive it against a real `Supervisor`
replacing real generations, exercising the restart verb seam and probe
confirmation end to end.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import pytest

from services.ava_root import health as health_mod
from services.ava_root.health import HealthConfig, HealthMonitor
from services.ava_root.manifest import RestartPolicy, UnitManifest, UnitRegistry, UnknownUnitError
from services.ava_root.probes import ProbeRegistry
from services.ava_root.supervisor import Supervisor
from shared.daemon_health import DaemonProbe
from shared.proc_tree import OwnedProcess


class _Recorder:
    """Stands in for shared.log.logger; records every structured call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def warning(self, message: str, **extra: object) -> None:
        self.calls.append({"message": message, **extra})

    def events(self, name: str) -> list[dict[str, object]]:
        return [c for c in self.calls if c.get("event") == name]


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    import shared.log as shared_log

    rec = _Recorder()
    monkeypatch.setattr(shared_log, "logger", rec)
    return rec


class StubSupervisor:
    """RevivalHost stand-in: records restart calls; deferral answers from a map.

    `unknown_units` mimic the real supervisor's `revival_deferral` raise for
    units outside its registry (the static probe path).
    """

    def __init__(
        self,
        deferrals: dict[str, str] | None = None,
        *,
        unknown_units: set[str] | None = None,
    ) -> None:
        self.restart_calls: list[str] = []
        self.deferrals: dict[str, str] = dict(deferrals or {})
        self.unknown_units: set[str] = set(unknown_units or ())
        self.on_restart: Callable[[], None] | None = None
        self.generation = (OwnedProcess(42, 100.0, None), 0.0)

    async def restart(self, unit_id: str) -> dict[str, object]:
        self.restart_calls.append(unit_id)
        if self.on_restart is not None:
            self.on_restart()
        return {"verb": "restart", "units": []}

    def health_generation(self, unit_id: str) -> tuple[OwnedProcess, float]:
        if unit_id in self.unknown_units:
            raise UnknownUnitError(f"unknown unit {unit_id!r}")
        return self.generation

    def revival_deferral(self, unit_id: str) -> str | None:
        if unit_id in self.unknown_units:
            raise UnknownUnitError(f"unknown unit {unit_id!r}")
        return self.deferrals.get(unit_id)


class CellProbe:
    """A probe returning one flippable verdict; counts its calls."""

    def __init__(self, verdict: DaemonProbe) -> None:
        self.verdict = verdict
        self.calls = 0

    def __call__(self) -> DaemonProbe:
        self.calls += 1
        return self.verdict


class FakeClock:
    """The `_monotonic` seam: advances a fixed step per call so windows elapse fast."""

    def __init__(self, start: float = 1000.0, step: float = 0.02) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(health_mod, "_monotonic", fake)
    return fake


def _registry(unit_id: str, probe: Callable[[], DaemonProbe]) -> ProbeRegistry:
    registry = ProbeRegistry()
    registry.register(unit_id, probe)
    return registry


def _config(**overrides: Any) -> HealthConfig:
    values: dict[str, Any] = {
        "interval_s": 0.01,
        "startup_grace_s": 0.0,  # Existing policy cases begin after startup grace.
        "verify_deadline_s": 0.05,
        "verify_interval_s": 0.001,
        "failures_before_restart": 1,
        "backoff_base_s": 30.0,
        "backoff_cap_s": 120.0,
        "breaker_rounds": 3,
    }
    values.update(overrides)
    return HealthConfig(**values)


# -- configuration -------------------------------------------------------------


def test_config_validation() -> None:
    with pytest.raises(ValueError, match="failures_before_restart"):
        _config(failures_before_restart=0)
    with pytest.raises(ValueError, match="breaker_rounds"):
        _config(breaker_rounds=1, failures_before_restart=1)
    with pytest.raises(ValueError, match="interval_s"):
        _config(interval_s=0)
    with pytest.raises(ValueError, match="verify_interval_s"):
        _config(verify_interval_s=0)
    with pytest.raises(ValueError, match="verify_deadline_s"):
        _config(verify_deadline_s=0)
    with pytest.raises(ValueError, match="backoff_base_s"):
        _config(backoff_base_s=0)
    with pytest.raises(ValueError, match="backoff_cap_s"):
        _config(backoff_base_s=60.0, backoff_cap_s=10.0)


# -- round policy --------------------------------------------------------------


async def test_verified_restart_resets_state(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="services.ava_root.health")
    probe = CellProbe(DaemonProbe.down("no"))
    stub = StubSupervisor()

    def flip() -> None:
        probe.verdict = DaemonProbe.up("ok")

    stub.on_restart = flip
    monitor = HealthMonitor(stub, _registry("svc", probe), config=_config())
    await monitor.run_round()
    state = monitor.snapshot()["svc"]
    assert stub.restart_calls == ["svc"]
    assert state.last_verdict == "alive"
    assert state.consecutive_failures == 0
    assert state.respawn_attempts == 0
    assert state.next_respawn_at is None
    assert state.breaker_since is None
    assert "restarted, verified alive" in caplog.text


async def test_failed_restart_backs_off_until_due(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="services.ava_root.health")
    probe = CellProbe(DaemonProbe.down("no"))
    stub = StubSupervisor()
    monitor = HealthMonitor(stub, _registry("svc", probe), config=_config(breaker_rounds=5))

    await monitor.run_round()  # 1: the restart attempt is not verified
    state = monitor.snapshot()["svc"]
    assert stub.restart_calls == ["svc"]
    assert state.respawn_attempts == 1
    assert state.consecutive_failures == 1
    armed = cast(float, state.next_respawn_at)
    assert 25.0 < armed - clock.now < 30.5
    assert "restart FAILED" in caplog.text

    await monitor.run_round()  # 2: inside the backoff window — no second attempt
    assert stub.restart_calls == ["svc"]
    assert monitor.snapshot()["svc"].consecutive_failures == 2
    assert "backing off" in caplog.text

    clock.now += 31.0
    await monitor.run_round()  # 3: the deadline passed — a second attempt fires
    assert stub.restart_calls == ["svc", "svc"]
    assert monitor.snapshot()["svc"].respawn_attempts == 2


async def test_breaker_opens_once_then_holds(
    clock: FakeClock, recorder: _Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="services.ava_root.health")
    probe = CellProbe(DaemonProbe.down("no"))
    stub = StubSupervisor()
    monitor = HealthMonitor(
        stub,
        _registry("svc", probe),
        config=_config(backoff_base_s=600.0, backoff_cap_s=600.0),
    )
    await monitor.run_round()  # 1: restart attempt, not verified
    await monitor.run_round()  # 2: backing off
    await monitor.run_round()  # 3: the third non-alive round opens the breaker
    await monitor.run_round()  # 4: held
    assert stub.restart_calls == ["svc"]
    opens = recorder.events("root_restart_breaker_open")
    assert len(opens) == 1
    assert opens[0]["unit"] == "svc"
    assert opens[0]["rounds"] == 3
    assert opens[0]["respawn_attempts"] == 1
    holds = [r for r in caplog.records if "restart held" in str(r.message)]
    assert len(holds) == 2
    assert monitor.snapshot()["svc"].breaker_since is not None


async def test_terminal_verdict_reports_and_resets(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="services.ava_root.health")
    probe = CellProbe(DaemonProbe.down("no"))
    stub = StubSupervisor()
    monitor = HealthMonitor(stub, _registry("svc", probe), config=_config(breaker_rounds=5))
    await monitor.run_round()  # leaves failure state behind
    assert monitor.snapshot()["svc"].consecutive_failures == 1

    probe.verdict = DaemonProbe.port_taken("alien daemon")
    await monitor.run_round()
    state = monitor.snapshot()["svc"]
    assert state.consecutive_failures == 0
    assert state.respawn_attempts == 0
    assert state.next_respawn_at is None
    assert state.breaker_since is None
    assert state.last_verdict == "port-taken"
    assert stub.restart_calls == ["svc"]  # only the first round's attempt
    assert "NOT REVIVABLE" in caplog.text


async def test_probe_raise_is_unavailable_and_round_continues(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="services.ava_root.health")

    def raising() -> DaemonProbe:
        raise RuntimeError("boom")

    registry = ProbeRegistry()
    registry.register("unit-a", raising)
    registry.register("unit-b", CellProbe(DaemonProbe.up("ok")))
    stub = StubSupervisor()
    monitor = HealthMonitor(stub, registry, config=_config(breaker_rounds=5))
    await monitor.run_round()
    snapshot = monitor.snapshot()
    assert stub.restart_calls == []  # no observed failure: no restart authority
    assert snapshot["unit-a"].last_verdict == "unavailable"
    assert snapshot["unit-a"].last_detail.startswith("probe raised RuntimeError")
    assert snapshot["unit-b"].last_verdict == "alive"  # the round continued
    assert "probe raised" in caplog.text


async def test_unresolvable_probe_is_no_action(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="services.ava_root.health")
    registry = ProbeRegistry()
    registry.register_ref("svc", "tmp_ghost_health_module:probe")
    stub = StubSupervisor()
    monitor = HealthMonitor(stub, registry, config=_config())
    await monitor.run_round()
    assert stub.restart_calls == []
    assert "no resolvable probe" in caplog.text


async def test_snapshot_is_a_copy(clock: FakeClock) -> None:
    probe = CellProbe(DaemonProbe.down("no"))
    stub = StubSupervisor({"svc": "policy never"})
    monitor = HealthMonitor(stub, _registry("svc", probe), config=_config())
    await monitor.run_round()
    taken = monitor.snapshot()
    taken["svc"].consecutive_failures = 99
    assert monitor.snapshot()["svc"].consecutive_failures == 1


# -- deferral: expected stop vs everything else --------------------------------


async def test_expected_stop_counts_nothing_and_alerts_nothing(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="services.ava_root.health")
    probe = CellProbe(DaemonProbe.down("no"))
    stub = StubSupervisor({"svc": "held down"})
    monitor = HealthMonitor(stub, _registry("svc", probe), config=_config())
    for _ in range(4):
        await monitor.run_round()
    state = monitor.snapshot()["svc"]
    assert stub.restart_calls == []
    assert state.consecutive_failures == 0
    assert state.breaker_since is None
    assert state.last_verdict == "down"  # the verdict still lands in the snapshot
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == []
    assert "expected (operator stop)" in caplog.text


async def test_inflight_retry_counts_to_breaker_but_never_acts(
    clock: FakeClock, recorder: _Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="services.ava_root.health")
    probe = CellProbe(DaemonProbe.down("no"))
    stub = StubSupervisor({"svc": "already scheduled"})
    monitor = HealthMonitor(stub, _registry("svc", probe), config=_config())
    await monitor.run_round()  # 1: deferred
    await monitor.run_round()  # 2: deferred, still counting
    await monitor.run_round()  # 3: the breaker opens despite the deferral
    state = monitor.snapshot()["svc"]
    assert stub.restart_calls == []
    assert state.consecutive_failures == 3
    assert state.breaker_since is not None
    assert len(recorder.events("root_restart_breaker_open")) == 1
    assert "restart deferred (already scheduled)" in caplog.text


async def test_policy_never_counts_and_surfaces_via_breaker(
    clock: FakeClock, recorder: _Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="services.ava_root.health")
    probe = CellProbe(DaemonProbe.down("no"))
    stub = StubSupervisor({"svc": "policy never"})
    monitor = HealthMonitor(
        stub,
        _registry("svc", probe),
        config=_config(backoff_base_s=600.0, backoff_cap_s=600.0),
    )
    for _ in range(3):
        await monitor.run_round()
    state = monitor.snapshot()["svc"]
    assert stub.restart_calls == []
    assert state.consecutive_failures == 3
    assert len(recorder.events("root_restart_breaker_open")) == 1
    assert "policy never" in caplog.text


async def test_out_of_tree_unit_counts_and_surfaces_via_breaker(
    clock: FakeClock, recorder: _Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    """A registered probe for a unit outside the tree (static path — e.g. the
    launchd-owned permissions helper): counted, surfaced, never restarted."""
    caplog.set_level(logging.DEBUG, logger="services.ava_root.health")
    probe = CellProbe(DaemonProbe.down("lwcr-stuck: job state=spawn failed"))
    stub = StubSupervisor(unknown_units={"permissions-helper"})
    monitor = HealthMonitor(stub, _registry("permissions-helper", probe), config=_config())
    for _ in range(3):
        await monitor.run_round()
    state = monitor.snapshot()["permissions-helper"]
    assert stub.restart_calls == []
    assert state.consecutive_failures == 3
    assert state.breaker_since is not None
    opens = recorder.events("root_restart_breaker_open")
    assert len(opens) == 1
    assert "lwcr-stuck" in cast("str", opens[0]["detail"])
    assert "no revival verb" in caplog.text


async def test_deferral_clears_then_the_round_acts(clock: FakeClock) -> None:
    probe = CellProbe(DaemonProbe.down("no"))
    stub = StubSupervisor({"svc": "already scheduled"})
    monitor = HealthMonitor(stub, _registry("svc", probe), config=_config(breaker_rounds=5))
    await monitor.run_round()
    assert stub.restart_calls == []
    stub.deferrals.clear()
    await monitor.run_round()
    assert stub.restart_calls == ["svc"]
    assert monitor.snapshot()["svc"].respawn_attempts == 1


# -- the gate seam -------------------------------------------------------------


async def test_gate_decline_resets_and_later_round_acts(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="services.ava_root.health")
    probe = CellProbe(DaemonProbe.down("no"))
    stub = StubSupervisor()
    allowed = {"value": False}
    monitor = HealthMonitor(
        stub,
        _registry("svc", probe),
        config=_config(breaker_rounds=5),
        gate=lambda: (allowed["value"], "paused"),
    )
    await monitor.run_round()
    await monitor.run_round()
    state = monitor.snapshot()["svc"]
    assert stub.restart_calls == []
    assert state.consecutive_failures == 0  # a decline is "not yet allowed"
    assert "not restarting this round" in caplog.text

    allowed["value"] = True
    await monitor.run_round()
    assert stub.restart_calls == ["svc"]


# -- the round loop ------------------------------------------------------------


async def test_start_stop_loop(clock: FakeClock) -> None:
    probe = CellProbe(DaemonProbe.up("ok"))
    stub = StubSupervisor()
    monitor = HealthMonitor(stub, _registry("svc", probe), config=_config(interval_s=0.01))
    await monitor.start()
    with pytest.raises(RuntimeError, match="already started"):
        await monitor.start()
    await asyncio.sleep(0.05)
    await monitor.stop()
    assert probe.calls >= 1
    await monitor.stop()  # a second stop is a no-op


# -- integration against the real supervisor -----------------------------------

_SLEEP_FOREVER = [sys.executable, "-c", "import time; time.sleep(60)"]

StartFactory = Callable[..., Awaitable[Supervisor]]


def _unit(unit_id: str) -> UnitManifest:
    return UnitManifest(
        id=unit_id,
        exec=tuple(_SLEEP_FOREVER),
        restart=RestartPolicy.ALWAYS,
        attach="root",
    )


@pytest.fixture
async def started(short_tmp: Path) -> AsyncIterator[StartFactory]:
    created: list[Supervisor] = []

    async def factory(units: list[UnitManifest]) -> Supervisor:
        supervisor = Supervisor(UnitRegistry(units), run_dir=short_tmp)
        created.append(supervisor)
        await supervisor.start()
        return supervisor

    yield factory
    for supervisor in created:
        await supervisor.shutdown()


async def test_integration_probe_confirmed_restart_replaces_generation(
    started: StartFactory,
) -> None:
    supervisor = await started([_unit("svc")])
    first = supervisor._units["svc"].generation
    assert first is not None
    first_pid = first.proc.pid

    def probe() -> DaemonProbe:
        generation = supervisor._units["svc"].generation
        if generation is not None and generation.proc.pid != first_pid:
            return DaemonProbe.up("new generation serving")
        return DaemonProbe.down("wedged")

    monitor = HealthMonitor(
        supervisor,
        _registry("svc", probe),
        config=_config(verify_deadline_s=1.0),
    )
    await monitor.run_round()
    state = monitor.snapshot()["svc"]
    assert state.last_verdict == "alive"
    assert state.consecutive_failures == 0
    current = supervisor._units["svc"].generation
    assert current is not None and current.proc.pid != first_pid
    assert supervisor._units["svc"].restart_count == 1


async def test_integration_unconfirmed_restart_backs_off(
    started: StartFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="services.ava_root.health")
    supervisor = await started([_unit("svc")])
    first = supervisor._units["svc"].generation
    assert first is not None
    first_pid = first.proc.pid

    def probe() -> DaemonProbe:
        return DaemonProbe.down("still wedged")

    monitor = HealthMonitor(supervisor, _registry("svc", probe), config=_config())
    await monitor.run_round()
    state = monitor.snapshot()["svc"]
    assert state.consecutive_failures == 1
    assert state.next_respawn_at is not None
    assert "restart FAILED" in caplog.text
    second = supervisor._units["svc"].generation
    assert second is not None and second.proc.pid != first_pid

    await monitor.run_round()  # inside the backoff window — no second replacement
    assert supervisor._units["svc"].restart_count == 1
    assert supervisor._units["svc"].generation is second
    assert "backing off" in caplog.text


async def test_health_snapshot_exposes_the_status_view(clock: FakeClock) -> None:
    probe = CellProbe(DaemonProbe.down("no"))
    stub = StubSupervisor()
    monitor = HealthMonitor(stub, _registry("svc", probe), config=_config(breaker_rounds=2))
    await monitor.run_round()  # a failed restart arms the backoff
    entry = cast("dict[str, object]", monitor.health_snapshot()["svc"])
    assert entry["consecutive_failures"] == 1
    assert entry["respawn_attempts"] == 1
    assert entry["breaker_open"] is False
    assert entry["breaker_for_s"] is None
    remaining = entry["next_restart_in_s"]
    assert remaining is not None and cast(float, remaining) > 0
    assert entry["last_verdict"] == "down"

    await monitor.run_round()  # second round opens the breaker (breaker_rounds=2)
    entry = cast("dict[str, object]", monitor.health_snapshot()["svc"])
    assert entry["breaker_open"] is True
    age = entry["breaker_for_s"]
    assert age is not None and cast(float, age) >= 0.0


async def test_initial_down_observation_does_not_replace_starting_generation(
    tmp_path: Path,
) -> None:
    unit = UnitManifest(
        "starting",
        (sys.executable, "-c", "import time; time.sleep(60)"),
        RestartPolicy.ALWAYS,
        "root",
    )
    owner = Supervisor(UnitRegistry([unit]), run_dir=tmp_path)
    await owner.start()
    original = cast("list[dict[str, object]]", (await owner.status())["units"])[0]["pid"]
    probe = CellProbe(DaemonProbe.down("frontend is still building"))
    monitor = HealthMonitor(owner, _registry("starting", probe), config=_config(startup_grace_s=30))
    try:
        await monitor.run_round()
        current = cast("list[dict[str, object]]", (await owner.status())["units"])[0]["pid"]
        assert current == original, "an unready first observation replaced the initial generation"
        assert monitor.snapshot()["starting"].last_verdict == "down"
        assert monitor.snapshot()["starting"].consecutive_failures == 0
    finally:
        await owner.shutdown()


async def test_first_alive_ends_generation_startup_grace(clock: FakeClock) -> None:
    stub = StubSupervisor()
    stub.generation = (OwnedProcess(42, 100.0, None), clock.now)
    probe = CellProbe(DaemonProbe.up("ready"))
    monitor = HealthMonitor(stub, _registry("svc", probe), config=_config(startup_grace_s=180))
    await monitor.run_round()
    assert monitor.snapshot()["svc"].generation_ready
    probe.verdict = DaemonProbe.down("failed after readiness")
    await monitor.run_round()
    assert stub.restart_calls == ["svc"]


async def test_each_new_generation_has_bounded_startup_grace(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="services.ava_root.health")
    stub = StubSupervisor()
    stub.generation = (OwnedProcess(42, 100.0, None), clock.now)
    probe = CellProbe(DaemonProbe.down("building"))
    monitor = HealthMonitor(stub, _registry("svc", probe), config=_config(startup_grace_s=45))
    await monitor.run_round()
    assert not stub.restart_calls
    assert monitor.snapshot()["svc"].consecutive_failures == 0
    clock.now += 46

    def replace_generation() -> None:
        stub.generation = (OwnedProcess(42 + len(stub.restart_calls), clock.now, None), clock.now)

    stub.on_restart = replace_generation
    await monitor.run_round()
    assert stub.restart_calls == ["svc"]
    # Existing restart backoff expires, but this newborn generation is still initializing.
    clock.now += 31
    await monitor.run_round()
    assert stub.restart_calls == ["svc"]
    assert monitor.snapshot()["svc"].consecutive_failures == 1
    clock.now += 16
    await monitor.run_round()
    assert stub.restart_calls == ["svc", "svc"]
    assert not monitor.snapshot()["svc"].generation_ready
    assert "replacement is initializing" in caplog.text
    assert "restart FAILED" not in caplog.text


async def test_observation_cannot_arm_another_generation(clock: FakeClock) -> None:
    stub = StubSupervisor()
    stub.generation = (OwnedProcess(42, 100.0, None), clock.now)

    def changed_during_probe() -> DaemonProbe:
        stub.generation = (OwnedProcess(43, 101.0, None), clock.now)
        return DaemonProbe.up("response from the preceding generation")

    monitor = HealthMonitor(
        stub, _registry("svc", changed_during_probe), config=_config(startup_grace_s=180)
    )
    await monitor.run_round()
    state = monitor.snapshot()["svc"]
    assert state.last_verdict == "unavailable"
    assert not state.generation_ready
    assert not stub.restart_calls


async def test_startup_budgets_are_per_unit(clock: FakeClock) -> None:
    stub = StubSupervisor()
    stub.generation = (OwnedProcess(42, 100.0, None), clock.now - 60)
    registry = ProbeRegistry()
    registry.register("core", lambda: DaemonProbe.down("starting"))
    registry.register("extra", lambda: DaemonProbe.down("starting"))
    monitor = HealthMonitor(
        stub, registry, config=_config(), startup_graces={"core": 180, "extra": 45}
    )
    await monitor.run_round()
    assert stub.restart_calls == ["extra"]
    assert monitor.snapshot()["core"].consecutive_failures == 0


def test_startup_grace_rejects_unbounded_or_negative_values() -> None:
    for invalid in (-1.0, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="startup_grace"):
            _config(startup_grace_s=invalid)
