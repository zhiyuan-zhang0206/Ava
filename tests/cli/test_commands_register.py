"""Gateway probe and machine registration commands; split from tests/cli/test_commands.py (task #4554)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from cli import commands as _cli
from cli.commands._setup import SetupValues
from tests.cli._commands_helpers import _fake_session_backends as _fake_session_backends
from tests.cli._commands_helpers import _hermetic_gateway_base as _hermetic_gateway_base
from tests.cli._commands_helpers import _real_register_machine_or_die

# ─── probe gateway via HTTP, not relying on pidfile ───────────────────────────────────


def _spec_by_service(service: str) -> _cli.ServiceSpec:
    """Look up a ServiceSpec by bare service name."""
    for spec in _cli.build_services():
        if spec.session == service:
            return spec
    raise AssertionError(f"no ServiceSpec service={service!r}")


def test_gateway_spec_uses_http_probe_not_pidfile() -> None:
    """Gateway uvicorn(reload=True) makes pidfile-based liveness unreliable — must probe HTTP.

    `_probe_service` prefers the identity probe, then curl_url, and only falls back
    to the pidfile when neither is set. The gateway spec must set curl_url,
    otherwise `ava status` / the watchdog healthcheck would probe the pidfile —
    which the reload supervisor's fork makes wrong, reporting a healthy gateway dead.
    """
    spec = _spec_by_service("gateway")
    assert spec.curl_url is not None, "gateway must use curl probe (uvicorn reload)"
    assert spec.curl_url.startswith("http://"), f"curl_url shape wrong: {spec.curl_url!r}"


def test_probe_gateway_takes_the_identity_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_probe_service(gateway_spec)` asks the identity probe, not a bare curl.

    The gateway declares one (`probe_home` — 2xx AND this unit's `$AVA_HOME`), so
    the operator surface and the watchdog ask the same question of the same port.
    A plain 2xx would still be satisfied by another cluster's gateway."""
    spec = _spec_by_service("gateway")
    monkeypatch.setattr(
        _cli,
        "_curl_ok",
        lambda _u: pytest.fail("gateway must not fall back to a bare 2xx"),  # pyright: ignore[reportUnknownArgumentType]
    )
    from shared.daemon_health import DaemonProbe

    spec = replace(spec, identity_probe=lambda: DaemonProbe.up("root-owned gateway"))
    probe = _cli._probe_service(spec)
    assert probe.alive is True
    assert probe.label == "identity"


def test_probe_gateway_reports_which_fact_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ✗ carries the reason. "down" and "answering, but it is another cluster's
    home" call for completely different actions, and this row is where an operator
    learns which one they have."""
    spec = _spec_by_service("gateway")
    from shared.daemon_health import DaemonProbe

    spec = replace(
        spec,
        identity_probe=lambda: DaemonProbe.port_taken("identity mismatch: home='/home/ava/.ava'"),
    )
    probe = _cli._probe_service(spec)
    assert probe.alive is False
    assert "/home/ava/.ava" in probe.detail


def test_probe_survives_an_identity_probe_that_raises() -> None:
    """A plugin's non-total `identity_probe` reports ✗ — it does not take `ava status`
    down with it.

    The three built-in probes convert every failure mode into a `DaemonProbe`;
    a plugin-registered one is under no such obligation. `ava status` is what an
    operator runs when the unit is ALREADY misbehaving, so one plugin raising must
    cost that plugin's row and nothing else."""
    import dataclasses

    def _boom() -> object:
        raise RuntimeError("no socket for you")

    spec = dataclasses.replace(_spec_by_service("gateway"), identity_probe=_boom)
    probe = _cli._probe_service(spec)
    assert probe.alive is None
    assert probe.label == "unavailable"
    # The type AND the message: "the probe is broken" and "the daemon is down" are
    # different problems, and a fixed string would have made them look alike.
    assert "RuntimeError" in probe.detail
    assert "no socket for you" in probe.detail


def test_service_without_identity_probe_cannot_claim_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = replace(_spec_by_service("gateway"), identity_probe=None)
    monkeypatch.setattr(_cli, "_curl_ok", lambda _url: True)
    result = _cli._probe_service(spec)
    assert result.alive is None
    assert result.label == "unavailable"


