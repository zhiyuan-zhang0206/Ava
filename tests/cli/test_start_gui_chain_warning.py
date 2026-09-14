"""`ava start` — the macOS GUI-chain warning (task #3346, R2a).

A start chain outside the GUI login session seeds every session it launches
with the wrong launchd management domain (the login Keychain is unreachable
there, which wedges the headed browser). The warning names the condition and
the GUI-domain remedy, and stays silent when no remedy exists (no GUI login for
this account, an unknown domain) or none is needed (Aqua, other platforms,
gateway-only roles).
"""

from __future__ import annotations

import pytest

from cli.commands import _start_gui_chain as warn_mod

_ME = "ava-test-user"


@pytest.fixture
def console(monkeypatch: pytest.MonkeyPatch) -> dict[str, str | None]:
    """macOS + agent-runner + a GUI login owned by this account."""
    state: dict[str, str | None] = {"domain": "Background", "console_user": _ME}
    monkeypatch.setattr(warn_mod, "IS_MACOS", True)
    monkeypatch.setattr(warn_mod, "gui_session_domain", lambda: state["domain"])
    monkeypatch.setattr(warn_mod, "gui_login_user", lambda: state["console_user"])
    monkeypatch.setattr(warn_mod, "_current_account_name", lambda: _ME)
    monkeypatch.setattr(
        warn_mod,
        "gui_domain_kickstart_command",
        lambda: "launchctl kickstart -k gui/501/com.ava.test.autostart",
    )
    return state


def test_warns_on_a_chain_outside_the_gui_session(
    capsys: pytest.CaptureFixture[str], console: dict[str, str | None]
) -> None:
    warn_mod._warn_when_chain_outside_gui_session(frozenset({"gateway", "agent-runner"}))
    err = capsys.readouterr().err
    assert "outside the macOS GUI login session" in err
    assert "Background" in err
    assert "launchctl kickstart -k gui/501/com.ava.test.autostart" in err


def test_silent_inside_the_gui_session(
    capsys: pytest.CaptureFixture[str], console: dict[str, str | None]
) -> None:
    console["domain"] = "Aqua"
    warn_mod._warn_when_chain_outside_gui_session(frozenset({"agent-runner"}))
    assert capsys.readouterr().err == ""


def test_silent_when_the_domain_is_unknown(
    capsys: pytest.CaptureFixture[str], console: dict[str, str | None]
) -> None:
    """An unavailable answer is not evidence — the warning must not guess."""
    console["domain"] = None
    warn_mod._warn_when_chain_outside_gui_session(frozenset({"agent-runner"}))
    assert capsys.readouterr().err == ""


def test_silent_without_a_gui_login_of_this_account(
    capsys: pytest.CaptureFixture[str], console: dict[str, str | None]
) -> None:
    """No re-home into another account's GUI session exists, and a headless
    host has no GUI session at all — silent either way."""
    console["console_user"] = "someone-else"
    warn_mod._warn_when_chain_outside_gui_session(frozenset({"agent-runner"}))
    assert capsys.readouterr().err == ""

    console["console_user"] = None
    warn_mod._warn_when_chain_outside_gui_session(frozenset({"agent-runner"}))
    assert capsys.readouterr().err == ""


def test_silent_on_a_host_without_the_agent_runner_role(
    capsys: pytest.CaptureFixture[str], console: dict[str, str | None]
) -> None:
    warn_mod._warn_when_chain_outside_gui_session(frozenset({"gateway"}))
    assert capsys.readouterr().err == ""


def test_silent_off_macos(
    capsys: pytest.CaptureFixture[str],
    console: dict[str, str | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(warn_mod, "IS_MACOS", False)
    warn_mod._warn_when_chain_outside_gui_session(frozenset({"agent-runner"}))
    assert capsys.readouterr().err == ""
