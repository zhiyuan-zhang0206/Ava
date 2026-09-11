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
    answered: bool = False,
) -> None:
    """CDP reachability, the cluster's Chrome pids, who holds the port, and the
    holder's own (name, argv) for the listener-first direction. `answered`
    distinguishes a dial that got an HTTP response from one that got nothing."""

    def _answer(_port: int) -> probe_mod._CdpAnswer:
        return probe_mod._CdpAnswer(cdp, answered)

    monkeypatch.setattr(probe_mod, "_cdp_unreachable", _answer)
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


def _fake_urlopen(body: bytes = b"", status: int = 200) -> object:
    """A urllib response stand-in for the CDP body tests."""

    class _Resp:
        def __init__(self, code: int) -> None:
            self.status = code

        def read(self) -> bytes:
            return body

        def __enter__(self) -> object:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

    return _Resp(status)


def _wire_cdp_body(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    monkeypatch.setattr(
        probe_mod.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _fake_urlopen(body),  # pyright: ignore[reportUnknownArgumentType]
    )


def test_cdp_200_with_empty_body_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 2026-09-09 swap-pressure shape: HTTP 200 with an EMPTY body. A
    status-only probe called that alive; the body check must call it dead —
    and record that something answered, so identity (not the status) decides
    the verdict."""
    _wire_cdp_body(monkeypatch, b"")
    answer = probe_mod._cdp_unreachable(9222)
    assert answer.reason is not None
    assert answer.answered is True


def test_cdp_200_with_non_json_body_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_cdp_body(monkeypatch, b"<html>not json</html>")
    answer = probe_mod._cdp_unreachable(9222)
    assert answer.reason is not None
    assert answer.answered is True


def test_cdp_200_with_json_missing_browser_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_cdp_body(monkeypatch, b'{"WebKit-Version": "537.36"}')
    answer = probe_mod._cdp_unreachable(9222)
    assert answer.reason is not None
    assert answer.answered is True


def test_an_http_error_status_counts_as_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    """urlopen raises HTTPError on 4xx/5xx; the status is still an HTTP answer,
    so it takes the identity path (an occupant holds the port) rather than the
    nothing-answered respawnable DOWN (#2692's exact churn class)."""
    import urllib.error

    def _raise_502(*_args: object, **_kwargs: object) -> object:
        raise urllib.error.HTTPError(
            "http://127.0.0.1:9222/json/version", 502, "Bad Gateway", None, None
        )

    monkeypatch.setattr(probe_mod.urllib.request, "urlopen", _raise_502)  # pyright: ignore[reportUnknownArgumentType]
    answer = probe_mod._cdp_unreachable(9222)
    assert answer.answered is True
    assert "502" in (answer.reason or "")


def test_a_refused_dial_is_unanswered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing answered: the respawnable DOWN, not the identity branch."""
    import urllib.error

    def _refuse(*_args: object, **_kwargs: object) -> object:
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    monkeypatch.setattr(probe_mod.urllib.request, "urlopen", _refuse)  # pyright: ignore[reportUnknownArgumentType]
    answer = probe_mod._cdp_unreachable(9222)
    assert answer.reason is not None
    assert answer.answered is False


def test_cdp_200_with_valid_version_body_is_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    body = (
        b'{"Browser": "Chrome/150.0.0.0", "Protocol-Version": "1.3", '
        b'"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/x"}'
    )
    _wire_cdp_body(monkeypatch, body)
    assert probe_mod._cdp_unreachable(9222).reason is None


def test_fake_alive_verdict_is_down_even_when_our_chrome_listens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-09 macmini: our orphaned Chrome still LISTENs on the port and
    answers 200 with an empty body. The probe must read DOWN (so the
    healthcheck's sweep + rebuild runs), never ALIVE."""
    _wire_cdp_body(monkeypatch, b"")
    monkeypatch.setattr(probe_mod, "find_cluster_chrome", lambda _profile: [42])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(probe_mod, "_listens_on", lambda *_args: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(probe_mod, "_listener_pid", lambda _port: 42)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(probe_mod.macos_readiness, "degraded_wait_reason", lambda: None)
    verdict = probe_mod.probe_browser(9222, _PROFILE)
    assert verdict.verdict is ProbeVerdict.DOWN
    assert "wedged" in verdict.detail


def test_garbage_200_with_our_own_wedged_endpoint_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-09-09 shape with the identity arms stubbed plainly: our Chrome
    holds the port, so the sweep + rebuild can heal it — DOWN, never terminal."""
    _wire(
        monkeypatch,
        cdp="CDP :9222 answered 200 but the body is not JSON — wedged DevTools endpoint",
        answered=True,
        chromes=[42],
        listening={42: True},
        holder=42,
    )
    verdict = probe_mod.probe_browser(9222, _PROFILE)
    assert verdict.verdict is ProbeVerdict.DOWN
    assert verdict.terminal is False
    assert "42" in verdict.detail
    assert "wedged" in verdict.detail


def test_garbage_200_with_a_foreign_listener_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unusable answer still proves the port has an occupant: when the
    listener is not this cluster's Chrome, a respawn cannot bind the port and
    must not be attempted once every 60s (task #2692)."""
    _wire(
        monkeypatch,
        cdp="CDP :9222 answered 200 but the body is not JSON — wedged DevTools endpoint",
        answered=True,
        chromes=[],
        listening={},
        holder=999,
        holder_facts=("chrome", ["--user-data-dir=/somebody-elses-profile"]),
    )
    verdict = probe_mod.probe_browser(9222, _PROFILE)
    assert verdict.verdict is ProbeVerdict.PORT_TAKEN
    assert verdict.terminal is True


def test_garbage_200_with_nobody_identifiable_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing identifiable holds the port but something answered: terminal, the
    same line the valid-payload path draws — fail closed, never churn."""
    _wire(
        monkeypatch,
        cdp="CDP :9222 returned HTTP 502",
        answered=True,
        chromes=[],
        listening={},
    )
    verdict = probe_mod.probe_browser(9222, _PROFILE)
    assert verdict.verdict is ProbeVerdict.PORT_TAKEN
    assert verdict.terminal is True


def test_garbage_200_listener_identified_ours_by_its_own_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The listener-first arm still applies on the unusable-payload branch: a
    holder the profile walk missed is ours when its own argv says so."""
    _wire(
        monkeypatch,
        cdp="CDP :9222 answered 200 but the body is not JSON — wedged DevTools endpoint",
        answered=True,
        chromes=[],  # the walk missed it
        listening={},
        holder=42,
        holder_facts=(
            "Google Chrome",
            [f"--user-data-dir={_PROFILE}", "--remote-debugging-port=9222"],
        ),
    )
    verdict = probe_mod.probe_browser(9222, _PROFILE)
    assert verdict.verdict is ProbeVerdict.DOWN
    assert "42" in verdict.detail


def test_unanswered_cdp_stays_the_plain_respawnable_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused dial proves nothing answered — no occupant to identify, so the
    verdict is the plain DOWN a respawn can act on."""
    _wire(
        monkeypatch,
        cdp="CDP unreachable on http://127.0.0.1:9222/json/version: URLError: refused",
        answered=False,
        chromes=[42],
        listening={42: False},
    )
    verdict = probe_mod.probe_browser(9222, _PROFILE)
    assert verdict.verdict is ProbeVerdict.DOWN
    assert verdict.terminal is False


def test_cdp_url_is_the_one_definition() -> None:
    """The daemon's port guard, the healthcheck and this probe all dial the same
    endpoint; a second spelling is how they would drift apart."""
    import services.browser.daemon as daemon_mod

    assert probe_mod.cdp_url(9222) == "http://127.0.0.1:9222/json/version"
    assert daemon_mod.cdp_url is probe_mod.cdp_url
