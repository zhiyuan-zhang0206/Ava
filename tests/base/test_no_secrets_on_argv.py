"""One guard for the whole class: no launch path may put secret material on a
command line (issue #974).

`ps -eo command` shows every process's argv to any local user on macOS and Linux
alike — a wider surface than an environment, which is owner-only on both. So
every place Ava starts a long-running process is driven here with a sentinel
secret in the environment, and the argv it builds is asserted clean. A future
launcher that reaches for `redis-cli -a`, an env-var argv splice, or an
argv-carried JSON blob fails here rather than in a `ps` listing.

Coverage is per *launcher*, not per caller: the `ava start` session launch,
schedule processes, agent shells, and the Redis bring-up each funnel into one
of the functions below. Schedule and agent-shell sessions are created through the
pty-sessions service: their environment travels in the request body of its
owner-only unix socket, so the service process and every shell it spawns carry
no secret on their argv. Agent-host startup also passes its secret-bearing DB
projection through the environment.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import psutil
import pytest

from base.cluster import ownership, port_preflight
from base.native_process.os_platform import IS_WINDOWS
from tests.path_scoped.pty_service import PtyServiceProcess
from tests.path_scoped.pty_service import pty_service as pty_service
from tests.path_scoped.pty_shells import output_until, type_line, wait_for

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


@pytest.fixture
def _fake_child_pid_never_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the fabricated child pid from naming a real process.

    Without this the tests here are a live weapon. `new_session` reads
    `psutil.Process(<reported pid>).create_time()`; a fabricated pid that
    happens to be in use on the box resolves to a stranger, and the session
    record then carries that stranger's REAL create_time — so
    `_process_for_record`'s recycling guard passes (it is genuinely the same
    process) and a later `kill_session("ava-gateway")` force-kills that
    stranger's whole process tree. On a CI runner low pids are always in use,
    and the stranger is typically a sibling xdist worker: the observed symptom is
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
    forwarders read `os.environ` by nature (see no_os_environ's allowlist)."""
    monkeypatch.setattr(os, "environ", dict(_SECRET_ENV))


@pytest.fixture
def captured_argv(
    monkeypatch: pytest.MonkeyPatch, _fake_child_pid_never_resolves: None
) -> list[list[str]]:
    """Every subprocess argv the code under test builds (with the fabricated child pid
    made unresolvable: the faked reparent helper reports it)."""
    calls: list[list[str]] = []

    def fake_run(args: Any, **_kwargs: Any) -> Any:
        if isinstance(args, (list, tuple)):
            calls.append([str(a) for a in args])  # pyright: ignore[reportUnknownArgumentType]
        if isinstance(args, (list, tuple)) and "base._reparent" in [str(a) for a in args]:  # pyright: ignore[reportUnknownArgumentType]
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
    from base.sessions.backend import PosixProcSessionBackend
    from base.sessions.env_forwarding import forward_env_dict

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
    pid here is fabricated, and `kill_session("ava-gateway")` resolves whatever
    record it finds and force-kills that pid's whole process TREE. A record
    naming a live stranger therefore turns an argv-shape assertion into a kill
    of an unrelated process
    — a sibling xdist worker, on a CI runner where low pids are always in use.

    `has_session` is the same resolve `kill_session` does, so False here means
    there is nothing for it to reach.
    """
    from base.sessions import posixproc
    from base.sessions.backend import PosixProcSessionBackend
    from base.sessions.env_forwarding import forward_env_dict

    PosixProcSessionBackend().new_session(
        "ava-gateway", ".venv/bin/python -m gateway", Path("/repo"), env=forward_env_dict()
    )
    assert captured_argv, "no subprocess was launched"
    assert not posixproc.has_session("ava-gateway")


@pytest.fixture
def secrets_in_creator_env(pty_service: PtyServiceProcess, monkeypatch: pytest.MonkeyPatch) -> None:
    """The creating process holds the secrets (as an agent or gateway does); the
    already-running service does not, so a secret reaching a shell could only have
    come through the request."""
    del pty_service
    for key, value in _SECRET_ENV.items():
        if key not in ("AVA_HOME", "PATH"):
            monkeypatch.setenv(key, value)


def _service_tree_argvs(service: PtyServiceProcess) -> list[list[str]]:
    """The argv of the service process and of every process under it (shells, jobs)."""
    root = psutil.Process(service.pid)
    argvs = [root.cmdline()]
    for child in root.children(recursive=True):
        try:
            argvs.append(child.cmdline())
        except psutil.NoSuchProcess:
            continue
    return argvs


def _assert_service_tree_clean(service: PtyServiceProcess, *, label: str) -> None:
    argvs = _service_tree_argvs(service)
    assert len(argvs) >= 2, f"{label}: expected the service and a shell, saw {argvs!r}"
    for argv in argvs:
        _assert_clean(argv, label=label)


def _shell_env_report(name: str) -> str:
    """What a secret-name lookup in the session's shell prints (empty: none is set)."""
    type_line(
        name,
        "printenv AVA_CLUSTER_SECRET AVA_DB_URL AVA_REDIS_URL DEEPSEEK_API_KEY; echo ENVCHECK_DONE",
    )
    return output_until(name, "ENVCHECK_DONE")