def test_register_gateway_advertises_without_gateway_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """gateway registration no longer requires AVA_GATEWAY_URL.

    WP4 (conventions/reachability-and-credentials.md): the advertised URL is
    built on `reachable_host()` — the machine's private-network address — not
    on the bare gateway URL, so a gateway unit with a reachable identity
    registers a dialable address even before `AVA_GATEWAY_URL` is configured
    (a loopback gateway_url advertisement was what made the page proxy refuse
    the host's page servers, the 2026-08-30 serve 400). The port falls back to
    the gateway bind-port setting.

    The real `_register_machine_or_die` is used (autouse fixture replaces the
    module attribute with a noop; `_real_register_machine_or_die` captures the
    original at import time)."""
    from shared.config import settings

    calls: list[str | None] = []

    def fake_register_self(*, url: str | None = None) -> None:
        calls.append(url)

    monkeypatch.setattr("shared.machines.register_self", fake_register_self)
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    monkeypatch.setattr(settings.gateway, "gateway_port", 8000)
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "10.0.0.2")
    monkeypatch.setattr("shared.machine.ava_home", lambda: tmp_path)

    rc = _real_register_machine_or_die(
        cast(SetupValues, {"machine_name": "control"}), frozenset({"gateway"})
    )
    assert rc == 0
    assert calls == ["http://10.0.0.2:8000"]


def test_register_gateway_only_advertises_reachable_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A gateway-only unit advertises `reachable_host` + the gateway URL's port —
    NOT the bare gateway URL (WP4: the hostname is what the page proxy's SSRF
    allowlist consumes; a loopback advertisement breaks page serves)."""
    from shared.config import settings

    calls: list[str | None] = []

    def fake_register_self(*, url: str | None = None) -> None:
        calls.append(url)

    monkeypatch.setattr("shared.machines.register_self", fake_register_self)
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "10.0.0.2")
    monkeypatch.setattr("shared.machine.ava_home", lambda: tmp_path)
    (tmp_path / "gateway_url").write_text("https://ava.example:8000")

    rc = _real_register_machine_or_die(
        cast(SetupValues, {"machine_name": "control"}), frozenset({"gateway"})
    )
    assert rc == 0
    assert calls == ["http://10.0.0.2:8000"]


def test_register_agent_runner_advertises_ops_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent-runner registers its reachable ops server URL — the exact string the
    gateway later dials. Shape: http://<reachable-host>:<ops_port>."""
    calls: list[str | None] = []

    def fake_register_self(*, url: str | None = None) -> None:
        calls.append(url)

    monkeypatch.setattr("shared.machines.register_self", fake_register_self)
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "10.0.0.2")
    monkeypatch.setattr(
        "shared.daemon_health.health_port",
        lambda name: 8106 if name == "ops" else 0,  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = _real_register_machine_or_die(
        cast(SetupValues, {"machine_name": "wsl"}), frozenset({"agent-runner"})
    )
    assert rc == 0
    assert calls == ["http://10.0.0.2:8106"]


def test_register_agent_runner_loopback_host_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A remote agent-runner whose reachable address resolves to loopback must fail
    loud (exit 1): register_self raises LoopbackDialUrlRefused rather than writing a
    self-dialing localhost ops URL that a remote gateway would dial itself."""
    from shared.machines import LoopbackDialUrlRefused

    calls: list[str | None] = []

    def _reject(*, url: str | None = None) -> None:
        calls.append(url)
        raise LoopbackDialUrlRefused(f"loopback dial url refused: {url}")

    monkeypatch.setattr("shared.machines.register_self", _reject)
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "127.0.0.1")
    monkeypatch.setattr(
        "shared.daemon_health.health_port",
        lambda name: 8106 if name == "ops" else 0,  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = _real_register_machine_or_die(
        cast(SetupValues, {"machine_name": "wsl"}), frozenset({"agent-runner"})
    )
    assert rc == 1
    # register_self was reached with the loopback URL and rejected it; the caller
    # translated that into a non-zero exit rather than a persisted dead row.
    assert calls == ["http://127.0.0.1:8106"]
