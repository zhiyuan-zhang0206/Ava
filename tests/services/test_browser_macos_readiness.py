"""Mac-specific startup prerequisites for the shared headed browser.

The browser must wait in its supervised session until the logged-in GUI user
and that user's login Keychain can serve Chrome's encryption material. These
tests hold the checks to bounded, side-effect-free probes and pin the waiting
marker used by the healthcheck to distinguish a deliberate wait from a crash.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import services.browser.macos_readiness as readiness


def _macos_probes(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    *,
    keychain_code: int = 0,
    managername: str = "Aqua",
    managername_code: int = 0,
) -> list[list[str]]:
    """Make the current uid the active console user and record bounded probes.

    The default `managername="Aqua"` is the GUI login session (the ready case);
    a "Background" answer is the agent/SSH chain that must be refused.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(readiness, "IS_MACOS", True)
    monkeypatch.setattr(readiness.os, "getuid", lambda: 501)

    def _account() -> tuple[str, Path]:
        return "ava", home

    monkeypatch.setattr(readiness, "_current_account", _account)

    def _run(argv: list[str]) -> readiness._ProbeResult:
        calls.append(argv)
        if argv[0] == "/usr/bin/stat":
            return readiness._ProbeResult(0, "ava\n", "")
        if argv[1:2] == ["managername"]:
            return readiness._ProbeResult(managername_code, managername, "")
        if argv[0] == "/bin/launchctl":
            return readiness._ProbeResult(0, "", "")
        return readiness._ProbeResult(keychain_code, "", "errSecInteractionNotAllowed")

    monkeypatch.setattr(readiness, "_run_probe", _run)
    return calls


def test_non_macos_needs_no_macos_readiness_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness, "IS_MACOS", False)
    assert readiness.probe_startup_readiness().ready is True


def test_macos_ready_only_after_console_gui_and_keychain_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _macos_probes(monkeypatch, tmp_path)
    assert readiness.probe_startup_readiness().ready is True
    assert calls == [
        ["/usr/bin/stat", "-f%Su", "/dev/console"],
        ["/bin/launchctl", "print", "gui/501"],
        ["/bin/launchctl", "managername"],
        [
            "/usr/bin/security",
            "show-keychain-info",
            str(tmp_path / "Library/Keychains/login.keychain-db"),
        ],
    ]


def test_macos_refuses_start_without_active_console_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _macos_probes(monkeypatch, tmp_path)

    def _console_root(_argv: list[str]) -> readiness._ProbeResult:
        return readiness._ProbeResult(0, "root\n", "")

    monkeypatch.setattr(readiness, "_run_probe", _console_root)
    result = readiness.probe_startup_readiness()
    assert result.ready is False
    assert result.reason is not None
    assert "GUI login session" in result.reason


def test_macos_refuses_start_when_login_keychain_is_not_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _macos_probes(monkeypatch, tmp_path, keychain_code=36)
    result = readiness.probe_startup_readiness()
    assert result.ready is False
    assert result.reason is not None
    assert "login Keychain" in result.reason


def test_macos_refuses_start_outside_the_gui_login_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A daemon respawned from an agent/SSH chain inherits launchd's
    "Background" domain, where securityd denies every login-Keychain query no
    matter how ready the Keychain is. That is not a wait — it is the one case
    a GUI-domain relaunch must fix, so the readiness names it and the Keychain
    probe (which can only blame the Keychain) never runs."""
    calls = _macos_probes(monkeypatch, tmp_path, managername="Background")
    result = readiness.probe_startup_readiness()
    assert result.ready is False
    assert result.context_missing is True
    assert result.reason is not None and "GUI login session" in result.reason
    assert not any(call[0] == "/usr/bin/security" for call in calls)


def test_macos_domain_probe_failure_falls_through_to_keychain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty or failed `launchctl managername` answer is not evidence about
    the context; the probe falls through to the Keychain check."""
    calls = _macos_probes(monkeypatch, tmp_path, managername="", managername_code=1)
    assert readiness.probe_startup_readiness().ready is True
    assert any(call[0] == "/usr/bin/security" for call in calls)


def test_wait_loop_marks_degraded_then_clears_before_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = iter(
        [
            readiness.StartupReadiness(False, "no GUI login session"),
            readiness.StartupReadiness(True),
        ]
    )
    marked: list[str] = []
    sleeps: list[float] = []

    def _mark(reason: str, *, context_missing: bool = False) -> None:
        marked.append(f"{reason} (context_missing={context_missing})")

    monkeypatch.setattr(readiness, "probe_startup_readiness", lambda: next(states))
    monkeypatch.setattr(readiness, "mark_waiting", _mark)
    monkeypatch.setattr(readiness, "clear_waiting", lambda: marked.append("cleared"))
    monkeypatch.setattr(readiness.time, "sleep", sleeps.append)
    readiness.wait_for_browser_startup_readiness()
    assert marked == ["no GUI login session (context_missing=False)", "cleared"]
    assert sleeps == [readiness._READINESS_RETRY_S]


