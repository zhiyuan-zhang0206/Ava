"""Permissions-helper process sessions and spawn-chain routing."""

from __future__ import annotations

import inspect
import os
import signal
import sys
from pathlib import Path

import psutil
import pytest

from services.permissions_helper import client
from services.permissions_helper.client import PermissionsHelperError
from shared import helperproc
from shared.helper_chain_guard import parent_chain_intact
from shared.session_record import SessionRecord, pid_starttime_ticks


def _current_process_record(*, generation: str | None = None) -> SessionRecord:
    process = psutil.Process()
    return SessionRecord(
        pid=process.pid,
        create_time=process.create_time(),
        cmd="test command",
        cwd="/test",
        started_at=123.0,
        starttime=pid_starttime_ticks(process.pid),
        generation=generation,
    )


def test_new_session_preserves_login_shell_env_stderr_and_record(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared import session_env

    calls: list[dict[str, object]] = []

    def fake_spawn_process(
        name: str,
        argv: list[str],
        env: dict[str, str],
        cwd: str,
        stdout: str,
        stderr: str,
    ) -> client.SpawnResult:
        calls.append(
            {
                "name": name,
                "argv": argv,
                "env": env,
                "cwd": cwd,
                "stdout": stdout,
                "stderr": stderr,
            }
        )
        return {"pid": os.getpid(), "reused": False}

    monkeypatch.setattr(client, "spawn_process", fake_spawn_process)
    monkeypatch.setattr(session_env, "venv_activation_prefix", lambda: "activate-venv && ")
    stderr = unit_home / "logs" / "service.stderr.log"
    backend = helperproc.HelperProcSessionBackend()

    assert backend.new_session(
        "ava-service",
        "python -m services.example.daemon",
        unit_home,
        env={"AVA_TEST": "forwarded"},
        stderr_append=stderr,
    )
    assert calls == [
        {
            "name": "ava-service",
            "argv": [
                "/bin/bash",
                "-lc",
                f"cd {unit_home.as_posix()} && activate-venv && "
                "exec python -m services.example.daemon",
            ],
            "env": {"AVA_TEST": "forwarded"},
            "cwd": str(unit_home),
            "stdout": str(unit_home / "logs" / "ava-service.out.log"),
            "stderr": str(stderr),
        }
    ]
    record = SessionRecord.read(unit_home / "run" / "sessions" / "ava-service.json")
    assert record is not None
    assert record.pid == os.getpid()
    assert record.cmd == "python -m services.example.daemon"
    assert backend.has_session("ava-service")

    # Persistent records, not the helper's volatile nursery table, own idempotency.
    assert backend.new_session(
        "ava-service",
        "python -m services.example.daemon",
        unit_home,
        env={"AVA_TEST": "forwarded"},
    )
    assert len(calls) == 1


def test_new_session_preserves_argv_and_non_login_shell(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_spawn_process(
        _name: str,
        argv: list[str],
        _env: dict[str, str],
        _cwd: str,
        _stdout: str,
        _stderr: str,
    ) -> client.SpawnResult:
        calls.append(argv)
        return {"pid": os.getpid(), "reused": False}

    monkeypatch.setattr(client, "spawn_process", fake_spawn_process)
    backend = helperproc.HelperProcSessionBackend()
    argv = [sys.executable, "-m", "services.agent_host.daemon"]

    assert backend.new_session("ava-agent", argv, unit_home, env={})
    assert backend.new_session(
        "ava-shell-command",
        "echo direct",
        unit_home,
        env={},
        login_shell=False,
    )
    assert calls == [argv, ["/bin/sh", "-c", "echo direct"]]


def test_spawn_failure_is_loud_and_has_no_fallback(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> client.SpawnResult:
        raise PermissionsHelperError("socket refused")

    monkeypatch.setattr(client, "spawn_process", unavailable)

    with pytest.raises(RuntimeError, match=r"helper is down or refused.*ava converge.*helper log"):
        helperproc.HelperProcSessionBackend().new_session(
            "ava-service", "echo nope", unit_home, env={}
        )


def test_record_registry_drives_list_timestamps_and_generation(unit_home: Path) -> None:
    sessions = unit_home / "run" / "sessions"
    live = _current_process_record(generation="generation-1")
    dead = SessionRecord(
        pid=999_999_999,
        create_time=-1.0,
        cmd="dead",
        cwd="/test",
        started_at=456.0,
    )
    live.write(sessions / "ava-live.json")
    dead.write(sessions / "ava-dead.json")
    backend = helperproc.HelperProcSessionBackend()

    assert backend.list_sessions("ava-") == ["ava-live"]
    assert not (sessions / "ava-dead.json").exists()
    assert backend.session_started_at("ava-live") == 123.0
    assert backend.session_started_ats(["ava-live", "missing"]) == {
        "ava-live": 123.0,
        "missing": None,
    }
    assert backend.session_generation("ava-live") == "generation-1"
    assert backend.session_log_path("ava-live") == unit_home / "logs" / "ava-live.out.log"


def test_kill_signals_record_pid_and_unlinks_only_after_confirmed_death(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _current_process_record()
    path = unit_home / "run" / "sessions" / "ava-live.json"
    record.write(path)
    sent: list[tuple[int, int]] = []
    liveness = iter((True, False))

    monkeypatch.setattr(helperproc.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(helperproc, "_process_is_live", lambda _proc: next(liveness))

    assert helperproc.HelperProcSessionBackend().kill_session(
        "ava-live", graceful=True, timeout=0.0
    ) == (True, "graceful")
    assert sent == [(record.pid, signal.SIGTERM)]
    assert not path.exists()


@pytest.mark.parametrize(
    ("is_windows", "is_macos", "enabled", "spawn", "expected"),
    [
        (True, False, True, True, "WinprocSessionBackend"),
        (False, False, True, True, "PosixProcSessionBackend"),
        (False, True, False, True, "PosixProcSessionBackend"),
        (False, True, True, False, "PosixProcSessionBackend"),
        (False, True, True, True, "HelperProcSessionBackend"),
    ],
)
def test_backend_route_matrix(
    monkeypatch: pytest.MonkeyPatch,
    is_windows: bool,
    is_macos: bool,
    enabled: bool,
    spawn: bool,
    expected: str,
) -> None:
    from shared import session_backend
    from shared.config import settings

    monkeypatch.setattr(session_backend, "IS_WINDOWS", is_windows)
    monkeypatch.setattr(session_backend, "IS_MACOS", is_macos)
    monkeypatch.setattr(settings.services, "permissions_helper_enabled", enabled)
    monkeypatch.setattr(settings.services, "permissions_helper_spawn", spawn)
    monkeypatch.setattr(session_backend, "_backend", None)

    backend = session_backend.get_backend()
    assert type(backend).__name__ == expected
    if expected == "HelperProcSessionBackend":
        assert session_backend.native_proc() is backend


def test_backend_route_fails_closed_when_settings_are_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shared.config
    from shared import session_backend

    class BrokenSettings:
        @property
        def services(self) -> object:
            raise RuntimeError("unreadable")

    monkeypatch.setattr(session_backend, "IS_WINDOWS", False)
    monkeypatch.setattr(session_backend, "IS_MACOS", True)
    monkeypatch.setattr(shared.config, "settings", BrokenSettings())
    monkeypatch.setattr(session_backend, "_backend", None)

    assert type(session_backend.get_backend()).__name__ == "PosixProcSessionBackend"


def test_permissions_helper_spawn_defaults_off() -> None:
    from shared.config import FIELD_INFOS, field_alias
    from shared.config.services import ServiceSettings

    assert ServiceSettings.model_fields["permissions_helper_spawn"].default is False
    assert field_alias("permissions_helper_spawn") == "AVA_PERMISSIONS_HELPER_SPAWN"
    assert FIELD_INFOS["permissions_helper_spawn"].json_schema_extra == {
        "capability": "agent-runner",
        "restart_required": "",
        "writable": False,
        "sensitive": False,
        "scope": "host",
        "remote_writable": True,
    }


def test_pty_host_uses_direct_helper_child_when_enabled(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared import session_backend
    from shared.pty_sessions import cli

    calls: list[dict[str, object]] = []

    def fake_spawn_via_helper(
        name: str,
        argv: list[str],
        cwd: Path,
        *,
        env: dict[str, str],
        stdout: Path,
        stderr: Path,
    ) -> int:
        calls.append(
            {
                "name": name,
                "argv": argv,
                "cwd": cwd,
                "env": env,
                "stdout": stdout,
                "stderr": stderr,
            }
        )
        return os.getpid()

    monkeypatch.setattr(session_backend, "helper_spawn_enabled", lambda: True)
    monkeypatch.setattr(helperproc, "spawn_via_helper", fake_spawn_via_helper)
    monkeypatch.setattr(cli, "session_request", lambda *_args: {"ok": True})
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("helper mode must not invoke _reparent"),
    )
    envfile = unit_home / "env.sh"

    assert cli._spawn_host("ava-shell", str(unit_home), str(envfile), "gen-1", "") == 0
    assert len(calls) == 1
    call = calls[0]
    assert call["name"] == "pty-host-ava-shell"
    assert call["argv"] == [
        sys.executable,
        "-m",
        "shared.pty_sessions.host",
        "ava-shell",
        str(unit_home),
        str(envfile),
        str(cli.record_path("ava-shell")),
        str(cli.socket_path("ava-shell")),
        str(cli.transcript_path("ava-shell")),
        "gen-1",
    ]
    assert call["cwd"] == unit_home
    assert call["stdout"] == unit_home / "logs" / "ava-shell.host.log"
    assert call["stderr"] == unit_home / "logs" / "ava-shell.host.log"


def test_parent_chain_guard_allows_unmanaged_and_requires_direct_helper_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AVA_PERMISSIONS_HELPER_PID", raising=False)
    monkeypatch.setattr(os, "getppid", lambda: 100)
    assert parent_chain_intact()

    monkeypatch.setenv("AVA_PERMISSIONS_HELPER_PID", "100")
    assert parent_chain_intact()

    monkeypatch.setattr(os, "getppid", lambda: 101)
    assert not parent_chain_intact()

    monkeypatch.setenv("AVA_PERMISSIONS_HELPER_PID", "not-an-int")
    assert not parent_chain_intact()


def test_parent_chain_checks_are_wired_at_host_boot_and_heartbeat() -> None:
    from services.agent_host import daemon

    hosted_boot = inspect.getsource(daemon.main)
    hosted_beat = inspect.getsource(daemon._beat_forever)

    assert "_require_helper_parent_chain()" in hosted_boot
    assert "_require_helper_parent_chain()" in hosted_beat


def test_broken_parent_chain_exits_host_with_software_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.agent_host import daemon

    exits: list[int] = []
    monkeypatch.setattr(daemon, "parent_chain_intact", lambda: False)
    monkeypatch.setattr(os, "_exit", exits.append)

    daemon._require_helper_parent_chain()

    assert exits == [70]
