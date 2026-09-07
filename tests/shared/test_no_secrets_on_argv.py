"""One guard for the whole class: no launch path may put secret material on a
command line (issue #974).

`ps -eo command` shows every process's argv to any local user on macOS and Linux
alike — a wider surface than an environment, which is owner-only on both. So
every place Ava starts a long-running process is driven here with a sentinel
secret in the environment, and the argv it builds is asserted clean. A future
launcher that reaches for `redis-cli -a`, an env-var argv splice, or an
argv-carried JSON blob fails here rather than in a `ps` listing.

Coverage is per *launcher*, not per caller: the `ava start` session launch,
`spawn_update` / `spawn_rollout` / `spawn_restart` (via
`_spawn_detached_session`), and the healthcheck respawns (via
`respawn_service`) each funnel into one of the functions below. Agent-host
startup also passes its secret-bearing DB projection through the environment.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from shared.platform import IS_WINDOWS

# Values that must never appear in an argv. Shaped like the real thing: the
# cluster secret, the data-plane URLs that embed it, a provider key.
_SECRET = "sentinel-cluster-secret-6f21ab"  # noqa: S105 — a sentinel to search argv for, not a credential
_DB_URL = f"postgresql://ava:{_SECRET}@10.0.0.4:5433/ava"
_REDIS_URL = f"redis://ava:{_SECRET}@10.0.0.4:6380/0"
_API_KEY = "sk-sentinel-provider-key-4c19"
_SECRET_VALUES = (_SECRET, _DB_URL, _REDIS_URL, _API_KEY)

_SECRET_ENV = {
    "AVA_HOME": "/tmp/ava-home",  # noqa: S108 — a literal env value, never opened
    "AVA_CLUSTER_SECRET": _SECRET,
    "AVA_DB_URL": _DB_URL,
    "AVA_REDIS_URL": _REDIS_URL,
    "DEEPSEEK_API_KEY": _API_KEY,
    "PATH": "/usr/bin:/bin",
}

pytestmark = pytest.mark.skipif(
    IS_WINDOWS, reason="POSIX launch paths only (Windows hands env to CreateProcess)"
)

# The pid the faked reparent helper reports. Every launch below drives the REAL
# `posixproc.new_session` (only `subprocess.run` is faked), and that function
# records the reported pid together with the create_time it reads for it — the
# record a later `kill_session` resolves and SIGKILLs the process TREE of.
_FAKE_CHILD_PID = 4242


@pytest.fixture(autouse=True)
def _fake_child_pid_never_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the fabricated child pid from naming a real process.

    Without this the tests here are a live weapon. `new_session` reads
    `psutil.Process(<reported pid>).create_time()`; a fabricated pid that
    happens to be in use on the box resolves to a stranger, and the session
    record then carries that stranger's REAL create_time — so
    `_process_for_record`'s recycling guard passes (it is genuinely the same
    process) and the next `respawn_service`, which opens with
    `kill_session("ava-gateway")`, force-kills that stranger's whole process
    tree. On a CI runner low pids are always in use, and the stranger is
    typically a sibling xdist worker: the observed symptom is
    `[gwN] node down: Not properly terminated` charged to whichever test was
    running there, with no hint of where the kill came from.

    Raising `NoSuchProcess` for this one pid routes the record through the
    `_DEAD_CHILD_SENTINEL` branch `posixproc` already has for exactly this
    danger (audit 2026-08-08 P2), which records a create_time no live process
    can match. Other pids resolve normally.
    """
    import psutil

    real_process = psutil.Process

    def guarded(pid: int | None = None, *args: Any, **kwargs: Any) -> Any:
        if pid == _FAKE_CHILD_PID:
            raise psutil.NoSuchProcess(pid)
        return real_process(pid, *args, **kwargs)

    monkeypatch.setattr(psutil, "Process", guarded)


def _assert_clean(argv: list[str], *, label: str) -> None:
    for element in argv:
        for secret in _SECRET_VALUES:
            assert secret not in element, f"{label} leaked a secret on argv: {argv!r}"