def test_wait_loop_records_the_missing_gui_context_in_the_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wait loop is the one place a context-less daemon can tell its
    healthcheck what is wrong, so the flag must survive into the marker."""
    monkeypatch.setattr(
        readiness,
        "probe_startup_readiness",
        lambda: readiness.StartupReadiness(
            ready=False, reason="outside the GUI login session", context_missing=True
        ),
    )
    recorded: list[tuple[str, bool]] = []

    def _mark(reason: str, *, context_missing: bool = False) -> None:
        recorded.append((reason, context_missing))

    monkeypatch.setattr(readiness, "mark_waiting", _mark)

    def _stop(_seconds: float) -> None:
        raise KeyboardInterrupt  # end the infinite wait after the first tick

    monkeypatch.setattr(readiness.time, "sleep", _stop)
    with pytest.raises(KeyboardInterrupt):
        readiness.wait_for_browser_startup_readiness()
    assert recorded == [("outside the GUI login session", True)]


def test_wait_marker_requires_the_recorded_owner_to_still_be_alive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(readiness.shared.paths, "run_dir", lambda: tmp_path)
    monkeypatch.setattr(readiness.os, "getpid", lambda: 123)
    monkeypatch.setattr(readiness, "_current_process_started_at", lambda: 1.0)

    def _owner_alive(_pid: int, _started: float) -> bool:
        return True

    monkeypatch.setattr(readiness, "_owner_is_alive", _owner_alive)
    readiness.mark_waiting("login Keychain is not ready")
    marker = readiness.waiting_state()
    assert marker is not None
    assert marker.reason == "login Keychain is not ready"

    def _owner_gone(_pid: int, _started: float) -> bool:
        return False

    monkeypatch.setattr(readiness, "_owner_is_alive", _owner_gone)
    assert readiness.waiting_state() is None


def test_wait_marker_carries_the_context_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(readiness.shared.paths, "run_dir", lambda: tmp_path)

    def _owner_alive(_pid: int, _started: float) -> bool:
        return True

    monkeypatch.setattr(readiness, "_owner_is_alive", _owner_alive)

    readiness.mark_waiting("login Keychain is not ready")
    marker = readiness.waiting_state()
    assert marker is not None and marker.context_missing is False

    readiness.mark_waiting("this process is not in the GUI login session", context_missing=True)
    marker = readiness.waiting_state()
    assert marker is not None and marker.context_missing is True


@pytest.mark.parametrize("value", [None, "yes", 1])
def test_wait_marker_context_flag_fails_quiet_on_malformed_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: object
) -> None:
    """A marker without the field (older daemon) or with a non-boolean value
    must not trigger the healthcheck's GUI-domain relaunch on its own."""
    monkeypatch.setattr(readiness.shared.paths, "run_dir", lambda: tmp_path)

    def _owner_alive(_pid: int, _started: float) -> bool:
        return True

    monkeypatch.setattr(readiness, "_owner_is_alive", _owner_alive)
    payload: dict[str, object] = {
        "reason": "login Keychain is not ready",
        "pid": 1,
        "started_at": 1.0,
        "observed_at": time.time(),
    }
    if value is not None:
        payload["context_missing"] = value
    (tmp_path / "browser-readiness-wait.json").write_text(json.dumps(payload))
    marker = readiness.waiting_state()
    assert marker is not None and marker.context_missing is False


def test_degraded_wait_state_keeps_the_context_flag_from_the_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        readiness,
        "waiting_state",
        lambda: readiness.BrowserWaitState(
            "outside the GUI login session", 1, 0.0, context_missing=True
        ),
    )
    state = readiness.degraded_wait_state()
    assert state is not None
    assert state.context_missing is True
    assert state.reason == "outside the GUI login session"
    assert readiness.degraded_wait_reason() == "outside the GUI login session"


def test_degraded_wait_reason_falls_back_when_marker_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(readiness, "waiting_state", lambda: None)
    monkeypatch.setattr(
        readiness,
        "probe_startup_readiness",
        lambda: readiness.StartupReadiness(ready=False, reason="login Keychain is not ready"),
    )
    assert readiness.degraded_wait_reason() == "login Keychain is not ready"
    state = readiness.degraded_wait_state()
    assert state is not None and state.context_missing is False
