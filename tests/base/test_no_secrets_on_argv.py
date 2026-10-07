"""One guard for the whole class: no launch path may put secret material on a
command line (issue #974).

`ps -eo command` shows every process's argv to any local user on macOS and Linux
alike — a wider surface than an environment, which is owner-only on both. So
every place Ava starts a long-running process is driven here with a sentinel
secret in the environment, and the argv it builds is asserted clean. A future
launcher that reaches for `redis-cli -a`, an env-var argv splice, or an
argv-carried JSON blob fails here rather than in a `ps` listing.

The schedule launcher contract lives with the schedule-manager tests and shares
the sentinel environment and real PTY argv harness from tests.factories.secret_argv.

Coverage is per *launcher*, not per caller: the `ava start` session launch,
schedule processes, agent shells, and the Redis bring-up each funnel into one
of the functions below. Schedule and agent-shell sessions are created through the
pty-sessions service: their environment travels in the request body of its
owner-only unix socket, so the service process and every shell it spawns carry
no secret on their argv. Agent-host startup also passes its secret-bearing DB
projection through the environment.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, cast

import psutil
import pytest

from base.cluster import ownership, port_preflight
from base.native_process.os_platform import IS_WINDOWS
from tests.factories.secret_argv import (
    SECRET,
    SECRET_VALUES,
    assert_clean,
    assert_service_tree_clean,
)
from tests.factories.secret_argv import secret_env as secret_env
from tests.factories.secret_argv import secrets_in_creator_env as secrets_in_creator_env
from tests.path_scoped.pty_service import PtyServiceProcess
from tests.path_scoped.pty_service import pty_service as pty_service
from tests.path_scoped.pty_shells import output_until, type_line

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

    real_process = psutil.Process

    def guarded(pid: int | None = None, *args: Any, **kwargs: Any) -> Any:
        if pid == _FAKE_CHILD_PID:
            raise psutil.NoSuchProcess(pid)
        return real_process(pid, *args, **kwargs)

    monkeypatch.setattr(psutil, "Process", guarded)


@pytest.fixture
def captured_argv(
    monkeypatch: pytest.MonkeyPatch, _fake_child_pid_never_resolves: None
) -> list[list[str]]:
    """Every subprocess argv the code under test builds (with the fabricated child pid
    made unresolvable: the faked reparent helper reports it)."""
    calls: list[list[str]] = []

    def fake_run(args: Any, **_kwargs: Any) -> Any:
        if isinstance(args, (list, tuple)):
            calls.append([str(a) for a in cast(list[object] | tuple[object, ...], args)])
        if isinstance(args, (list, tuple)) and "base.native_process.reparent" in [
            str(a) for a in cast(list[object] | tuple[object, ...], args)
        ]:
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
    assert_clean(captured_argv[-1], label="PosixProcSessionBackend.new_session")


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


def _shell_env_report(name: str) -> str:
    """What a secret-name lookup in the session's shell prints (empty: none is set)."""
    type_line(
        name,
        "printenv AVA_CLUSTER_SECRET AVA_DB_URL AVA_REDIS_URL DEEPSEEK_API_KEY; echo ENVCHECK_DONE",
    )
    return output_until(name, "ENVCHECK_DONE")


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
    for secret in SECRET_VALUES:
        assert secret not in report, f"{secret!r} leaked into the shell's environment"
    assert_service_tree_clean(pty_service, label="ava.shell.sessions.create_session")


def test_redis_bringup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The cluster's own redis: `--requirepass <secret>` and `redis-cli -a <secret>`
    would both publish the cluster secret. It goes through a 0600 conf file and
    `$REDISCLI_AUTH` instead."""
    from cli.commands.data_plane import cluster_instance as ci

    calls: list[list[str]] = []

    def fake_run(args: Any, **_kw: Any) -> Any:
        calls.append([str(a) for a in cast(list[object] | tuple[object, ...], args)])
        return subprocess.CompletedProcess(args, returncode=0, stdout="PONG", stderr="")

    monkeypatch.setattr(ci.subprocess, "run", fake_run)
    monkeypatch.setattr(ownership, "redis_data_dir", lambda: tmp_path / "redis")
    monkeypatch.setattr(ci, "_ensure_redis_acl", lambda *_a, **_k: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(port_preflight, "bind_addrs", lambda _secret: ["127.0.0.1"])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(ci, "print", lambda *_a, **_k: None, raising=False)  # pyright: ignore[reportUnknownArgumentType]
    # `_redis_server_bin` resolves through `brew_prefix`, which shells out to `brew`; the faked
    # `subprocess.run` above would answer it with "PONG". Stub the resolved path directly.
    monkeypatch.setattr(
        ci,
        "_redis_server_bin",
        lambda: "/opt/homebrew/opt/redis@8.2/bin/redis-server",
    )

    # down on the first probe (so the full start path runs), up on the next
    probes = iter([False, True, True])
    monkeypatch.setattr(ci, "_redis_running", lambda *_a: next(probes))  # pyright: ignore[reportUnknownArgumentType]
    assert ci.start_redis(46999, SECRET, SECRET, SECRET, "ava") == 0

    for argv in calls:
        assert_clean(argv, label="redis bring-up")
    conf = tmp_path / "redis" / "redis.conf"
    assert conf.stat().st_mode & 0o777 == 0o600
    assert SECRET in conf.read_text()
