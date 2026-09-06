"""services.browser.daemon — capability assertion, arg construction, and the
exec entrypoint. The three-prong capability check (display + Chrome + npx) now
lives in shared.platform_probes.browser_incapability (tested per-prong in
tests/shared/test_platform_probes.py); assert_browser_capable is a thin raising
wrapper over it, so these tests patch browser_incapability as bound in the daemon
module. The real exec is not unit-tested (it replaces the process); the testable
surface is the pure helpers, the launch ordering, and `_launch`'s platform split
— exec-in-place on POSIX vs supervise-the-browser on Windows, which is
parametrized over `bd.IS_WINDOWS` so both branches run on a macOS/Linux test host.

The Windows branch's central claim is that Chrome's launched process is not
always the process that stays alive, so CDP reachability — not the child's exit —
decides whether the browser is gone. Every Windows-branch test therefore pins its
CDP answer (`_cdp_confirmed_gone`, or `_cdp_reachable` under it): leaving the
probe live would let a unit test dial port 9222 for real.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import services.browser.daemon as bd
from shared.config import settings


def test_assert_capable_raises_reason_with_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """assert_browser_capable raises the browser_incapability reason, prefixed
    with the ava-browser tag, so a launch on an incapable host dies loudly with
    the same wording `ava status` shows."""
    monkeypatch.setattr(bd, "browser_incapability", lambda: "no display (headless server)")
    with pytest.raises(RuntimeError, match=r"ava-browser: no display"):
        bd.assert_browser_capable()


def test_assert_capable_ok_when_capable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bd, "browser_incapability", lambda: None)
    bd.assert_browser_capable()  # no raise


def test_chrome_args_has_debug_port_and_profile(tmp_path: Path) -> None:
    args = bd._chrome_args("/chrome", 9222, tmp_path / "prof")
    assert args[0] == "/chrome"
    assert "--remote-debugging-port=9222" in args
    assert f"--user-data-dir={tmp_path / 'prof'}" in args
    assert "--no-first-run" in args
    assert not any(a.startswith("--headless") for a in args)


def test_main_asserts_capable_then_execvps_chrome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """main() must fail-loud-check capability before launching, then exec the
    resolved Chrome with the configured port but no automatic page."""
    order: list[str] = []
    monkeypatch.setattr(bd, "assert_browser_capable", lambda: order.append("capable"))
    monkeypatch.setattr(
        bd,
        "_cdp_reachable",
        lambda _p: (order.append("port"), False)[1],  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(bd, "resolve_chrome_binary", lambda: "/chrome")

    def _profile() -> Path:
        order.append("profile")
        return tmp_path / "prof"

    monkeypatch.setattr(bd, "_profile_dir", _profile)
    monkeypatch.setattr(
        bd.macos_readiness, "wait_for_browser_startup_readiness", lambda: order.append("ready")
    )
    monkeypatch.setattr(
        bd.browser_profile,
        "validate_local_state",
        lambda _profile: None,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(bd, "logs_dir", lambda: tmp_path)
    monkeypatch.setattr(settings.services, "app_port", 3001)
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://10.0.0.72:8000")
    monkeypatch.setattr(bd.os, "open", lambda *_a, **_k: 99)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(bd.os, "dup2", lambda *_a: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(bd.os, "close", lambda *_a: None)  # pyright: ignore[reportUnknownArgumentType]
    captured_file = ""
    captured_args: list[str] = []

    def _fake_execvp(file: str, args: list[str]) -> None:
        nonlocal captured_file, captured_args
        order.append("exec")
        captured_file = file
        captured_args = args

    monkeypatch.setattr(bd.os, "execvp", _fake_execvp)
    bd.main()
    assert order == ["capable", "port", "ready", "profile", "exec"]
    assert captured_file == "/chrome"
    # main() takes the port from settings, and a cluster gets one out of its own
    # port block — a literal here only holds on a default-home install.
    assert f"--remote-debugging-port={settings.services.browser_cdp_port}" in captured_args
    # Even a gateway host with an app port must leave Chrome's first page alone.
    assert len([arg for arg in captured_args if not arg.startswith("--")]) == 1


def test_main_propagates_capability_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> None:
        raise RuntimeError("no display")

    monkeypatch.setattr(bd, "assert_browser_capable", _boom)
    with pytest.raises(RuntimeError, match="no display"):
        bd.main()


def test_cdp_reachable_true_on_200(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = MagicMock(status=200)
    cm = MagicMock()
    cm.__enter__.return_value = resp
    monkeypatch.setattr(bd.urllib.request, "urlopen", lambda *_a, **_k: cm)  # pyright: ignore[reportUnknownArgumentType]
    assert bd._cdp_reachable(9222) is True


def test_cdp_reachable_false_on_connection_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refused(*_a: object, **_k: object) -> None:
        raise bd.urllib.error.URLError("connection refused")

    monkeypatch.setattr(bd.urllib.request, "urlopen", _refused)
    assert bd._cdp_reachable(9222) is False


# ─── _cdp_confirmed_gone: bounded, and one reachable answer is enough ─────


def test_cdp_confirmed_gone_false_on_first_reachable_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live CDP endpoint ends the confirmation immediately — no sleeping, no
    further probes. This is the handoff verdict: Chrome is alive."""
    probes: list[int] = []
    monkeypatch.setattr(bd, "_cdp_reachable", lambda p: probes.append(p) or True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(bd.time, "sleep", lambda _s: pytest.fail("must not sleep when CDP is up"))  # pyright: ignore[reportUnknownArgumentType]
    assert bd._cdp_confirmed_gone(9222) is False
    assert probes == [9222]


def test_cdp_confirmed_gone_tolerates_a_gap_before_the_port_is_rebound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detached Chrome may need a moment to rebind the port after its launcher
    exits, so a single refused probe must not be the verdict."""
    answers = iter([False, False, True])
    monkeypatch.setattr(bd, "_cdp_reachable", lambda _p: next(answers))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(bd.time, "sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]
    assert bd._cdp_confirmed_gone(9222) is False


def test_cdp_confirmed_gone_true_after_bounded_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail fast: a permanently dead endpoint is confirmed gone after at most
    `_CDP_CONFIRM_ATTEMPTS` probes, never retried forever."""
    probes: list[int] = []
    monkeypatch.setattr(bd, "_cdp_reachable", lambda p: bool(probes.append(p)))  # pyright: ignore[reportUnknownArgumentType]
    slept: list[float] = []
    monkeypatch.setattr(bd.time, "sleep", slept.append)
    assert bd._cdp_confirmed_gone(9222) is True
    assert len(probes) == bd._CDP_CONFIRM_ATTEMPTS
    assert len(slept) == bd._CDP_CONFIRM_ATTEMPTS - 1  # no trailing sleep before the verdict


def test_cdp_confirmed_gone_stops_at_the_wall_clock_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second bound: slow probes must not stretch the window to
    attempts x timeout. Once the deadline has passed, no new probe is issued."""
    ticks = [0.0, bd._CDP_CONFIRM_DEADLINE_S + 1.0]  # deadline is set, then already blown

    def _clock() -> float:
        return ticks.pop(0) if len(ticks) > 1 else ticks[0]

    monkeypatch.setattr(bd.time, "monotonic", _clock)
    probes: list[int] = []
    monkeypatch.setattr(bd, "_cdp_reachable", lambda p: bool(probes.append(p)))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        bd.time,
        "sleep",
        lambda _s: pytest.fail("deadline reached: no more waiting"),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert bd._cdp_confirmed_gone(9222) is True
    assert probes == [9222]  # one probe, then the deadline ends it


def test_watch_cdp_returns_only_once_chrome_is_confirmed_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-handoff stand-in for `proc.wait()`: it blocks while CDP answers
    and returns when it stops — one exit, and that exit is "browser gone"."""
    verdicts = iter([False, False, True])
    monkeypatch.setattr(bd, "_cdp_confirmed_gone", lambda _p: next(verdicts))  # pyright: ignore[reportUnknownArgumentType]
    slept: list[float] = []
    monkeypatch.setattr(bd.time, "sleep", slept.append)
    bd._watch_cdp(9222)
    assert slept == [bd._CDP_WATCH_INTERVAL_S] * 3


def test_launch_execs_in_place_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSIX keeps `os.execvp`: the pane's process BECOMES Chrome, so there is
    one pid and killing the pane kills the browser."""
    monkeypatch.setattr(bd, "IS_WINDOWS", False)
    monkeypatch.setattr(
        bd.subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("POSIX must exec, not spawn"),  # pyright: ignore[reportUnknownArgumentType]
    )
    captured: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(bd.os, "execvp", lambda f, a: captured.append((f, a)))  # pyright: ignore[reportUnknownArgumentType]
    bd._launch("/chrome", ["/chrome", "--remote-debugging-port=9222"], 9222)
    assert captured == [("/chrome", ["/chrome", "--remote-debugging-port=9222"])]


def _windows_branch(monkeypatch: pytest.MonkeyPatch, *, cdp_gone: bool) -> None:
    """Take the Windows half of `_launch` with a fixed answer to "is Chrome gone?".

    Pinning `_cdp_confirmed_gone` is what keeps these tests off the real network,
    and it is the fact the supervisor now branches on — the child's exit only asks
    the question.
    """
    monkeypatch.setattr(bd, "IS_WINDOWS", True)
    monkeypatch.setattr(
        bd.os,
        "execvp",
        lambda *_a: pytest.fail("Windows must not exec (pid would change)"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(bd, "_cdp_confirmed_gone", lambda _p: cdp_gone)  # pyright: ignore[reportUnknownArgumentType]


def test_launch_supervises_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows has no exec — `os.execvp` there spawns a new process and exits the
    caller, so the pid winproc recorded dies while Chrome runs on (session shows
    dead, CDP shows alive, and kill_session can no longer reach the browser).
    The launcher must instead stay Chrome's parent and wait, so the tracked tree
    is cmd -> python -> chrome.exe."""
    _windows_branch(monkeypatch, cdp_gone=True)
    args = ["C:\\chrome.exe", "--remote-debugging-port=9222"]
    proc = MagicMock()
    proc.wait.return_value = 0
    spawned: list[tuple[list[str], dict[str, object]]] = []

    def _popen(argv: list[str], **kwargs: object) -> MagicMock:
        spawned.append((argv, kwargs))
        return proc

    monkeypatch.setattr(bd.subprocess, "Popen", _popen)
    with pytest.raises(SystemExit) as exc:
        bd._launch("C:\\chrome.exe", args, 9222)
    assert [argv for argv, _kw in spawned] == [args]
    # fds 1/2 explicitly, not inherited: main() dup2-ed them onto browser.log,
    # which on Windows moves the CRT fds without moving GetStdHandle.
    assert spawned[0][1]["stdout"] == 1
    assert spawned[0][1]["stderr"] == 2
    proc.wait.assert_called_once()  # the launcher waits — it does not return early
    # Chrome exited 0 AND CDP is unreachable, so the browser really is gone. This
    # service is meant to stay up, so a gone browser is a non-zero exit whatever
    # status the launcher carried — the supervisor's cue to respawn.
    assert exc.value.code != 0


def test_launch_on_windows_propagates_chrome_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """The supervisor dies exactly when Chrome does, and with its status — so a
    crashed browser leaves a dead session for the healthcheck to revive."""
    _windows_branch(monkeypatch, cdp_gone=True)
    proc = MagicMock()
    proc.wait.return_value = 9
    monkeypatch.setattr(bd.subprocess, "Popen", lambda *_a, **_k: proc)  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(SystemExit) as exc:
        bd._launch("C:\\chrome.exe", ["C:\\chrome.exe"], 9222)
    assert exc.value.code == 9


def test_launch_on_windows_stays_up_when_chrome_hands_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE regression this fix exists for. Chrome's launched process is not always
    the process that stays alive: on a SingletonLock handoff the launcher exits 0
    while the browser runs on. Exiting there killed the session under a healthy
    browser — a spurious respawn the port guard then refused forever.

    A fake launcher that exits 0 while CDP stays reachable must NOT end the
    daemon: it hands over to `_watch_cdp` and exits only once CDP goes away."""
    _windows_branch(monkeypatch, cdp_gone=False)
    proc = MagicMock()
    proc.wait.return_value = 0
    monkeypatch.setattr(bd.subprocess, "Popen", lambda *_a, **_k: proc)  # pyright: ignore[reportUnknownArgumentType]
    watched: list[int] = []

    def _watch(port: int) -> None:
        watched.append(port)  # returns == "now CDP is gone", so the daemon may exit

    monkeypatch.setattr(bd, "_watch_cdp", _watch)
    with pytest.raises(SystemExit) as exc:
        bd._launch("C:\\chrome.exe", ["C:\\chrome.exe"], 9222)
    assert watched == [9222]  # supervised the live browser instead of exiting
    assert exc.value.code == 1  # ...and exited non-zero only after CDP went away


def test_launch_on_windows_never_returns_while_cdp_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same claim without stubbing `_watch_cdp`: while CDP keeps answering the
    supervisor must not reach any exit at all. A live `_watch_cdp` over a
    permanently reachable endpoint would loop forever, so the sleep raises to
    prove where control was parked."""
    monkeypatch.setattr(bd, "IS_WINDOWS", True)
    monkeypatch.setattr(
        bd,
        "_cdp_reachable",
        lambda _p: True,  # pyright: ignore[reportUnknownArgumentType]
    )  # a healthy browser, always
    proc = MagicMock()
    proc.wait.return_value = 0
    monkeypatch.setattr(bd.subprocess, "Popen", lambda *_a, **_k: proc)  # pyright: ignore[reportUnknownArgumentType]

    class _StopWatchingError(Exception):
        pass

    def _sleep(_s: float) -> None:
        raise _StopWatchingError

    monkeypatch.setattr(bd.time, "sleep", _sleep)
    with pytest.raises(_StopWatchingError):  # parked in _watch_cdp, not exiting
        bd._launch("C:\\chrome.exe", ["C:\\chrome.exe"], 9222)


def test_launch_on_windows_kills_chrome_on_ctrl_break(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl-Break from `ava stop` lands on the launcher first; it must take
    Chrome down rather than orphan it back onto the CDP port. A deliberate stop
    skips the CDP question entirely — no probe, no handoff watch."""
    monkeypatch.setattr(bd, "IS_WINDOWS", True)
    monkeypatch.setattr(
        bd,
        "_cdp_confirmed_gone",
        lambda _p: pytest.fail("a Ctrl-Break stop must not probe CDP"),  # pyright: ignore[reportUnknownArgumentType]
    )
    proc = MagicMock()
    proc.wait.side_effect = [KeyboardInterrupt(), 0]
    monkeypatch.setattr(bd.subprocess, "Popen", lambda *_a, **_k: proc)  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(SystemExit):
        bd._launch("C:\\chrome.exe", ["C:\\chrome.exe"], 9222)
    proc.terminate.assert_called_once()


def test_launch_on_windows_leaves_on_ctrl_break_during_handoff_watch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ava stop` must still be able to stop a daemon parked in the handoff watch
    — the watch is not allowed to swallow the interrupt into another loop."""
    _windows_branch(monkeypatch, cdp_gone=False)
    proc = MagicMock()
    proc.wait.return_value = 0
    monkeypatch.setattr(bd.subprocess, "Popen", lambda *_a, **_k: proc)  # pyright: ignore[reportUnknownArgumentType]

    def _interrupted(_port: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(bd, "_watch_cdp", _interrupted)
    with pytest.raises(SystemExit):
        bd._launch("C:\\chrome.exe", ["C:\\chrome.exe"], 9222)


def test_launch_on_windows_exits_127_when_spawn_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same fail-loud exit code as the POSIX exec failure path. Nothing was ever
    spawned, so there is no handoff to consider — do not probe CDP."""
    monkeypatch.setattr(bd, "IS_WINDOWS", True)
    monkeypatch.setattr(
        bd,
        "_cdp_confirmed_gone",
        lambda _p: pytest.fail("a failed spawn must not probe CDP"),  # pyright: ignore[reportUnknownArgumentType]
    )

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("no such binary")

    monkeypatch.setattr(bd.subprocess, "Popen", _boom)
    with pytest.raises(SystemExit) as exc:
        bd._launch("C:\\chrome.exe", ["C:\\chrome.exe"], 9222)
    assert exc.value.code == 127


def test_main_refuses_when_cdp_port_already_served(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A Chrome already on the CDP port (e.g. a hand-started one holding the
    profile lock) makes main() refuse with a non-zero exit and never exec a
    colliding second Chrome — so the healthcheck/watchdog don't churn a
    profile-lock crash."""
    monkeypatch.setattr(bd, "assert_browser_capable", lambda: None)
    monkeypatch.setattr(bd, "_cdp_reachable", lambda _p: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(bd, "_profile_dir", lambda: Path("/x/chrome-profile"))
    monkeypatch.setattr(
        bd.os,
        "execvp",
        lambda *_a: pytest.fail("execvp must not run when the port is taken"),  # pyright: ignore[reportUnknownArgumentType]
    )
    with pytest.raises(SystemExit) as exc:
        bd.main()
    assert exc.value.code == 1
    assert "already served" in capsys.readouterr().err
