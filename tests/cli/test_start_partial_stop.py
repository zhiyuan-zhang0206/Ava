"""A proven pre-application start can stop without a nonexistent DB drain."""

from collections.abc import Callable
from pathlib import Path

import pytest

from cli.commands import _temporary_stop as stop
from cli.start_identity import IdentityInput, mark_phase, prepare_identity
from shared import cluster, paths, start_serving
from shared.hold_driver import HoldDriver
from shared.machine import MachineRoles


def _port_always_free(_port: int) -> bool:
    return True


@pytest.fixture
def partial_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setattr(paths, "ava_home", lambda: home)
    monkeypatch.setattr(cluster, "_port_free", _port_always_free)
    prepare_identity(
        IdentityInput(
            home,
            tmp_path / "registry.json",
            tmp_path,
            False,
            frozenset({"gateway", "agent-runner"}),
            {"AVA_MACHINE_NAME": "partial"},
        )
    )
    monkeypatch.setattr("cli.commands._maintenance_stop.require_no_terminals", lambda: None)
    return home


def test_configured_without_application_evidence_is_proven_unstarted(partial_home: Path) -> None:
    assert stop._require_unstarted_initialization() is True


@pytest.mark.parametrize("phase", ["provisioned", "ready"])
def test_progressed_start_must_use_ordinary_drain(partial_home: Path, phase: str) -> None:
    mark_phase(partial_home, phase)
    assert stop._require_unstarted_initialization() is False


@pytest.mark.parametrize("evidence", ["serving", "manifest", "custody", "locked-root"])
def test_any_application_evidence_refuses_partial_shortcut(
    partial_home: Path, evidence: str
) -> None:
    from services.ava_root.singleton import acquire_instance_lock, release_instance_lock

    fd = None
    if evidence == "serving":
        start_serving.begin_start()
    elif evidence == "manifest":
        path = paths.root_manifests_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    elif evidence == "custody":
        path = paths.root_run_dir() / "custody"
        path.mkdir(parents=True)
        (path / "pending.json").write_text("{}")
    else:
        fd = acquire_instance_lock(paths.root_run_dir())
    try:
        with pytest.raises(RuntimeError):
            stop._require_unstarted_initialization()
    finally:
        if fd is not None:
            release_instance_lock(fd)


def test_partial_stop_uses_native_cleanup_without_database_drain(
    partial_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps: list[str] = []

    def _fake_stop_plan(
        *, preserve_sessions: frozenset[str], keep_browser: bool, keep_infra: bool
    ) -> tuple[MachineRoles, frozenset[str], frozenset[str]]:
        return frozenset({"gateway"}), frozenset(), frozenset()

    def _fake_pause_agents(
        timeout: float = 0, *, driver: HoldDriver | None = None, reap: bool = False
    ) -> None:
        pytest.fail("unstarted home has no work to drain")

    def _fake_services_phase_action(
        *, preserved: frozenset[str], deadline: float
    ) -> Callable[[], object]:
        return lambda: steps.append("root")

    def _fake_stop_browser(_deadline: float) -> None:
        steps.append("browser")

    def _fake_stop_extras(_deadline: float) -> None:
        steps.append("extras")

    def _fake_stop_data_plane(timeout: float, *, save: bool = True) -> list[str]:
        steps.append("native")
        return []

    monkeypatch.setattr("shared.proc.hosting_exec_domain", lambda: None)
    monkeypatch.setattr("shared.proc.hosting_supervised_session", lambda: None)
    monkeypatch.setattr(stop, "_stop_plan", _fake_stop_plan)
    monkeypatch.setattr(stop, "pause_agents", _fake_pause_agents)
    monkeypatch.setattr(stop, "_services_phase_action", _fake_services_phase_action)
    monkeypatch.setattr(stop, "_stop_browser", _fake_stop_browser)
    monkeypatch.setattr(stop, "_stop_extras", _fake_stop_extras)
    monkeypatch.setattr(stop, "stop_data_plane", _fake_stop_data_plane)
    assert (
        stop.stop(
            require_confirmation=False,
            keep_infra=False,
            preserve_sessions=frozenset(),
            keep_browser=False,
            keep_terminals=False,
            announce=False,
            teardown_extras=True,
            timeout=3,
        )
        == 0
    )
    assert steps == ["root", "browser", "extras", "native"]