def test_schedule_launch(
    pty_service: PtyServiceProcess,
    secrets_in_creator_env: None,
    unit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A schedule's resident process. The launch is a `new` request to the pty-sessions
    service: the env (the schedule id included) rides the request body of its unix
    socket, the command is typed into the session's shell, and no argv of the service
    or the shell tree carries a secret; the shell's environment holds the schedule
    id and none of the secrets."""
    from services.schedule_manager import manager as sm
    from services.schedule_manager.manager import ScheduleManager

    # The runner is a stub that records its own environment, then stays up: the real
    # runner would need a database and exit within the test.
    stub = unit_home / ".venv" / "bin" / "python"
    stub.parent.mkdir(parents=True)
    dump = unit_home / "runner-env.txt"
    stub.write_text(f"#!/bin/sh\nenv > {dump}.tmp && mv {dump}.tmp {dump}\nexec sleep 300\n")
    stub.chmod(0o755)
    monkeypatch.setattr(sm, "REPO_ROOT", unit_home)

    manager = ScheduleManager(None)  # type: ignore[arg-type] — _launch's pool-touching writes are stubbed below
    monkeypatch.setattr(ScheduleManager, "_set_status", lambda *_a, **_k: True)  # pyright: ignore[reportUnknownArgumentType]
    # The orphan-run close is a pool write like _set_status — stubbed the same
    # way; this test asserts argv cleanliness, not DB behavior.
    monkeypatch.setattr(ScheduleManager, "_close_null_runs", lambda _self, _sid: None)  # pyright: ignore[reportUnknownArgumentType]
    manager._launch(7)

    assert wait_for(dump.exists), f"the runner never started:\n{pty_service.output()}"
    runner_env = dump.read_text()
    assert "AVA_SCHEDULE_ID=7\n" in runner_env, "the schedule id rides the request env"
    for secret in _SECRET_VALUES:
        assert secret not in runner_env, f"{secret!r} reached the schedule's environment"
    _assert_service_tree_clean(pty_service, label="ScheduleManager._launch")
    for argv in _service_tree_argvs(pty_service):
        assert "AVA_SCHEDULE_ID" not in " ".join(argv), f"schedule id on argv: {argv!r}"


def test_agent_shell_session(
    pty_service: PtyServiceProcess,
    secrets_in_creator_env: None,
) -> None:
    """An agent's own persistent shell. The create is a `new` request to the
    pty-sessions service carrying the host-scope session forward view in its body
    (the allowlist deliberately drops the cluster-scope secrets: the shell's
    children re-source them at their own boot), so the secret values appear
    NOWHERE in this launch path: not on an argv of the service or the shell, not in
    the shell's environment."""
    from ava.shell.sessions import ShellSessions
    from ava.shell.tests.support import FakeDatabase
    from base.sessions.backend import get_shell_backend

    ShellSessions(
        backend=get_shell_backend(), database=FakeDatabase(next_index=3), agent_id=1
    ).create("probe", ttl=120)
    name = "ava-agent-1-shell-3-probe"
    assert get_shell_backend().has_session(name)

    report = _shell_env_report(name)
    for secret in _SECRET_VALUES:
        assert secret not in report, f"{secret!r} leaked into the shell's environment"
    _assert_service_tree_clean(pty_service, label="ava.shell.sessions.create_session")


def test_redis_bringup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The cluster's own redis: `--requirepass <secret>` and `redis-cli -a <secret>`
    would both publish the cluster secret. It goes through a 0600 conf file and
    `$REDISCLI_AUTH` instead."""
    from cli.commands.data_plane import cluster_instance as ci

    calls: list[list[str]] = []

    def fake_run(args: Any, **_kw: Any) -> Any:
        calls.append([str(a) for a in args])
        return subprocess.CompletedProcess(args, returncode=0, stdout="PONG", stderr="")

    monkeypatch.setattr(ci.subprocess, "run", fake_run)
    monkeypatch.setattr(ownership, "redis_data_dir", lambda: tmp_path / "redis")
    monkeypatch.setattr(ci, "_ensure_redis_acl", lambda *_a, **_k: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(port_preflight, "bind_addrs", lambda _secret: ["127.0.0.1"])  # pyright: ignore[reportUnknownArgumentType]
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
    assert ci.start_redis(46999, _SECRET, _SECRET, _SECRET, "ava") == 0

    for argv in calls:
        _assert_clean(argv, label="redis bring-up")
    conf = tmp_path / "redis" / "redis.conf"
    assert conf.stat().st_mode & 0o777 == 0o600
    assert _SECRET in conf.read_text()
