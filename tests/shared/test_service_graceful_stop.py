"""Tests for the graceful stop of a SERVICE session — the path `ava stop` and
`ava cluster update` drive.

The behaviour under test is end-to-end and process-shaped, so these spawn REAL
detached children through the real backend: a service session's login shell must
`exec` into the daemon, so the pid the supervisor records and signals IS the
daemon. While a wrapper shell sat between the two, `proc.terminate()` signalled
the shell — which, waiting on a foreground child, neither forwards SIGTERM nor
exits before that child does — so no daemon ever observed the signal and every
graceful stop ran its full 15s timeout before hard-killing. Sixteen service
sessions stopped serially made that ~4 minutes of every rollout.

POSIX-only (posixproc is the POSIX supervisor) and on the `unit_home` fixture so
records/logs stay under a tmp $AVA_HOME.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import psutil
import pytest

from shared import posixproc
from shared.platform import IS_WINDOWS
from shared.session_backend import PosixProcSessionBackend
from shared.session_env import exec_into

pytestmark = pytest.mark.skipif(IS_WINDOWS, reason="posixproc is the POSIX supervisor")

# A daemon shaped like the real ones: SIGTERM unwinds through `finally`, so the
# marker file proves cleanup ran rather than the process merely dying. `started`
# gates the test on the handler being installed.
#
# Deliberately imports NOTHING from the repo. `_terminate_tree` waits out its
# timeout on the daemon AND every child, so anything the environment attaches to
# a repo-importing child (a CI runner did) would decide the timing instead of the
# signal delivery under test. `install_graceful_shutdown` itself is covered
# in-process by test_install_graceful_shutdown_raises_keyboardinterrupt below.
_DAEMON = """
import signal, sys, time

def _stop(signum, frame):
    raise KeyboardInterrupt

signal.signal(signal.SIGTERM, _stop)
open({started!r}, "w").write("up")
try:
    while True:
        time.sleep(0.05)
except KeyboardInterrupt:
    pass
finally:
    open({marker!r}, "w").write("clean")
"""

# Ignores SIGTERM outright — stands in for a daemon that cannot be stopped
# gracefully (the pty supervisor's documented posture).
_DEAF = """
import signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open({started!r}, "w").write("up")
while True:
    time.sleep(0.05)
