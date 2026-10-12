"""services.supervision.ava_root.health: one probe round — counting, policy, deferral.

Policy tests drive the monitor with a stub supervisor and a probe whose verdict
the test flips; two integration tests drive it against a real `Supervisor`
replacing real generations and exercising the finite restart budget.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import psutil
import pytest

from base.daemon.health import DaemonProbe
from base.host.exit_codes import CODE_BEHIND_MINIMUM_EXIT_CODE
from base.native_process.ownership import OwnedProcess
from services.supervision.ava_root import health as health_mod
from services.supervision.ava_root import manifest as manifest_mod
from services.supervision.ava_root.failure_state import UnitFailureFacts
from services.supervision.ava_root.health import HealthConfig, HealthMonitor
from services.supervision.ava_root.inputs import InputSeal
from services.supervision.ava_root.probes import ProbeRegistry
from services.supervision.ava_root.supervisor import Supervisor


class _Recorder:
    """Stands in for base.log.logger; records every structured call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def info(self, message: str, **extra: object) -> None:
        self.calls.append({"message": message, **extra})

    def warning(self, message: str, **extra: object) -> None:
        self.calls.append({"message": message, **extra})

    def events(self, name: str) -> list[dict[str, object]]:
        return [c for c in self.calls if c.get("event") == name]


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    import base.log as base_log

    rec = _Recorder()
    monkeypatch.setattr(base_log, "logger", rec)
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
        self.failure_facts = UnitFailureFacts(intent_running=True, restart_failed=None)

    async def restart(self, unit_id: str) -> dict[str, object]:
        self.restart_calls.append(unit_id)
        if self.on_restart is not None:
            self.on_restart()
        return {"verb": "restart", "units": []}

    def health_generation(self, unit_id: str) -> tuple[OwnedProcess, float]:
        if unit_id in self.unknown_units:
            raise manifest_mod.UnknownUnitError(f"unknown unit {unit_id!r}")
        return self.generation

    def revival_deferral(self, unit_id: str) -> str | None:
        if unit_id in self.unknown_units:
            raise manifest_mod.UnknownUnitError(f"unknown unit {unit_id!r}")
        return self.deferrals.get(unit_id)

    def unit_failure_facts(self, unit_id: str) -> UnitFailureFacts:
        if unit_id in self.unknown_units:
            raise manifest_mod.UnknownUnitError(f"unknown unit {unit_id!r}")
        return self.failure_facts


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
        "max_restart_attempts": 3,
    }
    values.update(overrides)
    return HealthConfig(**values)


def test_config_validation() -> None:
    with pytest.raises(ValueError, match="max_restart_attempts"):
        _config(max_restart_attempts=0)
    with pytest.raises(ValueError, match="interval_s"):
        _config(interval_s=0)


async def test_restart_uses_one_probe_and_next_round_observes_readiness(clock: FakeClock) -> None:
    probe = CellProbe(DaemonProbe.down("no"))
    stub = StubSupervisor()
    stub.on_restart = lambda: setattr(probe, "verdict", DaemonProbe.up("ok"))
    monitor = HealthMonitor(stub, _registry("svc", probe), config=_config())
    await monitor.run_round()
    assert probe.calls == 1  # No replacement confirmation polling.
    assert stub.restart_calls == ["svc"]
    assert monitor.snapshot()["svc"].last_verdict == "down"
    assert monitor.snapshot()["svc"].restart_attempts == 1
    await monitor.run_round()
    assert probe.calls == 2
    assert monitor.snapshot()["svc"].last_verdict == "alive"
    assert monitor.snapshot()["svc"].restart_attempts == 0


async def test_three_actual_restarts_then_only_observation(clock: FakeClock) -> None:
    probe = CellProbe(DaemonProbe.down("no"))
    stub = StubSupervisor()
    monitor = HealthMonitor(stub, _registry("svc", probe), config=_config())
    for _ in range(8):
        await monitor.run_round()
    assert probe.calls == 8
    assert stub.restart_calls == ["svc"] * 3
    assert monitor.snapshot()["svc"].restart_attempts == 3
    probe.verdict = DaemonProbe.up("repaired")
    await monitor.run_round()
    probe.verdict = DaemonProbe.down("new outage")
    await monitor.run_round()
    assert stub.restart_calls == ["svc"] * 4
    assert monitor.snapshot()["svc"].restart_attempts == 1


async def test_unknown_rounds_do_not_spend_or_replenish_attempts(clock: FakeClock) -> None:
    probe = CellProbe(DaemonProbe.down("no"))
    stub = StubSupervisor()
    monitor = HealthMonitor(stub, _registry("svc", probe), config=_config())
    for _ in range(7):
        probe.verdict = DaemonProbe.unavailable("inspection failed")
        await monitor.run_round()
        probe.verdict = DaemonProbe.down("no")
        await monitor.run_round()
    assert stub.restart_calls == ["svc"] * 3
    assert monitor.snapshot()["svc"].restart_attempts == 3


