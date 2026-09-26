"""Tests for ``shared.session_backend.PosixProcSessionBackend`` — the native
POSIX backend wrapping ``shared.posixproc``, migration target for service
sessions.

Most of these spawn REAL child processes (the double-fork reparent path is the
whole point of the supervisor), so they are POSIX-only and use the ``unit_home``
fixture to keep records/logs under a tmp $AVA_HOME — the same shape as
tests/shared/test_posixproc.py. Command-shape tests monkeypatch the supervisor
instead.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from shared import posixproc
from shared.platform import IS_WINDOWS
from shared.session_backend import (
    PosixProcSessionBackend,
    PtySessionBackend,
    SessionBackend,
    WinprocSessionBackend,
    get_shell_backend,
)
from tests.shared.poll_until import poll_until

pytestmark = pytest.mark.skipif(IS_WINDOWS, reason="posixproc is the POSIX supervisor")

# A long-lived child that outlives the test body; each test kills it explicitly.
_SLEEP = "/bin/sleep 300"


def _backend() -> PosixProcSessionBackend:
    return PosixProcSessionBackend()


def _pid(name: str) -> int:
    """The recorded pid for a live session (asserts the record exists)."""
    rec = posixproc._read_record(name)
    assert rec is not None
    return rec.pid


# ---------------------------------------------------------------------------
# Command shape (login_shell semantics) — monkeypatched supervisor
# ---------------------------------------------------------------------------


def test_new_session_login_shell_wraps(monkeypatch: pytest.MonkeyPatch, unit_home):
    """login_shell=True (the default) builds the bash -lc / cd / venv shape,
    while the env rides a real dict — no 0600 sourced-file prefix, no secret
    on any argv."""
    calls: list[tuple] = []

    def fake_new(name, cmd, cwd, *, env, stderr_append=None):
        calls.append((name, cmd, cwd, env))  # pyright: ignore[reportUnknownMemberType]
        return True

    monkeypatch.setattr(posixproc, "new_session", fake_new)  # pyright: ignore[reportUnknownArgumentType]

    env = {
        "AVA_HOME": str(unit_home),  # pyright: ignore[reportUnknownArgumentType]
        "PATH": "/bin",
        "AVA_CLUSTER_SECRET": "top-secret-value",
    }
    ok = _backend().new_session(
        "ava-main-gateway", ".venv/bin/python -m gateway", Path("/repo"), env=env
    )
    assert ok is True
    assert len(calls) == 1  # pyright: ignore[reportUnknownArgumentType]
    name, cmd, cwd, got_env = calls[0]
    assert name == "ava-main-gateway"
    assert cwd == Path("/repo")
    # env passes through untouched — the child inherits it as its real
    # environment (the legacy sourced-file mechanism existed only to keep values
    # off argv; posixproc has no such argv).
    assert got_env == env
    assert "top-secret-value" not in cmd
    # bash -lc wrapper, legacy-style: cd + venv re-activation inside the command
    argv = shlex.split(cmd)  # pyright: ignore[reportUnknownArgumentType]
    # Two execs, one per shell layer, so neither survives to swallow the
    # graceful-stop SIGTERM. The outer one is what collapses the supervisor's
    # own `/bin/sh -c` (dash does not exec into a lone command by itself, so
    # without it every Linux host records that `sh` instead of the daemon).
    assert argv[:3] == ["exec", "bash", "-lc"]
    inner = argv[3]
    assert inner.startswith("cd /repo && ")
    assert "export VIRTUAL_ENV=" in inner
    assert "export PATH=" in inner
    # The inner one hands the login shell's pid to the daemon itself.
    assert inner.endswith("exec .venv/bin/python -m gateway")
    # ... and NOT the legacy env-load prefix (set -a; . <file>; rm -f)
    assert "set -a" not in cmd
    assert "rm -f " not in cmd


def test_new_session_no_login_shell_passthrough(monkeypatch: pytest.MonkeyPatch):
    """Without login_shell the command is handed through unchanged — the
    caller owns PATH/venv semantics, same as the legacy backend's no-login path."""
    captured: dict[str, object] = {}

    def fake_new(name, cmd, cwd, *, env, stderr_append=None):
        captured["cmd"] = cmd
        return True

    monkeypatch.setattr(posixproc, "new_session", fake_new)  # pyright: ignore[reportUnknownArgumentType]

    ok = _backend().new_session(
        "ava-svc", "mycmd --flag", Path("/repo"), env={"PATH": "/bin"}, login_shell=False
    )
    assert ok is True
    assert captured["cmd"] == "mycmd --flag"


def test_posix_backend_keeps_its_own_log_file():
    """The native supervisor owns a session log file (liveness consumers can
    watch it); the abstract default answers None — a backend without a file."""
    from shared.session_backend import SessionBackend

    posix_be = _backend()
    assert posix_be.session_log_path("ava-x") is not None
    assert SessionBackend.session_log_path(posix_be, "ava-x") is None


# ---------------------------------------------------------------------------
# Spawn / kill lifecycle — real processes through the backend
# ---------------------------------------------------------------------------


