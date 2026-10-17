from __future__ import annotations

from pathlib import Path

import pytest

import services.desktop.browser.macos_readiness as readiness


def _macos_probes(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    *,
    keychain_code: int = 0,
    managername: str = "Aqua",
    managername_code: int = 0,
) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(readiness, "is_macos", lambda: True)
    monkeypatch.setattr(readiness.os, "getuid", lambda: 501)

    def _account() -> tuple[str, Path]:
        return "meiva", home

    monkeypatch.setattr(readiness, "current_account", _account)

    def _run(argv: list[str]) -> readiness.ProbeResult:
        calls.append(argv)
        if argv[0] == "/usr/bin/stat":
            return readiness.ProbeResult(0, "meiva\n", "")
        if argv[1:2] == ["managername"]:
            return readiness.ProbeResult(managername_code, managername, "")
        if argv[0] == "/bin/launchctl":
            return readiness.ProbeResult(0, "", "")
        return readiness.ProbeResult(keychain_code, "", "errSecInteractionNotAllowed")

    monkeypatch.setattr(readiness, "run_probe", _run)
    return calls


def test_non_macos_needs_no_macos_readiness_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness, "is_macos", lambda: False)
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

    def _console_root(_argv: list[str]) -> readiness.ProbeResult:
        return readiness.ProbeResult(0, "root\n", "")

    monkeypatch.setattr(readiness, "run_probe", _console_root)
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
    calls = _macos_probes(monkeypatch, tmp_path, managername="Background")
    result = readiness.probe_startup_readiness()
    assert result.ready is False
    assert result.reason is not None and "GUI login session" in result.reason
    assert not any(call[0] == "/usr/bin/security" for call in calls)


def test_macos_domain_probe_failure_falls_through_to_keychain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _macos_probes(monkeypatch, tmp_path, managername="", managername_code=1)
    assert readiness.probe_startup_readiness().ready is True
    assert any(call[0] == "/usr/bin/security" for call in calls)


def test_wait_loop_returns_when_ready_and_records_no_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MEIVA_HOME", str(tmp_path))
    states = iter(
        [
            readiness.StartupReadiness(False, "no GUI login session"),
            readiness.StartupReadiness(True),
        ]
    )
    sleeps: list[float] = []
    monkeypatch.setattr(readiness, "probe_startup_readiness", lambda: next(states))
    monkeypatch.setattr(readiness.time, "sleep", sleeps.append)

    readiness.wait_for_browser_startup_readiness()

    assert sleeps == [readiness.READINESS_RETRY_S]
    assert sorted(tmp_path.rglob("*")) == []


def test_degraded_wait_reason_probes_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        readiness,
        "probe_startup_readiness",
        lambda: readiness.StartupReadiness(ready=False, reason="login Keychain is not ready"),
    )
    assert readiness.degraded_wait_reason() == "login Keychain is not ready"


def test_degraded_wait_reason_is_none_when_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        readiness, "probe_startup_readiness", lambda: readiness.StartupReadiness(ready=True)
    )
    assert readiness.degraded_wait_reason() is None


def test_degraded_wait_reason_fails_safe_when_the_probe_raises_on_macos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> readiness.StartupReadiness:
        raise OSError("probe exploded")

    monkeypatch.setattr(readiness, "probe_startup_readiness", boom)
    monkeypatch.setattr(readiness, "is_macos", lambda: True)
    assert readiness.degraded_wait_reason() == "macOS startup readiness could not be probed"
    monkeypatch.setattr(readiness, "is_macos", lambda: False)
    assert readiness.degraded_wait_reason() is None