"""


def _wait_for(path: Path, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


def _launch(name: str, script: Path, tmp_path: Path) -> None:
    """Start a service session the way the real service path does: through the
    backend with login_shell=True, so the bash -lc / cd / venv / exec wrapper is
    exactly the one production builds."""
    ok = PosixProcSessionBackend().new_session(
        name, f"{sys.executable} {script}", tmp_path, env=dict(os.environ)
    )
    assert ok is True


def _recorded(name: str) -> psutil.Process:
    rec = posixproc._read_record(name)
    assert rec is not None, f"no session record for {name}"
    return psutil.Process(rec.pid)


def test_login_shell_session_records_the_daemon_not_a_wrapper_shell(tmp_path: Path, unit_home):
    """The structural invariant the whole kill layer rests on: the recorded pid
    is the daemon itself, with no shell left between it and the supervisor."""
    del unit_home
    started, marker = tmp_path / "started", tmp_path / "clean"
    script = tmp_path / "daemon.py"
    script.write_text(_DAEMON.format(started=str(started), marker=str(marker)))
    _launch("ava-test-shape", script, tmp_path)
    try:
        assert _wait_for(started), "daemon never started"
        proc = _recorded("ava-test-shape")
        cmdline = " ".join(proc.cmdline())
        assert str(script) in cmdline, f"recorded pid is not the daemon: {cmdline!r}"
        assert "bash" not in proc.name(), (
            f"a wrapper shell survived as the recorded pid: {cmdline!r}"
        )
        # ...and no shell anywhere below it either, which is where a wrapper
        # would reappear if the exec were dropped from an inner layer. Other
        # descendants are legitimate (the browser daemon owns Chrome), so only
        # shells are disqualifying.
        descendants = [(p.pid, p.name()) for p in proc.children(recursive=True)]
        shells = [d for d in descendants if d[1] in {"sh", "bash", "dash", "zsh"}]
        assert not shells, f"a shell survives under the daemon: {shells} (all: {descendants})"
        # The pid survives the exec chain (sh -> bash -> daemon) and so does its
        # start time, so the record's pid-recycling guard still recognises it. If
        # exec broke that, every service would read dead and the watchdog would
        # respawn-loop the whole roster.
        assert posixproc.has_session("ava-test-shape")
    finally:
        posixproc.kill_session("ava-test-shape", graceful=False)


def test_graceful_signal_reaches_the_daemon_and_runs_its_cleanup(tmp_path: Path, unit_home):
    """The fix itself: SIGTERM to the recorded pid arrives at the daemon, which
    unwinds through its `finally` promptly. Before the exec, the signal hit a
    wrapper shell and the daemon never learned anything — the marker never
    appeared and the stop ran its full 15s into a SIGKILL.

    Uses `graceful_signal` (send SIGTERM, no wait, no force) and times the
    daemon's own reaction, so the verdict depends only on signal delivery. Going
    through `kill_session` would additionally depend on the supervised process
    being reaped, and an orphan whose reparent target never reaps it lingers as a
    zombie that `psutil` still counts as running — a property of the host, not of
    this fix. `kill_session`'s graceful/forced mapping is covered separately, on
    a stubbed `_terminate_tree`."""
    del unit_home
    started, marker = tmp_path / "started", tmp_path / "clean"
    script = tmp_path / "daemon.py"
    script.write_text(_DAEMON.format(started=str(started), marker=str(marker)))
    _launch("ava-test-graceful", script, tmp_path)
    try:
        assert _wait_for(started), "daemon never started"
        t0 = time.monotonic()
        assert posixproc.graceful_signal("ava-test-graceful") is True
        assert _wait_for(marker, timeout=10.0), (
            "the daemon never ran its finally — SIGTERM did not reach it"
        )
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0, f"daemon took {elapsed:.1f}s to react to SIGTERM"
    finally:
        posixproc.kill_session("ava-test-graceful", graceful=False)


def test_kill_session_reports_forced_when_the_daemon_ignores_sigterm(tmp_path: Path, unit_home):
    """`mode` is what happened, not what was asked for — otherwise a stop that
    always escalated to SIGKILL reports ✓ (graceful) forever, which is how the
    unreachable-SIGTERM bug stayed invisible across every rollout."""
    del unit_home
    started = tmp_path / "started"
    script = tmp_path / "deaf.py"
    script.write_text(_DEAF.format(started=str(started)))
    _launch("ava-test-deaf", script, tmp_path)
    try:
        assert _wait_for(started), "daemon never started"
        ok, mode = posixproc.kill_session("ava-test-deaf", graceful=True, timeout=1.0)
        assert ok is True, "the SIGKILL fallback must still confirm the process is gone"
        assert mode == "forced"
    finally:
        posixproc.kill_session("ava-test-deaf", graceful=False)


def test_install_graceful_shutdown_raises_keyboardinterrupt():
    """The helper's whole contract: SIGTERM becomes the interrupt the daemons
    already catch around `asyncio.run(run())`, so their `finally` runs. Checked
    in-process (and the handler restored) rather than through a subprocess, so
    nothing about the host's process tree can affect the verdict."""
    import signal

    from shared.daemon_shutdown import install_graceful_shutdown

    previous = signal.getsignal(signal.SIGTERM)
    try:
        install_graceful_shutdown("unit-test")
        cleanup_ran = False
        with pytest.raises(KeyboardInterrupt):
            try:
                os.kill(os.getpid(), signal.SIGTERM)
                # The handler runs at the next bytecode boundary, not inside
                # os.kill; give the interpreter one.
                for _ in range(1000):
                    pass
            finally:
                cleanup_ran = True
        assert cleanup_ran, "the interrupt must unwind through the daemon's finally"
    finally:
        signal.signal(signal.SIGTERM, previous)


# ---------------------------------------------------------------------------
# The command-shape rule every service spec has to satisfy
# ---------------------------------------------------------------------------


def test_exec_into_prefixes_a_simple_command():
    assert exec_into(".venv/bin/python -m gateway") == "exec .venv/bin/python -m gateway"