def test_spawn_and_kill_idempotent(unit_home):
    """new_session twice on a live name is a no-op (same pid); kill confirms
    the session gone; a second kill is a noop."""
    backend = _backend()
    name = "ava-test-posixbe-1"
    assert backend.new_session(name, _SLEEP, unit_home, env=dict(os.environ)) is True  # pyright: ignore[reportUnknownArgumentType]
    try:
        first_pid = _pid(name)
        assert backend.has_session(name) is True
        assert backend.new_session(name, _SLEEP, unit_home, env=dict(os.environ)) is True  # pyright: ignore[reportUnknownArgumentType]
        assert _pid(name) == first_pid  # not relaunched

        ok, mode = backend.kill_session(name, graceful=False)
        assert ok is True and mode == "forced"
        assert backend.has_session(name) is False
        assert not posixproc._record_path(name).exists()

        ok, mode = backend.kill_session(name, graceful=False)
        assert ok is True and mode == "noop"
    finally:
        backend.kill_session(name, graceful=False)


def test_kill_session_accepts_expected_for_interface_parity(unit_home):
    """kill_session takes the `expected` kwarg callers pass on rollout/stop
    paths (the ABC requires it) and ignores it — the native supervisor has no
    force-kill escalation to quieten."""
    ok, mode = _backend().kill_session("ava-test-posixbe-ghost", expected=True)
    assert ok is True and mode == "noop"


def test_tree_kill_leaves_no_orphans(unit_home):
    """A force kill takes down the session's own descendants too, not just the
    top process — the tree kill the legacy SIGKILL escalation used to provide."""
    script = unit_home / "spawn_child.py"
    script.write_text(  # pyright: ignore[reportUnknownMemberType]
        "import subprocess, time\nsubprocess.Popen(['sleep', '300'])\ntime.sleep(300)\n"
    )
    name = "ava-test-posixbe-tree"
    cmd = f"{sys.executable} {shlex.quote(str(script))}"  # pyright: ignore[reportUnknownArgumentType]
    backend = _backend()
    assert backend.new_session(name, cmd, unit_home, env=dict(os.environ)) is True  # pyright: ignore[reportUnknownArgumentType]
    parent_pid = _pid(name)

    # Find the `sleep` descendant in ONE snapshot, capturing its pid inside the
    # poll. The login shell forks short-lived rc children on the way up, so
    # "children() is non-empty" and a later children()[0] are different
    # snapshots — the transient child can be gone in between (an empty list →
    # IndexError, seen on CI). The sleep is a grandchild (bash → python →
    # sleep), so search recursively for the process we actually spawned.
    sleep_pids: list[int] = []

    def _find_sleep() -> bool:
        try:
            descendants = psutil.Process(parent_pid).children(recursive=True)
        except psutil.NoSuchProcess:
            return False
        for proc in descendants:
            try:
                if proc.name() == "sleep":
                    sleep_pids.append(proc.pid)
                    return True
            except psutil.NoSuchProcess:
                continue  # exited between listing and name() — keep looking
        return False

    poll_until(_find_sleep, what="spawned sleep descendant appears")
    child_pid = sleep_pids[-1]

    ok, mode = backend.kill_session(name, graceful=False)
    assert ok is True and mode == "forced"
    poll_until(
        lambda: (not psutil.pid_exists(parent_pid), psutil.pid_exists(parent_pid)),
        what="tree-killed parent process exits",
    )
    poll_until(
        lambda: (not psutil.pid_exists(child_pid), psutil.pid_exists(child_pid)),
        what="tree-killed sleep descendant exits",
    )


def test_pid_reuse_protection(unit_home):
    """A record whose pid now belongs to a DIFFERENT process (stale
    create_time) is not a live session — the liveness check defeats pid
    recycling by the OS."""
    backend = _backend()
    name = "ava-test-posixbe-pidreuse"
    # An unrelated long-lived process we control, standing in for the recycled pid.
    proc = subprocess.Popen(["/bin/sleep", "300"])
    try:
        # Forge a record claiming that pid with a create_time from the distant past.
        rec = posixproc.SessionRecord(
            pid=proc.pid,
            create_time=1.0,
            cmd="sleep",
            cwd=str(unit_home),  # pyright: ignore[reportUnknownArgumentType]
            started_at=1.0,
        )
        rec.write(posixproc._record_path(name))
        assert backend.has_session(name) is False
        assert name not in backend.list_sessions(prefix="ava-test-posixbe-")
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_session_record_reclaimed(unit_home):
    """A record whose process died out from under it is unlinked as
    list_sessions walks, so the listing reflects reality."""
    backend = _backend()
    name = "ava-test-posixbe-reclaim"
    assert backend.new_session(name, _SLEEP, unit_home, env=dict(os.environ)) is True  # pyright: ignore[reportUnknownArgumentType]
    pid = _pid(name)
    # Kill the process WITHOUT going through kill_session, so the record lingers.
    proc = psutil.Process(pid)
    proc.kill()
    proc.wait(timeout=5)
    assert posixproc._record_path(name).exists()
    assert backend.list_sessions(prefix="ava-test-posixbe-") == []
    assert not posixproc._record_path(name).exists()


