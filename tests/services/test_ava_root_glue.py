"""services.ava_root_glue.glue / .drill: the reference and drill assemblies."""

from __future__ import annotations

import os
import sys
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from services.ava_root.health import HealthMonitor
from services.ava_root.manifest import ROOT_ID, RestartPolicy, UnitManifest, UnitRegistry
from services.ava_root.probes import Probe
from services.ava_root.supervisor import Supervisor
from services.ava_root.wiring import WiringContext
from services.ava_root_glue import drill, glue
from shared.config import settings
from shared.daemon_health import DaemonProbe

_SLEEPER = [sys.executable, "-u", "-c", "import time; time.sleep(60)"]


@dataclass(slots=True)
class _Spec:
    session: str
    healthcheck_module: str | None
    identity_probe: Probe | None


def _registry(unit_ids: tuple[str, ...]) -> UnitRegistry:
    manifests = [
        UnitManifest(
            id=unit_id,
            exec=tuple(_SLEEPER),
            restart=RestartPolicy.ALWAYS,
            attach=ROOT_ID,
        )
        for unit_id in unit_ids
    ]
    return UnitRegistry(manifests)


def _context(tmp_path: Path, registry: UnitRegistry) -> WiringContext:
    log_dir = tmp_path / "logs"
    return WiringContext(
        supervisor=Supervisor(registry, run_dir=tmp_path),
        registry=registry,
        run_dir=tmp_path,
        log_dir=log_dir,
    )


@pytest.fixture(autouse=True)
def _helper_gate_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the helper gate's platform input to a non-macOS value so tests are
    deterministic on every host; tests that exercise the helper opt back in."""
    monkeypatch.setattr(glue, "IS_MACOS", False)


async def test_reference_wiring_registers_gated_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(("svc-a",))
    context = _context(tmp_path, registry)
    specs = (
        _Spec("svc-a", "services.healthchecks.fake", lambda: DaemonProbe.up("fine")),
        _Spec("svc-b", None, lambda: DaemonProbe.up("not monitored")),
        _Spec("svc-c", "services.healthchecks.fake", None),
    )
    monkeypatch.setattr(glue, "build_services", lambda: specs)
    await context.supervisor.start()
    try:
        participants = glue.build_wiring(context)
        assert len(participants) == 2
        monitor = participants[0]
        assert isinstance(monitor, HealthMonitor)
        await monitor.run_round()
        health = cast("dict[str, dict[str, object]]", monitor.health_snapshot())
        # svc-b (no healthcheck module) and svc-c (no identity probe) are skipped.
        assert set(health) == {"svc-a"}
        assert health["svc-a"]["last_verdict"] == "alive"
        status = await context.supervisor.status()
        assert "health" in status and "metrics" in status
    finally:
        await context.supervisor.shutdown()


def test_helper_probe_gate_needs_macos_and_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(glue, "IS_MACOS", True)
    monkeypatch.setattr(settings.services, "permissions_helper_enabled", True)
    assert glue._helper_probe_enabled() is True
    monkeypatch.setattr(glue, "IS_MACOS", False)
    assert glue._helper_probe_enabled() is False
    monkeypatch.setattr(glue, "IS_MACOS", True)
    monkeypatch.setattr(settings.services, "permissions_helper_enabled", False)
    assert glue._helper_probe_enabled() is False


async def test_helper_probe_seam_is_gated_and_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The helper rides the static path; a down verdict is surfaced and held —
    the unit sits outside the supervisor's tree, so nothing ever restarts it."""
    registry = _registry(("svc-a",))
    context = _context(tmp_path, registry)
    monkeypatch.setattr(glue, "build_services", lambda: ())
    await context.supervisor.start()
    try:
        # Gate off (the autouse default): no helper entry in the roster.
        monitor = glue.build_wiring(context)[0]
        assert isinstance(monitor, HealthMonitor)
        await monitor.run_round()
        health = cast("dict[str, dict[str, object]]", monitor.health_snapshot())
        assert glue.HELPER_PROBE_UNIT_ID not in health

        # Gate on: registered through the static ref; the round surfaces the
        # down verdict and the round completes (no crash on the out-of-tree
        # deferral, no restart verb reached).
        monkeypatch.setattr(glue, "IS_MACOS", True)
        monkeypatch.setattr(settings.services, "permissions_helper_enabled", True)
        monkeypatch.setattr(glue, "HELPER_PROBE_REF", "wiring_fixture_helper_probe:probe")
        module = types.ModuleType("wiring_fixture_helper_probe")
        setattr(module, "probe", lambda: DaemonProbe.down("lwcr-stuck: job state=spawn failed"))  # noqa: B010 - dynamic module attr
        monkeypatch.setitem(sys.modules, "wiring_fixture_helper_probe", module)
        monitor = glue.build_wiring(context)[0]
        assert isinstance(monitor, HealthMonitor)
        await monitor.run_round()
        health = cast("dict[str, dict[str, object]]", monitor.health_snapshot())
        assert "lwcr-stuck" in cast("str", health[glue.HELPER_PROBE_UNIT_ID]["last_detail"])
    finally:
        await context.supervisor.shutdown()


