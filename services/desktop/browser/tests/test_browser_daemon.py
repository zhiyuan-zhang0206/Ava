"""services.desktop.browser.daemon — capability assertion, arg construction, and the
exec entrypoint. The three-prong capability check (display + Chrome + npx) now
lives in base.host.system.probes.browser_incapability (tested per-prong in
base/host/system/tests/test_probes.py); assert_browser_capable is a thin raising
wrapper over it, so these tests patch browser_incapability as bound in the daemon
module. The real exec is not unit-tested (it replaces the process); the testable
surface is the pure helpers, launch ordering, and the POSIX exec path.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import services.desktop.browser.daemon as bd
from base.config import settings


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


def test_launch_execs_in_place_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSIX keeps `os.execvp`: the pane's process BECOMES Chrome, so there is
    one pid and killing the pane kills the browser."""
    captured: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(bd.os, "execvp", lambda f, a: captured.append((f, a)))  # pyright: ignore[reportUnknownArgumentType]
    bd._launch("/chrome", ["/chrome", "--remote-debugging-port=9222"])
    assert captured == [("/chrome", ["/chrome", "--remote-debugging-port=9222"])]


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