@pytest.fixture
def secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live env a daemon launcher forwards. Replaced wholesale — the
    forwarders read `os.environ` by nature (see lint_no_os_environ's allowlist)."""
    monkeypatch.setattr(os, "environ", dict(_SECRET_ENV))


@pytest.fixture
def captured_argv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every subprocess argv the code under test builds."""
    calls: list[list[str]] = []

    def fake_run(args: Any, **_kwargs: Any) -> Any:
        if isinstance(args, (list, tuple)):
            calls.append([str(a) for a in args])  # pyright: ignore[reportUnknownArgumentType]
        if isinstance(args, (list, tuple)) and "shared._reparent" in [str(a) for a in args]:  # pyright: ignore[reportUnknownArgumentType]
            # The native supervisor's reparent helper reports the child pid on
            # stdout; the launch continues past it with a fake pid.
            pid_line = f"{_FAKE_CHILD_PID}\n"
            return subprocess.CompletedProcess(args, returncode=0, stdout=pid_line, stderr="")  # pyright: ignore[reportUnknownArgumentType]
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_session_backend_new_session(
    secret_env: None, captured_argv: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ava start` / `ava restart` bring every daemon up through here."""
    from shared.session_backend import PosixProcSessionBackend
    from shared.session_env import forward_env_dict

    PosixProcSessionBackend().new_session(
        "ava-gateway", ".venv/bin/python -m gateway", Path("/repo"), env=forward_env_dict()
    )
    assert captured_argv, "no subprocess was launched"
    _assert_clean(captured_argv[-1], label="PosixProcSessionBackend.new_session")


def test_launch_record_cannot_reach_a_live_process(
    secret_env: None, captured_argv: list[list[str]]
) -> None:
    """The session record a launch leaves behind must resolve to nothing.

    This is the one thing in this file that can act outside the test. The child
    pid here is fabricated, and `respawn_service` opens with
    `kill_session("ava-gateway")`, which resolves whatever record it finds and
    force-kills that pid's whole process TREE. A record naming a live stranger
    therefore turns an argv-shape assertion into a kill of an unrelated process
    — a sibling xdist worker, on a CI runner where low pids are always in use.

    `has_session` is the same resolve `kill_session` does, so False here means
    there is nothing for it to reach.
    """
    from shared import posixproc
    from shared.session_backend import PosixProcSessionBackend
    from shared.session_env import forward_env_dict

    PosixProcSessionBackend().new_session(
        "ava-gateway", ".venv/bin/python -m gateway", Path("/repo"), env=forward_env_dict()
    )
    assert captured_argv, "no subprocess was launched"
    assert not posixproc.has_session("ava-gateway")


@pytest.mark.real_service_respawn
def test_service_respawn(secret_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The healthcheck respawn every watchdog drives — post-switch it launches
    through the native supervisor: the reparent helper's argv carries the
    wrapped command (cd + venv + daemon), never the env values."""
    import subprocess as _sp
    import sys

    from shared.service_respawn import respawn_service

    calls: list[list[str]] = []

    def fake_run(args: Any, **_kwargs: Any) -> Any:
        if isinstance(args, (list, tuple)):
            calls.append([str(a) for a in args])  # pyright: ignore[reportUnknownArgumentType]
        if args[:2] == [sys.executable, "-m"] and "shared._reparent" in args:
            # The helper reports the child pid on stdout; a fake pid's
            # create_time read is caught by posixproc.
            pid_line = f"{_FAKE_CHILD_PID}\n"
            return _sp.CompletedProcess(args, returncode=0, stdout=pid_line, stderr="")  # pyright: ignore[reportUnknownArgumentType]
        return _sp.CompletedProcess(args, returncode=0, stdout="", stderr="")  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(subprocess, "run", fake_run)
    respawn_service("gateway", ".venv/bin/python -m gateway", Path("/repo"))
    # The launch shape the backend produces — the reparent helper (native
    # supervisor) — the env values never ride the argv: the env dict is handed
    # to the supervisor out-of-band.
    launches = [a for a in calls if "shared._reparent" in a]
    assert launches, f"no launch; saw {calls!r}"
    _assert_clean(launches[-1], label="respawn_service")


@pytest.mark.real_cluster_spawn
def test_cluster_orchestration_session(secret_env: None, captured_argv: list[list[str]]) -> None:
    """update / rollout / cluster-restart all spawn through this one.

    S7: the POSIX orchestration session launches through the native process
    supervisor (the reparent helper), whose argv carries the wrapped command
    — never the env values, which ride the env dict."""
    from ops.cluster_session import _spawn_detached_session

    _spawn_detached_session(
        "ava-updater", shell_cmd="ava cluster update | tee -a log", native_cmd="ava cluster update"
    )
    launches = [a for a in captured_argv if "shared._reparent" in a]
    assert launches, f"no backend launch; saw {captured_argv!r}"
    _assert_clean(launches[-1], label="_spawn_detached_session")


def test_schedule_launch(
    secret_env: None, captured_argv: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schedule's resident process. The launch is the PTY supervisor CLI
    `new <name> <cwd> <envfile> [cmd_b64]`: the argv carries the 0600
    envfile's path and a base64 command — never the env values; the schedule
    id rides the envfile, not the argv."""
    from gateway.schedule_manager import ScheduleManager

    manager = ScheduleManager(None)  # type: ignore[arg-type] — _launch's pool-touching writes are stubbed below
    monkeypatch.setattr(ScheduleManager, "_set_status", lambda *_a, **_k: True)  # pyright: ignore[reportUnknownArgumentType]
    # The orphan-run close is a pool write like _set_status — stubbed the same
    # way; this test asserts argv cleanliness, not DB behavior.
    monkeypatch.setattr(ScheduleManager, "_close_null_runs", lambda _self, _sid: None)  # pyright: ignore[reportUnknownArgumentType]
    manager._launch(7)
    launches = [
        a
        for a in captured_argv
        if a[:3] == [sys.executable, "-m", "shared.pty_sessions.cli"]
        and len(a) >= 5
        and a[4] == "new"
    ]
    assert launches, f"no pty CLI new; saw {captured_argv!r}"
    argv = launches[-1]
    assert argv[3:5] == ["ava-schedule-7", "new"]  # <name> <op>
    _assert_clean(argv, label="ScheduleManager._launch")
    # the envfile is 0600 and carries only the host-scope forward view +
    # AVA_SCHEDULE_ID — never the secrets
    envfile = Path(argv[-2])
    assert envfile.name.endswith(".env.sh")
    assert (envfile.stat().st_mode & 0o777) == 0o600
    body = envfile.read_text()
    for secret in _SECRET_VALUES:
        assert secret not in body, f"{secret!r} leaked into the schedule envfile"
    # the schedule id rides the envfile, not the argv
    assert "AVA_SCHEDULE_ID" not in " ".join(argv)
    assert "AVA_SCHEDULE_ID=" in body
    # the command rides base64 — decodes to the runner cmd, no secret material
    import base64

    cmd = base64.b64decode(argv[-1]).decode()
    assert "gateway.schedule_runner" in cmd
    envfile.unlink(missing_ok=True)


def test_agent_shell_session(
    secret_env: None, captured_argv: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent's own persistent shell. S6 step 2: the create is the PTY
    supervisor CLI `new <name> <cwd> <envfile>` — the argv carries the 0600
    envfile's PATH, never its contents. The envfile itself carries only the
    host-scope session forward view (the allowlist deliberately drops the
    cluster-scope secrets — the shell's children re-source them at their own
    boot), so the secret values appear NOWHERE in this launch path.
    """
    from ava.shell import sessions

    monkeypatch.setattr(sessions, "_next_session_index_from_db", lambda: 3)
    monkeypatch.setattr(sessions, "_shell_prefix", lambda: "ava-agent-1-shell-")
    sessions._create_session("probe")
    launches = [
        a for a in captured_argv if a[:3] == [sys.executable, "-m", "shared.pty_sessions.cli"]
    ]
    assert launches, f"no pty CLI launch; saw {captured_argv!r}"
    argv = launches[-1]
    assert argv[3:5] == ["ava-agent-1-shell-3-probe", "new"]  # <name> <op>
    _assert_clean(argv, label="ava.shell.sessions._create_session")
    # the envfile is 0600 and holds only the host-scope forward view — the
    # secrets must not be in it either (they never reach the shell's env)
    envfile = Path(argv[-1])
    assert envfile.name.endswith(".env.sh")
    assert (envfile.stat().st_mode & 0o777) == 0o600
    body = envfile.read_text()
    for secret in _SECRET_VALUES:
        assert secret not in body, f"{secret!r} leaked into the shell envfile"
    assert "AVA_HOME=" in body and "PATH=" in body
    envfile.unlink(missing_ok=True)


@pytest.mark.real_service_respawn
def test_agent_host_launch(
    secret_env: None, captured_argv: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real host healthcheck forwards its DB credential through env only.

    Exercise the healthcheck, shared service launcher, and native supervisor;
    only the final subprocess and the post-launch liveness probe are faked.
    """
    from services.healthchecks import agent_host
    from shared.config import settings
    from shared.daemon_health import DaemonProbe

    captured_env: list[dict[str, str]] = []
    fake_run = subprocess.run

    def capture_run(args: Any, **kwargs: Any) -> Any:
        if "shared._reparent" in args:
            captured_env.append(dict(kwargs["env"]))
        return fake_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", capture_run)
    runner_url = _DB_URL.replace("postgresql://ava:", "postgresql://ava_runner:")
    monkeypatch.setattr(settings.data_plane, "db_url", runner_url)
    monkeypatch.setattr(settings.services, "project_root", Path("/repo"))
    monkeypatch.setattr(agent_host, "_probe", lambda: DaemonProbe.up("test host"))

    assert agent_host._restart_daemon().alive

    launches = [argv for argv in captured_argv if "shared._reparent" in argv]
    assert len(launches) == len(captured_env) == 1
    assert "services.agent_host.daemon" in " ".join(launches[0])
    _assert_clean(launches[0], label="agent_host._restart_daemon")
    assert captured_env[0]["AVA_DB_URL"] == runner_url
    assert captured_env[0]["AVA_PROCESS_PROFILE"] == "agent"


def test_redis_bringup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The cluster's own redis: `--requirepass <secret>` and `redis-cli -a <secret>`
    would both publish the cluster secret. It goes through a 0600 conf file and
    `$REDISCLI_AUTH` instead."""
    from cli.commands import _cluster_instance as ci

    calls: list[list[str]] = []

    def fake_run(args: Any, **_kw: Any) -> Any:
        calls.append([str(a) for a in args])
        return subprocess.CompletedProcess(args, returncode=0, stdout="PONG", stderr="")

    monkeypatch.setattr(ci.subprocess, "run", fake_run)
    monkeypatch.setattr(ci, "_redis_data_dir", lambda: tmp_path / "redis")
    monkeypatch.setattr(ci, "_ensure_redis_acl", lambda *_a, **_k: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(ci, "_bind_addrs", lambda _secret: ["127.0.0.1"])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(ci, "print", lambda *_a, **_k: None, raising=False)  # pyright: ignore[reportUnknownArgumentType]
    # `_redis_server_bin` resolves through `brew_prefix`, which is `@cache`d for
    # the process lifetime — routing it through the faked `subprocess.run` above
    # would permanently poison that cache with "PONG" (brew's stdout stand-in)
    # for every other test in this worker, real infra tests included. Stub the
    # resolved path directly so `brew_prefix` is never called here at all.
    monkeypatch.setattr(
        ci,
        "_redis_server_bin",
        lambda: "/opt/homebrew/opt/redis@8.2/bin/redis-server",
    )

    # down on the first probe (so the full start path runs), up on the next
    probes = iter([False, True, True])
    monkeypatch.setattr(ci, "_redis_running", lambda *_a: next(probes))  # pyright: ignore[reportUnknownArgumentType]
    assert ci._start_redis(46999, _SECRET, _SECRET, _SECRET, "ava") == 0

    for argv in calls:
        _assert_clean(argv, label="redis bring-up")
    conf = tmp_path / "redis" / "redis.conf"
    assert conf.stat().st_mode & 0o777 == 0o600
    assert _SECRET in conf.read_text()
