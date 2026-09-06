"""No OS jobs: model manager states while exercising real desired-unit files."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cli.commands import _gate_systemd as gs
from shared.config import settings


class Manager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.loaded = False
        self.active = False
        self.enabled = False
        self.fail = ""

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        command = args[0]
        if command == self.fail:
            return subprocess.CompletedProcess(args, 1, "", "injected manager failure")
        output = ""
        if command == "show" and "--value" not in args:
            output = (
                f"LoadState={'loaded' if self.loaded else 'not-found'}\n"
                f"ActiveState={'active' if self.active else 'inactive'}\n"
                f"UnitFileState={'enabled' if self.enabled else 'disabled'}\n"
            )
        elif command == "stop":
            self.active = False
        elif command == "enable":
            self.enabled = True
        elif command == "disable":
            self.enabled = False
        elif command == "start":
            self.loaded = self.active = True
        return subprocess.CompletedProcess(args, 0, output, "")


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> Manager:
    manager = Manager()
    monkeypatch.setattr(gs, "_systemctl", manager.run)
    monkeypatch.setattr(settings.general, "os_jobs_enabled", True)
    return manager


def ensure(home: Path, digest: str = "v1") -> None:
    gs.ensure(home, home / "source", str(home / "source/.venv/bin/python"), digest)


def test_initial_registration_and_unchanged_converge(tmp_path: Path, manager: Manager) -> None:
    ensure(tmp_path)
    assert manager.active and manager.enabled
    unit = gs.unit_path(tmp_path)
    before = unit.stat().st_mtime_ns
    manager.calls.clear()
    ensure(tmp_path)
    assert [call[0] for call in manager.calls] == ["show", "show"]
    assert unit.stat().st_mtime_ns == before


def test_code_change_stops_before_replacement_and_restart(
    tmp_path: Path, manager: Manager, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure(tmp_path)
    real_write = gs._write_unit

    def write(path: Path, content: str) -> None:
        assert not manager.active
        real_write(path, content)

    monkeypatch.setattr(gs, "_write_unit", write)
    manager.calls.clear()
    ensure(tmp_path, "v2")
    assert [call[0] for call in manager.calls] == [
        "show",
        "show",
        "stop",
        "daemon-reload",
        "enable",
        "start",
        "show",
    ]
    assert "v2" in gs.unit_path(tmp_path).read_text()


def test_stop_failure_keeps_old_generation_for_next_attempt(
    tmp_path: Path, manager: Manager
) -> None:
    ensure(tmp_path)
    before = gs.unit_path(tmp_path).read_bytes()
    manager.fail = "stop"
    with pytest.raises(RuntimeError, match="injected"):
        ensure(tmp_path, "v2")
    assert gs.unit_path(tmp_path).read_bytes() == before
    assert manager.active
    manager.fail = ""
    ensure(tmp_path, "v2")
    assert "v2" in gs.unit_path(tmp_path).read_text()


@pytest.mark.parametrize("failure", ["daemon-reload", "enable", "start"])
def test_failed_replacement_recovers_on_next_converge(
    tmp_path: Path, manager: Manager, failure: str
) -> None:
    ensure(tmp_path)
    manager.fail = failure
    with pytest.raises(RuntimeError, match="injected"):
        ensure(tmp_path, "v2")
    assert not manager.active
    manager.fail = ""
    ensure(tmp_path, "v2")
    assert manager.active and manager.enabled


def test_missing_manager_leaves_legacy_and_unit_files_untouched(
    tmp_path: Path, manager: Manager
) -> None:
    (tmp_path / "run").mkdir()
    old = tmp_path / "run/gate.pid"
    old.write_text("123")
    manager.fail = "show"
    with pytest.raises(RuntimeError, match="requires a running systemd user manager"):
        ensure(tmp_path)
    assert old.read_text() == "123"
    assert not gs.unit_path(tmp_path).exists()


def test_registration_disabled_never_contacts_manager(
    tmp_path: Path, manager: Manager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.general, "os_jobs_enabled", False)
    ensure(tmp_path)
    assert manager.calls == []
    assert not gs.unit_path(tmp_path).exists()


def test_full_stop_keeps_registration_destroy_removes_it(tmp_path: Path, manager: Manager) -> None:
    ensure(tmp_path)
    assert gs.stop(tmp_path)
    assert not manager.active and manager.enabled
    assert gs.unit_path(tmp_path).exists()
    ensure(tmp_path)
    assert gs.stop(tmp_path, remove=True)
    assert not manager.active and not manager.enabled
    assert not gs.unit_path(tmp_path).exists()


def test_failed_destroy_keeps_file_and_does_not_claim_removal(
    tmp_path: Path, manager: Manager
) -> None:
    ensure(tmp_path)
    manager.fail = "disable"
    with pytest.raises(RuntimeError, match="injected"):
        gs.stop(tmp_path, remove=True)
    assert gs.unit_path(tmp_path).exists()
    assert not manager.active


def test_stop_finds_loaded_unit_even_if_its_fragment_disappeared(
    tmp_path: Path, manager: Manager
) -> None:
    ensure(tmp_path)
    gs.unit_path(tmp_path).unlink()
    assert gs.stop(tmp_path, remove=True)
    assert not manager.active and not manager.enabled


def test_missing_fragment_and_failed_manager_query_do_not_claim_stopped(
    tmp_path: Path, manager: Manager
) -> None:
    ensure(tmp_path)
    gs.unit_path(tmp_path).unlink()
    manager.fail = "show"
    with pytest.raises(RuntimeError, match="Cannot inspect"):
        gs.stop(tmp_path)
    assert manager.active


def test_pure_runner_without_gate_does_not_need_a_user_manager(
    tmp_path: Path, manager: Manager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli.commands._stop_extras import stop_gate_service

    monkeypatch.setattr("shared.platform.IS_MACOS", False)
    monkeypatch.setattr("cli.commands._stop_extras.sys.platform", "linux")
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
    monkeypatch.setattr("shared.machine.is_gateway", lambda: False)
    manager.fail = "show"
    stop_gate_service()
    assert manager.calls == []


def test_unit_identity_literal_paths_and_clean_environment(tmp_path: Path) -> None:
    home = tmp_path / 'home %i $HOME "quoted" \\ end '
    repo = home / "source"
    text = gs.unit_content(home, repo, str(repo / ".venv/bin/python"), "digest")
    assert 'ExecStart=:"/usr/bin/env" "--ignore-environment"' in text
    assert "%%i $HOME" in text
    assert '\\"quoted\\"' in text
    assert "Restart=always\nRestartSec=2" in text
    assert "StartLimitIntervalSec=0" in text
    assert "KillMode=control-group" in text
    assert "WantedBy=default.target" in text
    assert "\\x20" in gs.unit_name(home)
    assert gs.unit_name(tmp_path / "a/.ava") != gs.unit_name(tmp_path / "b/.ava")
    with pytest.raises(ValueError, match="control characters"):
        gs.unit_content(Path("/home/bad\nExecStart=evil"), repo, "python", "digest")


@pytest.mark.parametrize("owned,stopped", [(False, True), (True, False), (True, True)])
def test_legacy_pid_must_be_owned_and_gone_before_start(
    tmp_path: Path, manager: Manager, monkeypatch: pytest.MonkeyPatch, owned: bool, stopped: bool
) -> None:
    (tmp_path / "run").mkdir()
    pidfile = tmp_path / "run/gate.pid"
    pidfile.write_text("123")
    monkeypatch.setattr("shared.proc.process_alive", lambda _: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._converge_gate.gate_pid_is_ours", lambda *_: owned)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._pgbouncer._terminate_verified", lambda *_, **__: stopped)  # pyright: ignore[reportUnknownArgumentType]
    if owned and stopped:
        ensure(tmp_path)
        assert not pidfile.exists()
        assert manager.active
    else:
        with pytest.raises(RuntimeError, match=r"ownership|survived"):
            ensure(tmp_path)
        assert pidfile.exists()
        assert not manager.active
        assert not gs.unit_path(tmp_path).exists()


def test_linux_lifecycle_routes_to_the_same_home_unit(
    tmp_path: Path, manager: Manager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli.commands import _converge_gate as cg
    from cli.commands._converge_spec import ConvergeCtx
    from cli.commands._stop_extras import stop_gate_service

    monkeypatch.setattr(cg.sys, "platform", "linux")
    monkeypatch.setattr("shared.platform.IS_MACOS", False)
    monkeypatch.setattr("shared.platform.IS_WINDOWS", False)
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
    monkeypatch.setattr(cg, "_ensure_app_port", lambda _: 3001)  # pyright: ignore[reportUnknownArgumentType]
    ctx = ConvergeCtx(tmp_path / "source", tmp_path, frozenset({"gateway"}))
    cg.ensure_gate(ctx)
    assert manager.active
    assert cg.probe_gate(tmp_path).supervised
    stop_gate_service()
    assert not manager.active
    assert gs.unit_path(tmp_path).exists()
    cg.ensure_gate(ctx)
    cg.unregister_gate(tmp_path)
    assert not manager.active and not manager.enabled
    assert not gs.unit_path(tmp_path).exists()
