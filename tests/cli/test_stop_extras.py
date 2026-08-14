"""`ava stop` full-stop extras (cli/commands/_stop_extras.py:stop_gate_service /
stop_permissions_helper) + the verified process teardown they share with
PgBouncer (cli/commands/_pgbouncer.py:_terminate_verified).

The gap these close (preview teardown 2026-08-06): converge registers the gate
and the permissions helper OUTSIDE the service roster — a launchd KeepAlive job on macOS, a
detached pidfile daemon on other POSIX — so the service loop never sees them, and
a plain `kill` does not stick on the launchd ones (KeepAlive respawns). A full
`ava stop` must tear them down or they outlive the stop holding their ports /
sockets. cmd_update / cmd_restart pass teardown_extras=False and never reach
these, so the gate keeps the entry port live through a rollout.

**The `_terminate_verified` cases drive REAL child processes.** They used to run
against a fake `os.kill` table, and that is precisely how this function shipped
three POSIX-only spellings — `os.kill(pid, SIGTERM)`, `os.kill(pid, 0)` as a
liveness probe, `signal.SIGKILL` — into a path `_do_stop` takes on every platform:
a stub that answers `os.kill` makes the POSIX branch and the Windows branch
indistinguishable, so the test passed on macOS while the real call raised
`[WinError 87]` on win (2026-08-12). `shared.winproc` states the same rule for its
own constants. A real `sys.executable` child cannot be faked into hiding that: it
exercises whatever `shared.proc` actually dispatches to on the running platform.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from cli.commands._pgbouncer import _terminate_verified
from cli.commands._stop_extras import stop_gate_service, stop_permissions_helper

# -- _terminate_verified ------------------------------------------------------


def _detached_sleeper(tmp_path: Path, *, ignores_term: bool = False) -> int:
    """A real process that is deliberately **not this test's child**, and its pid.

    `_terminate_verified` only ever targets daemons — the pooler, an orphan port
    listener, the gate — never a child of the caller, and the difference is
    load-bearing for the assertions below: a killed CHILD becomes a zombie until its
    parent waits, and a zombie answers every liveness probe as alive. Spawning
    through a launcher that exits immediately reparents the sleeper away, so the test
    measures the stop rather than pytest's failure to reap.

    `ignores_term` installs a SIGTERM handler that does nothing — the straggler shape
    the force-kill rung exists for (a pooler draining live clients). POSIX-only by
    nature, since Windows has no catchable termination.

    The sleeper touches a ready marker as its LAST setup step and the caller waits
    for it, because the handler is what the straggler case is about: signalled in the
    window before `signal.signal` runs, the process takes the default action and dies
    politely, and the test silently measures the graceful rung instead of the forced
    one. It failed exactly that way before the marker existed.
    """
    ready = tmp_path / "sleeper-ready"
    setup = "signal.signal(signal.SIGTERM, lambda *_: None)\n" if ignores_term else ""
    child = (
        f"import pathlib, signal, time\n{setup}"
        f"pathlib.Path({str(ready)!r}).write_text('1')\n"
        "time.sleep(60)\n"
    )
    # The sleeper's stdio goes to DEVNULL, not to the launcher's inherited pipes:
    # `capture_output` below waits for EOF on stdout, and a grandchild holding the
    # write end open would keep it from ever arriving.
    launcher = (
        "import subprocess, sys\n"
        f"p = subprocess.Popen([sys.executable, '-c', {child!r}], stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print(p.pid)\n"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", launcher], capture_output=True, text=True, check=True, timeout=30
    )
    pid = int(done.stdout.strip())
    deadline = time.monotonic() + 30.0
    while not ready.exists():
        assert time.monotonic() < deadline, "the fixture process never became ready"
        time.sleep(0.02)
    return pid


def _reap(pid: int) -> None:
    """Leave no strays behind, whatever the assertion did."""
    with contextlib.suppress(Exception):
        psutil.Process(pid).kill()


def _wait_gone(pid: int, *, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(0.05)
    return False


def test_terminate_verified_graceful(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """The polite rung: a process that honours the stop request is gone before the
    grace period is up, and never reaches the force kill."""
    pid = _detached_sleeper(tmp_path)
    try:
        assert _terminate_verified(pid, label="pgbouncer") is True
        assert "✓ pgbouncer stopped" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    finally:
        _reap(pid)


def test_terminate_verified_already_gone(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """A pid that died between the pidfile read and here is a stop, not a failure —
    the race the pidfile path cannot close is covered by counting it as stopped."""
    pid = _detached_sleeper(tmp_path)
    psutil.Process(pid).kill()
    assert _wait_gone(pid), "the fixture process did not exit"

    assert _terminate_verified(pid, label="pgbouncer") is True
    assert "✓ pgbouncer stopped" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no catchable SIGTERM")
def test_terminate_verified_forced(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """A process that ignores the stop request is force-killed after the grace period
    and reported as forced — the caller must be able to tell the two apart."""
    pid = _detached_sleeper(tmp_path, ignores_term=True)
    try:
        assert _terminate_verified(pid, label="pgbouncer", timeout_s=0.3) is True
        assert "⚠ pgbouncer stopped (forced kill)" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    finally:
        _reap(pid)


def test_terminate_verified_survivor_is_never_reported_as_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The one contract the caller acts on (Task #965): a process this could not
    verify as gone returns False and says so on stderr, so `ava stop` reports the
    stop incomplete instead of claiming a success it did not see.

    The survivor is simulated at the `shared.proc` seam rather than with a real
    unkillable process — there is no portable way to spawn one — but the seam is the
    real dispatch point, so the escalation above it is the production path."""
    pid = _detached_sleeper(tmp_path)
    try:

        def _never_gone(_pid: int) -> bool:
            return True

        # Patched where it is looked up: `_pgbouncer` imports the name at module
        # scope, so patching `shared.proc` would leave that binding untouched.
        monkeypatch.setattr("cli.commands._pgbouncer.process_alive", _never_gone)
        assert _terminate_verified(pid, label="pgbouncer", timeout_s=0.1) is False
        assert "survived the force kill" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    finally:
        _reap(pid)