# ---------------------------------------------------------------------------
# login_shell PATH semantics — real login shell vs bare sh
# ---------------------------------------------------------------------------


def test_login_shell_rebuilds_path_with_venv_prefix(unit_home):
    """With login_shell=True the command runs under bash -lc: the login
    profile rebuilds PATH (the forwarded PATH is NOT authoritative), and the
    venv bin dir is re-added inside the command — the exact PATH/venv semantics
    the legacy path provided."""
    backend = _backend()
    out = unit_home / "path-login.txt"
    out.unlink(missing_ok=True)  # pyright: ignore[reportUnknownMemberType]
    env = dict(os.environ)
    # A marker prefix on a still-working PATH: the login profile must DROP the
    # forwarded prefix (it rebuilds PATH from scratch) while bash stays findable.
    env["PATH"] = "/custom-only/bin:" + os.environ["PATH"]
    name = "ava-test-posixbe-path1"
    # Write via tmp + rename: `>` creates the file empty before echo fills it,
    # so a bare redirect lets poll_until(out.exists) observe an empty window.
    tmp = out.with_suffix(".tmp")  # pyright: ignore[reportUnknownMemberType]
    cmd = f'echo "$PATH" > {shlex.quote(str(tmp))} && exec /bin/mv {shlex.quote(str(tmp))} {shlex.quote(str(out))}'  # pyright: ignore[reportUnknownArgumentType]
    assert backend.new_session(name, cmd, unit_home, env=env) is True  # pyright: ignore[reportUnknownArgumentType]
    try:
        poll_until(
            out.exists,  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            what="login-shell PATH output appears",
        )
        path = out.read_text().strip()  # pyright: ignore[reportUnknownMemberType]
        # the venv was re-activated after the profile ran
        assert ".venv/bin" in path, f"venv bin missing from login-shell PATH: {path!r}"
        # the login profile actually ran (path_helper / /etc/profile dirs)
        assert "/usr/bin" in path, f"login profile dirs missing from PATH: {path!r}"
        # the forwarded marker prefix was dropped — the profile rebuilt PATH
        assert not path.startswith("/custom-only/bin"), f"forwarded PATH survived: {path!r}"  # pyright: ignore[reportUnknownMemberType]
    finally:
        backend.kill_session(name, graceful=False)


def test_no_login_shell_preserves_env_path_exactly(unit_home):
    """With login_shell=False the command runs through plain sh -c with the
    caller's env authoritative — no profile sourcing, no PATH rewrite."""
    backend = _backend()
    out = unit_home / "path-nologin.txt"
    out.unlink(missing_ok=True)  # pyright: ignore[reportUnknownMemberType]
    env = dict(os.environ)
    env["PATH"] = "/custom-only/bin"
    name = "ava-test-posixbe-path2"
    # tmp + rename for the same empty-window reason as the login-shell test
    tmp = out.with_suffix(".tmp")  # pyright: ignore[reportUnknownMemberType]
    cmd = f'echo "$PATH" > {shlex.quote(str(tmp))} && exec /bin/mv {shlex.quote(str(tmp))} {shlex.quote(str(out))}'  # pyright: ignore[reportUnknownArgumentType]
    assert (
        backend.new_session(
            name,
            cmd,
            unit_home,  # pyright: ignore[reportUnknownArgumentType]
            env=env,
            login_shell=False,
        )
        is True
    )
    try:
        poll_until(
            out.exists,  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            what="no-login-shell PATH output appears",
        )
        assert out.read_text().strip() == "/custom-only/bin"  # pyright: ignore[reportUnknownMemberType]
    finally:
        backend.kill_session(name, graceful=False)


# ---------------------------------------------------------------------------
# Interface surface
# ---------------------------------------------------------------------------


def test_send_keys_and_capture_pane_raise():
    """No PTY: the interactive methods raise, like the Windows backend."""
    backend = _backend()
    with pytest.raises(NotImplementedError):
        backend.send_keys("s", "C-c")
    with pytest.raises(NotImplementedError):
        backend.capture_pane("s")


def test_backend_is_a_session_backend():
    assert isinstance(_backend(), SessionBackend)


def test_has_session_false_for_unknown(unit_home):
    assert _backend().has_session("ava-test-posixbe-unknown") is False


def test_get_shell_backend_returns_pty_backend():
    """get_shell_backend() is the PTY backend for agent shells/watchers —
    the self-hosted PTY supervisor on POSIX (S6 step 2), winproc on Windows —
    a singleton distinct from the get_backend() singleton (service sessions
    live on the native backend; interactive shells on the PTY one)."""
    from shared.session_backend import get_backend

    b1 = get_shell_backend()
    b2 = get_shell_backend()
    assert b1 is b2  # singleton
    if IS_WINDOWS:
        assert isinstance(b1, WinprocSessionBackend)
    else:
        assert isinstance(b1, PtySessionBackend)
    assert isinstance(b1, SessionBackend)
    assert b1 is not get_backend()
