"""`services.browser.probe` — the one service whose identity CDP cannot state.

Every other Ava service proves it is ours from its own `/healthz` body. Chrome's
DevTools Protocol carries no field we control, so identity is assembled from two
facts this host can observe: a Chrome running on THIS cluster's `--user-data-dir`
(`orphan.find_cluster_chrome`, the positive profile-token identification) that is
also holding the LISTEN socket on the CDP port being dialled.

Both halves are load-bearing and the tests pin each one separately:

- without the profile half, another unit's browser relayed onto this host's
  loopback reads as ours (the WSL2/Windows shape);
- without the listening half, our Chrome merely *existing* would vouch for a port
  it never won — which is exactly what happens when the relay binds first and
  Chrome comes up with a dead DevTools endpoint.

No Chrome is started: the CDP dial and the process facts are stubbed, so the
verdicts are asserted directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import services.browser.probe as probe_mod
from shared.daemon_health import ProbeVerdict

_PROFILE = Path("/home/u/.ava/chrome-profile")


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cdp: str | None,
    chromes: list[int],
    listening: dict[int, bool | None],
    holder: int | None = None,
    holder_facts: tuple[str, list[str] | None] | None = None,
) -> None:
    """CDP reachability, the cluster's Chrome pids, who holds the port, and the
    holder's own (name, argv) for the listener-first direction."""
    monkeypatch.setattr(probe_mod, "_cdp_unreachable", lambda _port: cdp)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(probe_mod, "find_cluster_chrome", lambda _profile: chromes)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(probe_mod, "_listens_on", lambda pid, _port: listening[pid])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(probe_mod, "_listener_pid", lambda _port: holder)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(probe_mod.macos_readiness, "degraded_wait_reason", lambda: None)
    if holder_facts is None:
        holder_facts = ("", None)
    monkeypatch.setattr(probe_mod, "_process_facts", lambda _pid: holder_facts)  # pyright: ignore[reportUnknownArgumentType]


def test_alive_when_our_chrome_holds_the_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """CDP answers and the listener is a Chrome on this cluster's profile."""
    _wire(monkeypatch, cdp=None, chromes=[42], listening={42: True})
    verdict = probe_mod.probe_browser(9222, _PROFILE)
    assert verdict.verdict is ProbeVerdict.ALIVE
    assert "42" in verdict.detail


def test_cdp_unreachable_is_down_not_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing serving = the respawnable case. `respawn_service` kills the stale
    session and relaunches, which is the fix — so this must NOT be terminal."""
    _wire(monkeypatch, cdp="CDP unreachable: ConnectionRefusedError", chromes=[], listening={})
    verdict = probe_mod.probe_browser(9222, _PROFILE)
    assert verdict.verdict is ProbeVerdict.DOWN
    assert verdict.terminal is False


def test_cdp_unreachable_names_a_deliberate_macos_readiness_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(monkeypatch, cdp="CDP unreachable: ConnectionRefusedError", chromes=[], listening={})
    monkeypatch.setattr(
        probe_mod.macos_readiness,
        "degraded_wait_reason",
        lambda: "login Keychain is not ready",
    )
    verdict = probe_mod.probe_browser(9222, _PROFILE)
    assert verdict.verdict is ProbeVerdict.DOWN
    assert "waiting" in verdict.detail
    assert "Keychain" in verdict.detail


def test_foreign_chrome_on_the_port_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A debuggable Chrome answers but none of ours is running: another unit's
    browser (or a hand-started one) holds the port. Terminal — the daemon refuses
    to launch while the port is served, so respawning is a 60s crash loop."""
    _wire(monkeypatch, cdp=None, chromes=[], listening={})
    verdict = probe_mod.probe_browser(9222, _PROFILE)
    assert verdict.verdict is ProbeVerdict.PORT_TAKEN
    assert verdict.terminal is True
    assert str(_PROFILE) in verdict.detail


def test_our_chrome_that_lost_the_bind_does_not_vouch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The half a profile check alone cannot do. Our Chrome is alive on our
    profile, but the CDP answer comes from something else — the relay won the
    bind and Chrome is running with a dead DevTools endpoint. Existing is not
    owning, so this is still PORT_TAKEN."""
    _wire(monkeypatch, cdp=None, chromes=[42], listening={42: False})
    verdict = probe_mod.probe_browser(9222, _PROFILE)
    assert verdict.verdict is ProbeVerdict.PORT_TAKEN


def test_our_chrome_with_a_dead_endpoint_names_its_pids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator-facing half of the win 2026-08-11 shape: the detail must say
    our Chrome is RUNNING (pids) but lost the port, not read like "no Chrome
    exists" — the two point at entirely different remedies."""
    _wire(monkeypatch, cdp=None, chromes=[42], listening={42: False})
    verdict = probe_mod.probe_browser(9222, _PROFILE)
    assert verdict.verdict is ProbeVerdict.PORT_TAKEN
    assert "running ([42])" in verdict.detail
    assert "DevTools endpoint is dead" in verdict.detail