@pytest.mark.parametrize("deferral", ["policy never", "already scheduled"])
async def test_deferred_down_reports_without_spending_attempts(
    deferral: str, clock: FakeClock, recorder: _Recorder
) -> None:
    probe = CellProbe(DaemonProbe.down("no"))
    stub = StubSupervisor({"svc": deferral})
    monitor = HealthMonitor(stub, _registry("svc", probe), config=_config())
    for _ in range(7):
        await monitor.run_round()
    assert stub.restart_calls == []
    assert monitor.snapshot()["svc"].restart_attempts == 0
    assert len(recorder.events("root_unit_not_revivable")) == 7
    stub.deferrals.clear()
    await monitor.run_round()
    assert stub.restart_calls == ["svc"]


async def test_probe_only_unit_reports_without_restarts(
    clock: FakeClock, recorder: _Recorder
) -> None:
    stub = StubSupervisor(unknown_units={"helper"})
    monitor = HealthMonitor(
        stub, _registry("helper", CellProbe(DaemonProbe.down("missing"))), config=_config()
    )
    for _ in range(5):
        await monitor.run_round()
    assert not stub.restart_calls
    assert monitor.snapshot()["helper"].restart_attempts == 0
    assert len(recorder.events("root_unit_not_revivable")) == 5


async def test_expected_stop_resets_attempts_and_alerts_nothing(
    clock: FakeClock, recorder: _Recorder
) -> None:
    stub = StubSupervisor()
    monitor = HealthMonitor(
        stub, _registry("svc", CellProbe(DaemonProbe.down("no"))), config=_config()
    )
    await monitor.run_round()
    stub.deferrals["svc"] = "held down"
    stub.failure_facts = UnitFailureFacts(intent_running=False, restart_failed=None)
    for _ in range(4):
        await monitor.run_round()
    assert stub.restart_calls == ["svc"]
    assert monitor.snapshot()["svc"].restart_attempts == 0
    assert not recorder.calls


async def test_terminal_verdict_reports_without_another_restart(
    clock: FakeClock, recorder: _Recorder
) -> None:
    probe = CellProbe(DaemonProbe.down("no"))
    stub = StubSupervisor()
    monitor = HealthMonitor(stub, _registry("svc", probe), config=_config())
    await monitor.run_round()
    probe.verdict = DaemonProbe.port_taken("alien daemon")
    await monitor.run_round()
    assert monitor.snapshot()["svc"].restart_attempts == 1
    assert stub.restart_calls == ["svc"]
    assert recorder.events("root_unit_not_revivable")[0]["detail"] == "alien daemon"


async def test_probe_raise_is_unavailable_and_round_continues(
    clock: FakeClock, recorder: _Recorder
) -> None:
    def raising() -> DaemonProbe:
        raise RuntimeError("boom")

    registry = _registry("unit-a", raising)
    registry.register("unit-b", CellProbe(DaemonProbe.up("ok")))
    stub = StubSupervisor()
    monitor = HealthMonitor(stub, registry, config=_config())
    await monitor.run_round()
    assert not stub.restart_calls
    assert monitor.snapshot()["unit-a"].last_detail.startswith("probe raised RuntimeError")
    assert monitor.snapshot()["unit-b"].last_verdict == "alive"
    assert recorder.events("root_unit_not_revivable")[0]["unit"] == "unit-a"


async def test_unresolvable_probe_is_no_action(clock: FakeClock) -> None:
    registry = ProbeRegistry()
    registry.register_ref("svc", "tmp_ghost_health:probe")
    stub = StubSupervisor()
    monitor = HealthMonitor(stub, registry, config=_config())
    await monitor.run_round()
    assert not stub.restart_calls
    assert monitor.snapshot()["svc"].last_verdict == "unavailable"


async def test_snapshot_is_a_copy(clock: FakeClock) -> None:
    monitor = HealthMonitor(
        StubSupervisor(), _registry("svc", CellProbe(DaemonProbe.down("no"))), config=_config()
    )
    await monitor.run_round()
    monitor.snapshot()["svc"].restart_attempts = 99
    assert monitor.snapshot()["svc"].restart_attempts == 1


async def test_maintenance_gate_preserves_spent_attempts(clock: FakeClock) -> None:
    probe = CellProbe(DaemonProbe.down("no"))
    stub = StubSupervisor()
    allowed = {"value": True}
    monitor = HealthMonitor(
        stub,
        _registry("svc", probe),
        config=_config(),
        gate=lambda: (allowed["value"], "paused"),
    )
    for _ in range(6):
        allowed["value"] = True
        await monitor.run_round()
        allowed["value"] = False
        await monitor.run_round()
    assert stub.restart_calls == ["svc"] * 3