def test_terminate_verified_uses_no_raw_posix_signal_calls() -> None:
    """The regression guard for the defect itself, asserted on the source rather
    than on behaviour: on the platform CI runs, a raw `os.kill` works and a test of
    behaviour alone would go on passing while the Windows path stays broken. The
    three spellings named here are the ones that do not survive the crossing, and
    `shared.proc` is where each already has a working twin."""
    import inspect

    from cli.commands import _pgbouncer

    source = inspect.getsource(_pgbouncer._terminate_verified)
    body = source.split('"""')[-1]  # the docstring names them to explain them
    for spelling in ("os.kill", "SIGKILL", "SIGTERM"):
        assert spelling not in body, f"{spelling} is back on the stop path"


# -- stop_gate_service --------------------------------------------------------


def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / ".ava"
    home.mkdir()
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)
    return home


def test_gate_macos_bootout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr("shared.platform.IS_MACOS", True)
    calls: list[str] = []

    monkeypatch.setattr("cli.commands._converge_gate._job_loaded", lambda _label: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        "cli.commands._converge_gate._bootout_and_wait",
        lambda _label: calls.append(_label) or True,  # pyright: ignore[reportUnknownArgumentType]
    )
    stop_gate_service()
    assert calls and calls[0].startswith("com.ava.gate.")
    assert "✓ gate launchd job booted out" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]


def test_gate_macos_not_loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr("shared.platform.IS_MACOS", True)
    booted: list[str] = []
    monkeypatch.setattr("cli.commands._converge_gate._job_loaded", lambda _label: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        "cli.commands._converge_gate._bootout_and_wait",
        lambda _label: booted.append(_label) or True,  # pyright: ignore[reportUnknownArgumentType]
    )
    stop_gate_service()
    assert booted == []
    assert "booted out" not in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]


def _posix_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pid: str
) -> tuple[Path, list[int]]:
    """A non-macOS home whose `run/gate.pid` names `pid`, plus the kill recorder."""
    home = _home(monkeypatch, tmp_path)
    (home / "run").mkdir()
    (home / "run" / "gate.pid").write_text(pid)
    monkeypatch.setattr("shared.platform.IS_MACOS", False)
    killed: list[int] = []
    monkeypatch.setattr(
        "cli.commands._pgbouncer._terminate_verified",
        lambda pid, **_kw: killed.append(pid),  # pyright: ignore[reportUnknownArgumentType]
    )
    return home, killed


