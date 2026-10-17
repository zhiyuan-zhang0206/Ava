from __future__ import annotations

import inspect
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import psutil
import pytest

from base.host.permissions_helper import client
from base.host.permissions_helper.client import PermissionsHelperClient, PermissionsHelperError
from base.native_process import pid_starttime_ticks
from base.sessions import helperproc
from base.sessions.helper_chain_guard import parent_chain_intact
from base.sessions.record import SessionRecord


class _SpawnClient(PermissionsHelperClient):
    def __init__(self, spawn: Callable[..., client.SpawnResult]) -> None:
        super().__init__("/unused.sock")
        self._spawn = spawn

    def spawn_process(self, *args: Any, **kwargs: Any) -> client.SpawnResult:  # pyright: ignore[reportIncompatibleMethodOverride]
        # meiva:note
        # The helper spawn double records argv, environment and output destinations without opening a helper socket; helper refusal remains loud and has no fallback launch.
        # meiva:link tests/harness/docs/helper-process-tests.meiva.okf.md
        return self._spawn(*args, **kwargs)


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


# meiva:link tests/harness/docs/helper-process-tests.meiva.okf.md
def test_new_session_preserves_login_shell_env_stderr_and_record(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from base.sessions import env_forwarding

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

    fake_client = _SpawnClient(fake_spawn_process)
    monkeypatch.setattr(env_forwarding, "venv_activation_prefix", lambda: "activate-venv && ")
    stderr = unit_home / "logs" / "service.stderr.log"
    backend = helperproc.HelperProcSessionBackend(lambda: fake_client)

    assert backend.new_session(
        "meiva-service",
        "python -m services.example.daemon",
        unit_home,
        env={"MEIVA_TEST": "forwarded"},
        stderr_append=stderr,
    )
    assert calls == [
        {
            "name": "meiva-service",
            "argv": [
                "/bin/bash",
                "-lc",
                f"cd {unit_home.as_posix()} && activate-venv && "
                "exec python -m services.example.daemon",
            ],
            "env": {"MEIVA_TEST": "forwarded"},
            "cwd": str(unit_home),
            "stdout": str(unit_home / "logs" / "meiva-service.out.log"),
            "stderr": str(stderr),
        }
    ]
    record = SessionRecord.read(unit_home / "run" / "sessions" / "meiva-service.json")
    assert record is not None
    assert record.pid == os.getpid()
    assert record.cmd == "python -m services.example.daemon"
    assert backend.has_session("meiva-service")

    assert backend.new_session(
        "meiva-service",
        "python -m services.example.daemon",
        unit_home,
        env={"MEIVA_TEST": "forwarded"},
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

    fake_client = _SpawnClient(fake_spawn_process)
    backend = helperproc.HelperProcSessionBackend(lambda: fake_client)
    argv = [sys.executable, "-m", "services.agent_runner.agent_host.daemon"]

    assert backend.new_session("meiva-agent", argv, unit_home, env={})
    assert backend.new_session(
        "meiva-shell-command",
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

    fake_client = _SpawnClient(unavailable)

    with pytest.raises(
        RuntimeError, match=r"helper is down or refused.*meiva converge.*helper log"
    ):
        helperproc.HelperProcSessionBackend(lambda: fake_client).new_session(
            "meiva-service", "echo nope", unit_home, env={}
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
    live.write(sessions / "meiva-live.json")
    dead.write(sessions / "meiva-dead.json")
    backend = helperproc.HelperProcSessionBackend(lambda: PermissionsHelperClient("/unused.sock"))

    assert backend.list_sessions("meiva-") == ["meiva-live"]
    assert not (sessions / "meiva-dead.json").exists()
    assert backend.session_started_at("meiva-live") == 123.0
    assert backend.session_started_ats(["meiva-live", "missing"]) == {
        "meiva-live": 123.0,
        "missing": None,
    }
    assert backend.session_generation("meiva-live") == "generation-1"
    assert backend.session_log_path("meiva-live") == unit_home / "logs" / "meiva-live.out.log"


def test_kill_signals_record_pid_and_unlinks_only_after_confirmed_death(
    unit_home: Path,
) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        record = replace(_current_process_record(), pid=child.pid)
        record = replace(
            record,
            create_time=psutil.Process(child.pid).create_time(),
            starttime=pid_starttime_ticks(child.pid),
        )
        path = unit_home / "run" / "sessions" / "meiva-live.json"
        record.write(path)

        assert helperproc.HelperProcSessionBackend(
            lambda: PermissionsHelperClient("/unused.sock")
        ).kill_session("meiva-live", graceful=True, timeout=10.0) == (True, "graceful")
        assert child.wait(timeout=10) == -signal.SIGTERM
        assert not path.exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


# meiva:link tests/harness/docs/helper-process-tests.meiva.okf.md
def test_graceful_delivery_tolerates_whole_second_create_time_drift(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drifted = replace(_current_process_record(), create_time=psutil.Process().create_time() - 1.0)
    sent: list[tuple[int, int]] = []

    drifted.write(unit_home / "run" / "sessions" / "meiva-live.json")
    monkeypatch.setattr(helperproc.os, "kill", lambda pid, sig: sent.append((pid, sig)))

    assert helperproc.HelperProcSessionBackend(
        lambda: PermissionsHelperClient("/unused.sock")
    ).graceful_signal("meiva-live", expected=drifted)
    assert sent == [(drifted.pid, signal.SIGTERM)]


def test_graceful_delivery_refuses_a_birth_beyond_the_tolerance(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replaced = replace(_current_process_record(), create_time=psutil.Process().create_time() + 60.0)
    sent: list[tuple[int, int]] = []

    replaced.write(unit_home / "run" / "sessions" / "meiva-live.json")
    monkeypatch.setattr(helperproc.os, "kill", lambda pid, sig: sent.append((pid, sig)))

    assert not helperproc.HelperProcSessionBackend(
        lambda: PermissionsHelperClient("/unused.sock")
    ).graceful_signal("meiva-live", expected=replaced)
    assert sent == []


@pytest.mark.parametrize(
    ("is_macos", "enabled", "spawn", "expected"),
    [
        (False, True, True, "PosixProcSessionBackend"),
        (True, False, True, "PosixProcSessionBackend"),
        (True, True, False, "PosixProcSessionBackend"),
        (True, True, True, "HelperProcSessionBackend"),
    ],
)
def test_backend_route_matrix(
    monkeypatch: pytest.MonkeyPatch,
    is_macos: bool,
    enabled: bool,
    spawn: bool,
    expected: str,
) -> None:
    from base.config import settings
    from base.sessions import backend as session_backend

    monkeypatch.setattr(session_backend, "is_macos", lambda: is_macos)
    monkeypatch.setattr(settings.services, "permissions_helper_enabled", enabled)
    monkeypatch.setattr(settings.services, "permissions_helper_spawn", spawn)

    backend = session_backend.get_backend()
    assert type(backend).__name__ == expected


def test_backend_route_fails_closed_when_settings_are_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base.config
    from base.sessions import backend as session_backend

    class BrokenSettings:
        @property
        def services(self) -> object:
            raise RuntimeError("unreadable")

    monkeypatch.setattr(session_backend, "is_macos", lambda: True)
    monkeypatch.setattr(base.config, "settings", BrokenSettings())

    assert type(session_backend.get_backend()).__name__ == "PosixProcSessionBackend"


def test_permissions_helper_spawn_defaults_off() -> None:
    from base.config import FIELD_INFOS, field_alias
    from base.config.domains.services.settings import ServiceSettings

    assert ServiceSettings.model_fields["permissions_helper_spawn"].default is False
    assert field_alias("permissions_helper_spawn") == "MEIVA_PERMISSIONS_HELPER_SPAWN"
    assert FIELD_INFOS["permissions_helper_spawn"].json_schema_extra == {
        "capability": "agent-runner",
        "restart_required": "",
        "writable": False,
        "sensitive": False,
        "scope": "host",
        "remote_writable": True,
    }


def test_parent_chain_guard_allows_unmanaged_and_rejects_malformed_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_ppid = {os.getpid(): 100}.get
    monkeypatch.delenv("MEIVA_PERMISSIONS_HELPER_PID", raising=False)
    assert parent_chain_intact(read_ppid)

    monkeypatch.setenv("MEIVA_PERMISSIONS_HELPER_PID", "100")
    assert parent_chain_intact(read_ppid)

    monkeypatch.setenv("MEIVA_PERMISSIONS_HELPER_PID", "not-an-int")
    assert not parent_chain_intact(read_ppid)

    monkeypatch.setenv("MEIVA_PERMISSIONS_HELPER_PID", "0")
    assert not parent_chain_intact(read_ppid)

    monkeypatch.setenv("MEIVA_PERMISSIONS_HELPER_PID", "-3")
    assert not parent_chain_intact(read_ppid)


def test_parent_chain_guard_rejects_a_helper_outside_the_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = {os.getpid(): 100, 100: 1}
    monkeypatch.setenv("MEIVA_PERMISSIONS_HELPER_PID", "101")
    assert not parent_chain_intact(chain.get)


# meiva:note
# A root supervisor between a unit and the permissions helper does not break the helper ancestry check; malformed markers, broken intermediate links and an unrelated helper are rejected.
# meiva:link tests/harness/docs/helper-process-tests.meiva.okf.md
def test_parent_chain_guard_accepts_the_helper_above_an_intermediate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = {os.getpid(): 101, 101: 100}
    monkeypatch.setenv("MEIVA_PERMISSIONS_HELPER_PID", "100")
    assert parent_chain_intact(chain.get)


def test_parent_chain_guard_rejects_a_broken_intermediate_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEIVA_PERMISSIONS_HELPER_PID", "100")
    assert not parent_chain_intact({os.getpid(): 101}.get)


def test_parent_chain_guard_stops_at_the_depth_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    links: dict[int, int] = {os.getpid(): 7000}
    for step in range(40):
        links[7000 + step] = 7001 + step

    monkeypatch.setenv("MEIVA_PERMISSIONS_HELPER_PID", "7030")
    assert parent_chain_intact(links.get)

    monkeypatch.setenv("MEIVA_PERMISSIONS_HELPER_PID", "7040")
    assert not parent_chain_intact(links.get)


# meiva:note
# The real ancestor-chain probe launches the current interpreter with a fixed repository probe script and test-owned paths, then checks the reported ancestry for an intact or unrelated helper.
# meiva:link tests/harness/docs/helper-process-tests.meiva.okf.md
def _run_probe_chain(marker_dir: Path, *, marker_value: str | None = None) -> str:
    env = None
    if marker_value is not None:
        env = {**os.environ, "MEIVA_TEST_HELPER_MARKER_VALUE": marker_value}
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(Path(__file__).parents[1] / "components" / "base" / "helper_chain_probe.py"),
            "helper",
            str(marker_dir / "marker"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        env=env,
    )
    return result.stdout.strip()


def test_parent_chain_guard_on_a_real_process_chain(tmp_path: Path) -> None:
    assert _run_probe_chain(tmp_path) == "intact"


def test_parent_chain_guard_rejects_an_outside_pid_on_a_real_chain(tmp_path: Path) -> None:
    outsider = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert _run_probe_chain(tmp_path, marker_value=str(outsider.pid)) == "broken"
    finally:
        outsider.kill()
        outsider.wait(timeout=10)


def test_parent_chain_checks_are_wired_at_host_boot_and_heartbeat() -> None:
    from services.agent_runner.agent_host import daemon

    hosted_boot = inspect.getsource(daemon.main)
    hosted_beat = inspect.getsource(daemon.beat_forever)

    assert "require_helper_parent_chain()" in hosted_boot
    assert "require_helper_parent_chain()" in hosted_beat


def test_broken_parent_chain_exits_host_with_software_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.agent_runner.agent_host import daemon

    exits: list[int] = []
    monkeypatch.setattr(daemon, "parent_chain_intact", lambda: False)
    monkeypatch.setattr(os, "_exit", exits.append)

    daemon.require_helper_parent_chain()

    assert exits == [70]