# -- the round loop ------------------------------------------------------------


async def test_start_stop_loop(clock: FakeClock) -> None:
    async with asyncio.TaskGroup() as tasks:
        probe = CellProbe(DaemonProbe.up("ok"))
        stub = StubSupervisor()
        monitor = HealthMonitor(
            stub, _registry("svc", probe), config=_config(interval_s=0.01), tasks=tasks
        )
        await monitor.start()
        with pytest.raises(RuntimeError, match="already started"):
            await monitor.start()
        await asyncio.sleep(0.05)
        await monitor.stop()
        assert probe.calls >= 1
        await monitor.stop()  # a second stop is a no-op


# -- integration against the real supervisor -----------------------------------

_SLEEP_FOREVER = [sys.executable, "-c", "import time; time.sleep(60)"]
_CHILD = Path(__file__).with_name("child_unit.py")

StartFactory = Callable[..., Awaitable[Supervisor]]


def _unit(unit_id: str) -> manifest_mod.UnitManifest:
    return manifest_mod.UnitManifest(
        id=unit_id,
        exec=tuple(_SLEEP_FOREVER),
        restart=manifest_mod.RestartPolicy.ALWAYS,
        attach="root",
    )


@pytest.fixture
async def started(short_tmp: Path) -> AsyncIterator[StartFactory]:
    created: list[Supervisor] = []

    async def factory(units: list[manifest_mod.UnitManifest]) -> Supervisor:
        supervisor = Supervisor(manifest_mod.UnitRegistry(units), run_dir=short_tmp)
        created.append(supervisor)
        await supervisor.start()
        return supervisor

    yield factory
    for supervisor in created:
        await supervisor.shutdown()


async def _unit_status(supervisor: Supervisor) -> dict[str, Any]:
    return cast("list[dict[str, Any]]", (await supervisor.status())["units"])[0]


async def _wait_stopped(supervisor: Supervisor) -> None:
    async with asyncio.timeout(10):
        while (await _unit_status(supervisor))["state"] != "stopped":
            await asyncio.sleep(0.01)


async def test_real_children_are_replaced_only_three_times(started: StartFactory) -> None:
    supervisor = await started([_unit("svc")])
    first_pid = int((await _unit_status(supervisor))["pid"])
    first = OwnedProcess.capture(psutil.Process(first_pid))
    pids = {first_pid}
    monitor = HealthMonitor(
        supervisor, _registry("svc", CellProbe(DaemonProbe.down("wedged"))), config=_config()
    )
    for _ in range(7):
        await monitor.run_round()
        current = await _unit_status(supervisor)
        assert current["state"] == "running"
        pids.add(int(current["pid"]))
    assert len(pids) == 4
    assert not first.live()
    assert (await _unit_status(supervisor))["restart_count"] == 3
    assert monitor.snapshot()["svc"].restart_attempts == 3


async def test_code_gate_exit_is_not_automatically_restarted(
    started: StartFactory, short_tmp: Path, recorder: _Recorder
) -> None:
    marker = short_tmp / "starts"
    unit = manifest_mod.UnitManifest(
        "svc",
        (sys.executable, str(_CHILD), "code_gate_exit", str(marker)),
        manifest_mod.RestartPolicy.ALWAYS,
        "root",
    )
    supervisor = await started([unit])
    await _wait_stopped(supervisor)
    assert (await _unit_status(supervisor))["last_exit"] == f"exit {CODE_BEHIND_MINIMUM_EXIT_CODE}"
    monitor = HealthMonitor(
        supervisor, _registry("svc", CellProbe(DaemonProbe.down("absent"))), config=_config()
    )
    for _ in range(6):
        await monitor.run_round()
    assert marker.read_text() == "start\n"
    assert (await _unit_status(supervisor))["restart_count"] == 0
    assert monitor.snapshot()["svc"].restart_attempts == 0
    assert len(recorder.events("root_unit_not_revivable")) == 6
    assert "update required" in str(recorder.events("root_unit_not_revivable")[0]["detail"])
    # An explicit operator command still owns another attempt.
    await supervisor.up("svc")
    await _wait_stopped(supervisor)
    assert marker.read_text() == "start\nstart\n"