def test_unreadable_sockets_are_reported_not_assumed(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "Could not look" is not "not the listener", and it is certainly not
    "healthy". Fail closed, and say which pid could not be read."""
    _wire(monkeypatch, cdp=None, chromes=[42], listening={42: None})
    verdict = probe_mod.probe_browser(9222, _PROFILE)
    assert verdict.verdict is ProbeVerdict.PORT_TAKEN
    assert "could not be read" in verdict.detail
    assert "42" in verdict.detail


def test_a_listening_chrome_wins_over_an_unreadable_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """One unreadable sibling must not mask a positive identification."""
    _wire(monkeypatch, cdp=None, chromes=[41, 42], listening={41: None, 42: True})
    assert probe_mod.probe_browser(9222, _PROFILE).verdict is ProbeVerdict.ALIVE


def test_probe_always_returns_a_verdict(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The probe contract: an unforeseen failure is reported down with a logged
    traceback, never raised at the watchdog and never called alive."""
    import logging

    def _boom(_port: int) -> str | None:
        raise RuntimeError("nobody predicted this")

    monkeypatch.setattr(probe_mod, "_cdp_unreachable", _boom)
    with caplog.at_level(logging.ERROR, logger="services.browser.probe"):
        verdict = probe_mod.probe_browser(9222, _PROFILE)
    assert verdict.verdict is ProbeVerdict.DOWN
    assert "RuntimeError" in verdict.detail
    assert any("raised unexpectedly" in r.getMessage() for r in caplog.records)


def test_the_listener_is_ours_even_when_the_walk_missed_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The false-terminal class the win 2026-08-11 diagnosis chased: when the
    profile walk misses the Chrome that actually serves CDP (unreadable argv,
    a failed socket read), the probe must not conclude "another unit's browser".
    The listener-first direction reads the holder's own argv, so a missed walk
    is not a false foreign verdict."""
    _wire(
        monkeypatch,
        cdp=None,
        chromes=[],
        listening={},
        holder=28408,
        holder_facts=("chrome.exe", [f"--user-data-dir={_PROFILE}", "--no-first-run"]),
    )
    verdict = probe_mod.probe_browser(9222, _PROFILE)
    assert verdict.verdict is ProbeVerdict.ALIVE
    assert "28408" in verdict.detail


def test_the_holder_who_is_not_ours_names_its_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """A positively foreign listener gets named in the detail — the pid is the
    actionable half for an operator hunting the occupant."""
    _wire(
        monkeypatch,
        cdp=None,
        chromes=[],
        listening={},
        holder=99,
        holder_facts=("firefox.exe", ["--profile", "/elsewhere"]),
    )
    verdict = probe_mod.probe_browser(9222, _PROFILE)
    assert verdict.verdict is ProbeVerdict.PORT_TAKEN
    assert "pid 99" in verdict.detail


def test_an_unidentifiable_holder_stays_terminal_without_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The holder's argv cannot be read and it is not in the walk — terminal,
    never guessed at as alive."""
    _wire(monkeypatch, cdp=None, chromes=[], listening={}, holder=99)
    verdict = probe_mod.probe_browser(9222, _PROFILE)
    assert verdict.verdict is ProbeVerdict.PORT_TAKEN


def test_an_unreadable_global_table_falls_back_to_the_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The listener-first direction is a gain, not a dependency: when the global
    table cannot be read, the profile-walk + per-pid socket check still decides."""
    _wire(monkeypatch, cdp=None, chromes=[42], listening={42: True}, holder=None)
    verdict = probe_mod.probe_browser(9222, _PROFILE)
    assert verdict.verdict is ProbeVerdict.ALIVE


def test_cdp_url_is_the_one_definition() -> None:
    """The daemon's port guard, the healthcheck and this probe all dial the same
    endpoint; a second spelling is how they would drift apart."""
    import services.browser.daemon as daemon_mod

    assert probe_mod.cdp_url(9222) == "http://127.0.0.1:9222/json/version"
    assert daemon_mod.cdp_url is probe_mod.cdp_url
