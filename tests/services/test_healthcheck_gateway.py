"""`services.healthchecks.gateway` unit tests — the `_probe` branches.

A 2xx is necessary but NOT sufficient: the body must also report this unit's
`$AVA_HOME`, otherwise the responder is some other cluster's gateway (or an
unrelated process) sitting on the port and the real gateway is dead. Identity is
home-only here, deliberately without the pid comparison the daemon healthchecks
apply — uvicorn reload serves from a worker forked out of the process that wrote
`gateway_pidfile`, so a healthy gateway routinely answers with an unrecorded pid.

The check itself is `shared.daemon_health.probe_home`, shared with the operator
surfaces (`ava status` / `ava cluster health-probe` reach it through
`ServiceSpec.identity_probe`) so the watchdog and the human cannot be told
different things about the same port. These tests exercise it through the
healthcheck, which is the caller whose behaviour they pin.

No gateway is started — `urlopen` is monkeypatched and `_restart` is exercised
against a stubbed respawn, asserting only the call shape.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

import shared.daemon_health as dh
from services.healthchecks import gateway as hc
from shared import service_respawn
from shared.daemon_health import EXIT_PORT_TAKEN, DaemonProbe, ProbeVerdict
from shared.paths import ava_home


class _FakeResponse:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    def read(self, _n: int = -1) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _respond(monkeypatch: pytest.MonkeyPatch, status: int, body: bytes) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", lambda _url, **_kw: _FakeResponse(status, body))  # pyright: ignore[reportUnknownArgumentType]


def _own_health_body() -> bytes:
    return json.dumps({"status": "ok", "home": str(ava_home()), "machine": "m"}).encode()


@pytest.fixture(autouse=True)
def _reset_gateway_probe_failures() -> Generator[None, None, None]:
    """Keep the watchdog's process-local two-probe threshold test-isolated."""
    service_respawn._reset_consecutive_probe_failures("gateway")
    yield
    service_respawn._reset_consecutive_probe_failures("gateway")


def test_probe_alive_when_home_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """200 + this unit's home → alive."""
    _respond(monkeypatch, 200, _own_health_body())
    assert hc._probe().alive is True


def test_probe_rejects_gateway_from_another_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """200 from a gateway whose `$AVA_HOME` is not ours → dead, not "healthy".

    The foreign-unit impostor case: another unit's gateway (or a leaked test one)
    holding this port would otherwise read as green forever while this unit's
    gateway stays down. The verdict is TERMINAL — this unit cannot kill the gateway
    session on another unit's socket (the socket lives under `$AVA_HOME`), so
    respawning into the bound port every 60s is a loop, not a repair."""
    _respond(
        monkeypatch,
        200,
        json.dumps({"status": "ok", "home": str(Path.home() / ".ava-some-other-cluster")}).encode(),
    )
    probe = hc._probe()
    assert probe.verdict is ProbeVerdict.PORT_TAKEN
    assert probe.terminal is True
    assert "home=" in probe.detail


def test_another_clusters_gateway_is_not_respawned_against(monkeypatch: pytest.MonkeyPatch) -> None:
    """The terminal verdict reaches `main()`: report at ERROR, exit with the
    distinct code, no respawn."""
    _respond(
        monkeypatch,
        200,
        json.dumps({"status": "ok", "home": str(Path.home() / ".ava-some-other-cluster")}).encode(),
    )
    monkeypatch.setattr(hc, "init_gateway_process", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "_restart", lambda: pytest.fail("must not respawn against an impostor"))
    with pytest.raises(SystemExit) as excinfo:
        hc.main()
    assert excinfo.value.code == EXIT_PORT_TAKEN


def test_an_unreachable_gateway_respawns_after_two_failed_probes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One failed probe is a blip; the second still triggers gateway recovery."""

    def fake_urlopen(_url: str, **_kw: Any) -> _FakeResponse:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(hc, "init_gateway_process", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    respawns: list[int] = []
    monkeypatch.setattr(hc, "_restart", lambda: (respawns.append(1), DaemonProbe.up("home /x"))[1])
    hc.main()
    assert respawns == []
    assert "probe failed (1/2) — not respawning yet" in caplog.text

    hc.main()
    assert respawns == [1]


def test_probe_rejects_body_without_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 whose body carries no identity at all is not evidence of our gateway."""
    _respond(monkeypatch, 200, b'{"status": "ok"}')
    assert hc._probe().alive is False


def test_probe_rejects_non_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some unrelated HTTP server on the port → dead."""
    _respond(monkeypatch, 200, b"<html>nginx</html>")
    probe = hc._probe()
    assert probe.verdict is ProbeVerdict.PORT_TAKEN
    assert "not JSON" in probe.detail


def test_probe_dead_on_http_500(monkeypatch: pytest.MonkeyPatch) -> None:
    _respond(monkeypatch, 500, b"")
    assert hc._probe().alive is False


def test_probe_dead_on_url_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connection refused / DNS fail → URLError → dead."""

    def fake_urlopen(_url: str, **_kw: Any) -> _FakeResponse:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert hc._probe().alive is False


def test_restart_respawns_gateway_session_and_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    """_restart goes through respawn_and_verify: session `gateway`, then the
    probe decides the verdict — the spawn accepting the command is not the answer."""
    calls: list[tuple[str, str, Path]] = []

    def fake_respawn_and_verify(session, cmd, repo, *, verify, **_kw) -> DaemonProbe:
        calls.append((session, cmd, repo))  # pyright: ignore[reportUnknownArgumentType]
        return verify()

    monkeypatch.setattr(hc, "respawn_and_verify", fake_respawn_and_verify)  # pyright: ignore[reportUnknownArgumentType]
    _respond(monkeypatch, 200, _own_health_body())

    result = hc._restart()
    assert result.alive is True
    assert [(s, c) for s, c, _r in calls] == [("gateway", ".venv/bin/python -m gateway")]


# ─── _probe always returns a verdict ─────────────────────────────────────


def test_probe_survives_an_http_exception(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """`http.client.HTTPException` is not an `OSError`, so the inner probe's
    narrow catch misses it. Losing the verdict matters most here: the gateway is
    what the cluster health probe polls, with `--auto-rollback --threshold 3`
    armed, and a probe that raises means no restart is ever attempted while every
    60s round writes a fresh traceback."""
    import http.client
    import logging

    def _boom(*_a: object, **_k: object) -> None:
        raise http.client.IncompleteRead(b"half a body")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    with caplog.at_level(logging.ERROR, logger="shared.daemon_health"):  # pyright: ignore[reportUnknownMemberType]
        probe = hc._probe()
    assert probe.alive is False
    assert "IncompleteRead" in probe.detail
    assert any("raised unexpectedly" in r.getMessage() for r in caplog.records)  # pyright: ignore[reportUnknownMemberType]


def test_probe_fails_closed_on_any_unexpected_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail CLOSED — reporting alive on an unreadable probe would make the
    watchdog skip a genuinely dead gateway forever."""
    monkeypatch.setattr(
        dh,
        "_probe_home",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("unpredicted")),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert hc._probe().alive is False


def test_probe_passes_through_a_normal_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wrapper adds a floor, not a behaviour change."""
    monkeypatch.setattr(dh, "_probe_home", lambda *_a, **_kw: DaemonProbe.up("home /x"))  # pyright: ignore[reportUnknownArgumentType]
    probe = hc._probe()
    assert probe.alive is True
    assert probe.detail == "home /x"