def test_gate_posix_pidfile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home, killed = _posix_gate(monkeypatch, tmp_path, "1234")
    monkeypatch.setattr("cli.commands._converge_gate.gate_pid_is_ours", lambda _pid, _repo: True)  # pyright: ignore[reportUnknownArgumentType]
    stop_gate_service()
    assert killed == [1234]
    assert not (home / "run" / "gate.pid").exists()


def test_gate_posix_unconfirmed_pid_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A pid this checkout cannot confirm is neither signalled nor forgotten.

    Leaving the pidfile is the load-bearing half. `ava cluster down --path` runs
    this against another home while `repo_root()` is still the invoking checkout,
    so "unconfirmed" can mean "that home's gate, launched by its own checkout" —
    deleting the file there would strand a live daemon nothing can find again.
    Clearing a genuinely stale file is converge's job, where the repo is known."""
    home, killed = _posix_gate(monkeypatch, tmp_path, "9999")
    monkeypatch.setattr("cli.commands._converge_gate.gate_pid_is_ours", lambda _pid, _repo: False)  # pyright: ignore[reportUnknownArgumentType]
    stop_gate_service()
    assert killed == []
    assert (home / "run" / "gate.pid").exists()
    assert "not signalling it" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_gate_posix_recycled_pid_is_not_signalled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pidfile names a LIVE process that is not this checkout's gate daemon.

    The stale number is this test's own interpreter — really alive, definitively
    not a gate — so the ownership check runs for real rather than through a stub.
    Trusted bare, this pid would take a SIGTERM and then a SIGKILL."""
    import os

    _, killed = _posix_gate(monkeypatch, tmp_path, str(os.getpid()))
    stop_gate_service()
    assert killed == []


def test_gate_posix_unparseable_pidfile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home, killed = _posix_gate(monkeypatch, tmp_path, "not-a-pid")
    stop_gate_service()
    assert killed == []
    assert not (home / "run" / "gate.pid").exists()


# -- stop_permissions_helper --------------------------------------------------


def test_helper_macos_bootout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr("shared.platform.IS_MACOS", True)
    calls: list[str] = []
    monkeypatch.setattr("cli.commands._converge_gate._job_loaded", lambda _label: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        "cli.commands._converge_gate._bootout_and_wait",
        lambda _label: calls.append(_label) or True,  # pyright: ignore[reportUnknownArgumentType]
    )
    stop_permissions_helper()
    assert calls and calls[0].startswith("com.ava.permissions-helper.")
    assert "✓ permissions-helper launchd job booted out" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]


def test_helper_non_macos_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr("shared.platform.IS_MACOS", False)
    calls: list[str] = []
    monkeypatch.setattr(
        "cli.commands._converge_gate._job_loaded",
        lambda _label: calls.append(_label) or True,  # pyright: ignore[reportUnknownArgumentType]
    )
    stop_permissions_helper()
    assert calls == []


def test_gate_teardown_not_used_by_update(monkeypatch: pytest.MonkeyPatch) -> None:
    """cmd_update reaches _do_stop with teardown_extras=False — the gate must
    survive updates by construction. Pin the default so a future call site
    cannot silently flip the full-stop extras on."""
    import inspect

    from cli.commands.stop import _do_stop

    sig = inspect.signature(_do_stop)
    assert sig.parameters["teardown_extras"].default is False


@pytest.mark.skipif(sys.platform == "win32", reason="PermissionError on signal is the POSIX shape")
def test_a_pid_this_user_may_not_signal_is_reported_as_a_survivor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The POSIX twin of the WinError 87 this PR is about: a process that passes the
    caller's ownership check but refuses our signal (a stray left by a `sudo` run of
    this same checkout). Every leg used to raise `PermissionError` straight out of
    `_do_stop` — the exact shape being removed — so the whole stop died on one
    unsignalable stray instead of reporting it.

    The verdict has to be `False`: nothing was delivered, so nothing is verified."""
    import shared.proc as sp

    pid = _detached_sleeper(tmp_path)
    try:
        real_kill = os.kill

        def _refuse(target: int, sig: int) -> None:
            if target == pid and sig != 0:
                raise PermissionError(1, "Operation not permitted")
            real_kill(target, sig)

        monkeypatch.setattr(sp.os, "kill", _refuse)

        assert _terminate_verified(pid, label="pgbouncer", timeout_s=0.2) is False
        assert "survived the force kill" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
        assert psutil.pid_exists(pid), "the stop must not have killed it by another route"
    finally:
        _reap(pid)