async def test_failed_replacement_remains_visible_and_budgeted(
    started: StartFactory, short_tmp: Path, recorder: _Recorder
) -> None:
    sealed = short_tmp / "sealed-input"
    sealed.write_text("v1")
    unit = manifest_mod.UnitManifest(
        "svc",
        tuple(_SLEEP_FOREVER),
        manifest_mod.RestartPolicy.ALWAYS,
        "root",
        inputs=(InputSeal.capture(sealed.resolve()),),
    )
    supervisor = await started([unit])
    probe = CellProbe(DaemonProbe.up("serving"))
    monitor = HealthMonitor(supervisor, _registry("svc", probe), config=_config())
    await monitor.run_round()
    sealed.write_text("v2")
    probe.verdict = DaemonProbe.down("not serving")
    for _ in range(7):
        await monitor.run_round()
    entry = cast("list[dict[str, Any]]", (await supervisor.status())["units"])[0]
    assert entry["intent"] == "running" and entry["state"] == "stopped"
    assert entry["restart_failed"]["stage"] == "up"
    assert monitor.snapshot()["svc"].restart_attempts == 3
    assert len(recorder.events("root_restart_failed")) == 1
    assert len(recorder.events("root_unit_failure_state")) == 7


async def test_a_probe_outlasting_its_own_deadline_is_unavailable_and_never_restarts() -> None:
    release = threading.Event()

    def probe() -> DaemonProbe:
        release.wait(timeout=10)
        return DaemonProbe.up("late")

    stub = StubSupervisor()
    monitor = HealthMonitor(stub, _registry("svc", probe), config=_config(probe_timeout_s=0.05))
    try:
        await monitor.run_round()
    finally:
        release.set()
    state = monitor.snapshot()["svc"]
    assert state.last_verdict == "unavailable"
    assert "exceeded 0.05s deadline" in state.last_detail
    assert not stub.restart_calls


async def test_health_snapshot_exposes_simple_attempt_budget(clock: FakeClock) -> None:
    monitor = HealthMonitor(
        StubSupervisor(), _registry("svc", CellProbe(DaemonProbe.down("no"))), config=_config()
    )
    await monitor.run_round()
    entry = cast("dict[str, object]", monitor.health_snapshot()["svc"])
    assert entry["restart_attempts"] == 1
    assert entry["restart_limit_reached"] is False
    assert entry["last_verdict"] == "down"
    for _ in range(3):
        await monitor.run_round()
    entry = cast("dict[str, object]", monitor.health_snapshot()["svc"])
    assert entry["restart_attempts"] == 3
    assert entry["restart_limit_reached"] is True


async def test_initial_down_observation_does_not_replace_starting_generation(
    tmp_path: Path,
) -> None:
    unit = manifest_mod.UnitManifest(
        "starting",
        (sys.executable, "-c", "import time; time.sleep(60)"),
        manifest_mod.RestartPolicy.ALWAYS,
        "root",
    )
    owner = Supervisor(manifest_mod.UnitRegistry([unit]), run_dir=tmp_path)
    await owner.start()
    original = cast("list[dict[str, object]]", (await owner.status())["units"])[0]["pid"]
    probe = CellProbe(DaemonProbe.down("frontend is still building"))
    monitor = HealthMonitor(owner, _registry("starting", probe), config=_config(startup_grace_s=30))
    try:
        await monitor.run_round()
        current = cast("list[dict[str, object]]", (await owner.status())["units"])[0]["pid"]
        assert current == original, "an unready first observation replaced the initial generation"
        assert monitor.snapshot()["starting"].last_verdict == "down"
        assert monitor.snapshot()["starting"].restart_attempts == 0
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
    caplog.set_level(logging.INFO, logger="services.supervision.ava_root.health")
    stub = StubSupervisor()
    stub.generation = (OwnedProcess(42, 100.0, None), clock.now)
    probe = CellProbe(DaemonProbe.down("building"))
    monitor = HealthMonitor(stub, _registry("svc", probe), config=_config(startup_grace_s=45))
    await monitor.run_round()
    assert not stub.restart_calls
    assert monitor.snapshot()["svc"].restart_attempts == 0
    clock.now += 46

    def replace_generation() -> None:
        stub.generation = (OwnedProcess(42 + len(stub.restart_calls), clock.now, None), clock.now)

    stub.on_restart = replace_generation
    await monitor.run_round()
    assert stub.restart_calls == ["svc"]
    # This newborn generation is still initializing; no extra verification loop runs.
    clock.now += 31
    await monitor.run_round()
    assert stub.restart_calls == ["svc"]
    assert monitor.snapshot()["svc"].restart_attempts == 1
    clock.now += 16
    await monitor.run_round()
    assert stub.restart_calls == ["svc", "svc"]
    assert not monitor.snapshot()["svc"].generation_ready
    assert "startup has" in caplog.text
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
    assert monitor.snapshot()["core"].restart_attempts == 0


def test_startup_grace_rejects_unbounded_or_negative_values() -> None:
    for invalid in (-1.0, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="startup_grace"):
            _config(startup_grace_s=invalid)