def test_exec_into_passes_through_a_command_that_execs_itself():
    cmd = "cd ui/web && npm run build && exec npm run start -- -p 3001"
    assert exec_into(cmd) == cmd


def test_frontend_service_command_is_exec_safe():
    """The single frontend command source hands the shell's pid to the serve
    stage — the exec rule every service spec satisfies. The watchdog respawn
    (services/healthchecks/frontend.py) builds this same string, so one check
    covers both launch paths; the 2026-08-27 drift (a respawn command without
    `exec`) was exactly what the validator rejected, leaving a dead frontend
    unable to self-heal."""
    from shared.cluster import frontend_service_cmd

    cmd = frontend_service_cmd(3001)
    assert "exec npm run start -- -p 3001" in cmd
    assert exec_into(cmd) == cmd


@pytest.mark.parametrize(
    "cmd",
    [
        'echo "$PATH" > /tmp/path.txt',  # a redirection is still one command
        'curl "http://host/x?a=1&b=2"',  # `&` inside a quoted argument
    ],
)
def test_exec_into_allows_single_commands_that_merely_look_compound(cmd: str):
    """The guard must not over-reject: a hard launch failure for a command that
    would have worked is worse than the stall it protects against."""
    assert exec_into(cmd) == f"exec {cmd}"


@pytest.mark.parametrize(
    "cmd",
    [
        'python -c "exec (x)"',  # `exec` inside the script the command runs
        'foo --mode "exec now"',  # `exec` inside a quoted argument
    ],
)
def test_exec_into_does_not_mistake_a_stray_exec_word_for_a_handover(cmd: str):
    """Only an `exec` on the stage that ends up supervised is a hand-off.

    Matching the word anywhere would return these unwrapped, which is the one
    outcome with no symptom at the call site — the wrapper shell survives and
    swallows the graceful-stop SIGTERM, and the service goes back to costing a
    full timeout with no cleanup. Everything unrecognised is prefixed or
    rejected; both are loud.
    """
    assert exec_into(cmd) == f"exec {cmd}"


def test_exec_into_rejects_an_exec_that_is_not_the_final_stage():
    """`exec build && serve` never runs `serve` — the exec'd stage replaces the
    shell. Not a hand-off, and not silently accepted as one."""
    with pytest.raises(ValueError, match="compound but never execs"):
        exec_into("exec npm run build && npm run start")


def test_exec_into_rejects_a_compound_command_that_never_execs():
    """Silence here would cost a service its cleanup and 15s of every rollout —
    invisible in the stop output, so it has to fail at launch instead."""
    with pytest.raises(ValueError, match="compound but never execs"):
        exec_into("npm run build && npm run start")


def test_orchestration_session_keeps_its_wrapper_shell(monkeypatch: pytest.MonkeyPatch, unit_home):
    """`exec_cmd=False` is the opt-out for a session whose shell is doing the
    work — the updater's `tee` pipeline and its `[session-exit] rc=` verdict have
    to outlive the command, so that shell must NOT exec itself away."""
    del unit_home
    seen: list[str] = []

    def _fake_new(_name: str, cmd: str, _cwd: Path, **_kw: object) -> bool:
        seen.append(cmd)
        return True

    monkeypatch.setattr(posixproc, "new_session", _fake_new)
    backend = PosixProcSessionBackend()
    pipeline = "ava cluster update | tee -a log"
    assert backend.new_session("ava-updater", pipeline, Path("/repo"), env={}, exec_cmd=False)
    # The pipeline is left for the login shell to run and outlive...
    assert seen[0].endswith(f"&& {pipeline}'")
    assert f"exec {pipeline}" not in seen[0]
    # ...while the outer exec still collapses the supervisor's `/bin/sh -c`, so
    # the recorded pid is that login shell rather than an `sh` in front of it.
    assert seen[0].startswith("exec bash -lc ")


def test_every_service_spec_command_is_exec_safe():
    """The roster gate: a new spec whose command would strand a wrapper shell
    fails here rather than silently reintroducing the full-timeout stop."""
    from ops.roster import build_services

    for spec in build_services():
        exec_into(spec.cmd)  # raises if the command could not hand over its pid