async def test_static_probe_seam_resolves_lazily(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(("svc-host",))
    context = _context(tmp_path, registry)
    monkeypatch.setattr(glue, "build_services", lambda: ())
    monkeypatch.setattr(glue, "STATIC_PROBES", {"svc-host": "wiring_fixture_probe:probe"})
    module = types.ModuleType("wiring_fixture_probe")
    setattr(module, "probe", lambda: DaemonProbe.up("static"))  # noqa: B010 - dynamic module attr
    monkeypatch.setitem(sys.modules, "wiring_fixture_probe", module)
    await context.supervisor.start()
    try:
        participants = glue.build_wiring(context)
        monitor = participants[0]
        assert isinstance(monitor, HealthMonitor)
        await monitor.run_round()
        health = cast("dict[str, dict[str, object]]", monitor.health_snapshot())
        assert health["svc-host"]["last_verdict"] == "alive"
    finally:
        await context.supervisor.shutdown()


async def test_unresolvable_static_probe_yields_no_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(("svc-host",))
    context = _context(tmp_path, registry)
    monkeypatch.setattr(glue, "build_services", lambda: ())
    monkeypatch.setattr(glue, "STATIC_PROBES", {"svc-host": "no_such_probe_module:probe"})
    await context.supervisor.start()
    try:
        participants = glue.build_wiring(context)
        monitor = participants[0]
        assert isinstance(monitor, HealthMonitor)
        await monitor.run_round()  # no crash; no verdict, no action
        health = cast("dict[str, dict[str, object]]", monitor.health_snapshot())
        assert health["svc-host"]["last_verdict"] is None
    finally:
        await context.supervisor.shutdown()


async def test_drill_wiring_verifies_claims(tmp_path: Path) -> None:
    registry = _registry(("light-a", "light-b"))
    context = _context(tmp_path, registry)
    await context.supervisor.start()
    try:
        participants = drill.build_drill_wiring(context)
        assert len(participants) == 2
        monitor = participants[0]
        assert isinstance(monitor, HealthMonitor)
        await monitor.run_round()
        health = cast("dict[str, dict[str, object]]", monitor.health_snapshot())
        assert health["light-a"]["last_verdict"] == "alive"
        assert health["light-b"]["last_verdict"] == "alive"
        # An operator stop is expected state: reported, never counted.
        await context.supervisor.down("light-b")
        await monitor.run_round()
        health = cast("dict[str, dict[str, object]]", monitor.health_snapshot())
        assert health["light-b"]["last_verdict"] == "down"
        assert health["light-b"]["consecutive_failures"] == 0
    finally:
        await context.supervisor.shutdown()


class _TreeStub:
    def __init__(
        self, view: dict[str, object] | None = None, error: Exception | None = None
    ) -> None:
        self._view = view
        self._error = error

    def tree_view(self) -> dict[str, object]:
        if self._error is not None:
            raise self._error
        assert self._view is not None
        return self._view


def _view(*units: dict[str, object]) -> dict[str, object]:
    return {"root_pid": 1, "units": list(units)}


def test_drill_liveness_probe_reads_the_raw_tree() -> None:
    dead = drill._liveness_probe(
        _TreeStub(_view({"id": "u1", "state": "running", "pid": 999_999_999})), "u1"
    )
    assert dead().verdict.value == "down"  # claimed running, pid not alive

    live = drill._liveness_probe(
        _TreeStub(_view({"id": "u1", "state": "running", "pid": os.getpid()})), "u1"
    )
    assert live().verdict.value == "alive"

    stopped = drill._liveness_probe(
        _TreeStub(_view({"id": "u1", "state": "stopped", "pid": None})), "u1"
    )
    assert stopped().verdict.value == "down"

    missing = drill._liveness_probe(_TreeStub(_view()), "u1")
    assert missing().verdict.value == "down"

    broken = drill._liveness_probe(_TreeStub(error=RuntimeError("no tree")), "u1")
    assert broken().verdict.value == "unavailable"


def test_drill_heartbeat_probe_requires_freshness(tmp_path: Path) -> None:
    heartbeat_dir = tmp_path / "heartbeat"
    heartbeat_dir.mkdir()
    beat = heartbeat_dir / "u1.beat"
    beat.touch()
    probe = drill._liveness_probe(
        _TreeStub(_view({"id": "u1", "state": "running", "pid": os.getpid()})),
        "u1",
        heartbeat_dir=heartbeat_dir,
    )

    fresh = probe()
    assert fresh.verdict.value == "alive"
    assert "heartbeat" in fresh.detail

    stale_at = time.time() - 30.0
    os.utime(beat, (stale_at, stale_at))
    stale = probe()
    assert stale.verdict.value == "down"
    assert "stale" in stale.detail

    beat.unlink()
    assert probe().verdict.value == "alive"  # no heartbeat published -> liveness only
