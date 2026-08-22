"""`ops.controllers.respawn` — the gateway-health deferral log line (#1126).

`RespawnController._dispatch_respawns` skips respawn and logs at DEBUG when
the gateway is unreachable — see the module docstring's `_gateway_healthy`
rationale. Before #1126's fix to `shared/log.py`, that line was structurally
unobservable: it uses a stdlib `logging.getLogger(...)` logger, and the
stdlib intercept's `basicConfig(level=logging.INFO)` dropped every DEBUG
record before it reached loguru. This is the regression lock for that
specific callsite, exercising the real deferral path rather than a synthetic
logger call.
"""

from __future__ import annotations

import socket
import time
from typing import Any, Never, cast

import httpx
import pytest
from psycopg_pool import ConnectionPool

import ops.controllers.respawn as respawn_mod
from shared.http_dial import PinnedIPv4Transport
from shared.log import _install_stdlib_intercept


def test_gateway_health_pins_tailnet_ipv4_and_follows_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production probe selects the AF_INET-pinned transport for a raw
    Tailnet IPv4 URL and preserves urllib's redirect-following behavior."""
    transports: list[httpx.BaseTransport] = []
    requests: list[tuple[str, float, bool]] = []

    class _Client:
        def __init__(self, *, transport: httpx.BaseTransport) -> None:
            transports.append(transport)

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def get(self, url: str, *, timeout: float, follow_redirects: bool) -> httpx.Response:
            requests.append((url, timeout, follow_redirects))
            return httpx.Response(200)

    url = "http://100.103.96.72:8000/api/health"

    def _dns_forbidden(*_args: object, **_kwargs: object) -> Never:
        raise AssertionError("raw IPv4 health probe must not call getaddrinfo")

    monkeypatch.setattr(respawn_mod, "_GATEWAY_HEALTH_URL", url)
    monkeypatch.setattr(socket, "getaddrinfo", _dns_forbidden)
    monkeypatch.setattr(httpx, "Client", _Client)

    assert respawn_mod._gateway_healthy() is True
    assert requests == [(url, respawn_mod._GATEWAY_HEALTH_TIMEOUT_S, True)]
    assert len(transports) == 1
    assert isinstance(transports[0], PinnedIPv4Transport)


def test_gateway_health_rejects_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    def _unavailable(*_args: object, **_kwargs: object) -> httpx.Response:
        return httpx.Response(503)

    monkeypatch.setattr(respawn_mod, "dial_get", _unavailable)

    assert respawn_mod._gateway_healthy() is False


def test_gateway_health_treats_httpx_failure_as_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail(*_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("gateway unavailable")

    monkeypatch.setattr(respawn_mod, "dial_get", _fail)

    assert respawn_mod._gateway_healthy() is False


def test_gateway_unhealthy_defers_respawn_and_logs_debug(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    """Restarting agents pending + gateway unreachable -> no respawn attempted,
    and the deferral reason lands in loguru (not silently dropped)."""
    _install_stdlib_intercept()
    monkeypatch.setattr(
        respawn_mod,
        "_select_local_restarting_ids",
        lambda _pool, _machine: [11, 22],  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(respawn_mod, "_gateway_healthy", lambda: False)

    controller = respawn_mod.RespawnController(cast(ConnectionPool, object()))
    acted = controller._dispatch_respawns("test-machine")

    assert acted is False
    assert any(
        r["level"].name == "DEBUG" and "deferring respawn of 2 agent(s)" in r["message"]
        for r in loguru_records
    )


def test_gateway_healthy_dispatches_without_deferral_log(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    """Sanity counterpart: no pending restarting agents means the health check
    is never consulted and the deferral line never fires."""
    _install_stdlib_intercept()
    monkeypatch.setattr(respawn_mod, "_select_local_restarting_ids", lambda _pool, _machine: [])  # pyright: ignore[reportUnknownArgumentType]

    def _fail_if_called() -> bool:
        pytest.fail("gateway health must not be probed when there is nothing to respawn")

    monkeypatch.setattr(respawn_mod, "_gateway_healthy", _fail_if_called)

    controller = respawn_mod.RespawnController(cast(ConnectionPool, object()))
    acted = controller._dispatch_respawns("test-machine")

    assert acted is False
    assert not any("deferring respawn" in r["message"] for r in loguru_records)


# ───────────────────────────────────────────────────────────────────────────
# Post-outage grace window for the lease-zombie pass (2026-08-08 audit P1-2)
# ───────────────────────────────────────────────────────────────────────────


def _reconcile_with_forced_reap(
    controller: respawn_mod.RespawnController, role: str = "agent-runner"
) -> None:
    """Run one reconcile with the 30s reap cadence forced open (first pass)."""
    controller._last_reap = 0.0
    controller.reconcile(role)  # type: ignore[arg-type]


def test_lease_zombie_pass_skipped_within_grace_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While the daemon's post-outage grace window is armed, the lease-zombie
    pass must NOT run — a paused-but-alive agent whose lease expired during the
    outage would otherwise be force-killed on the reaper's first post-outage
    pass (the exact kill+revive cycle the grace exists to prevent). The
    pid-based reapers and the revive pass still run (they never kill a live
    process)."""
    calls: list[object] = []
    monkeypatch.setattr(respawn_mod, "_select_local_restarting_ids", lambda *_a: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(respawn_mod, "_reap_local_dead_starting", lambda *_a: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(respawn_mod, "_reap_local_stale_allocated", lambda *_a: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(respawn_mod, "_revive_local_dead_running_idling", lambda *_a: [])  # pyright: ignore[reportUnknownArgumentType]

    def _collect(*_args: object, **_kwargs: object) -> list[int]:
        calls.append(_kwargs)
        return []

    monkeypatch.setattr(respawn_mod, "_collect_local_lease_zombies", _collect)

    controller = respawn_mod.RespawnController(cast(ConnectionPool, object()))
    controller.set_lease_zombie_grace_until(time.time() + 300.0)
    _reconcile_with_forced_reap(controller)

    assert calls == [], "lease-zombie pass must be skipped inside the grace window"


def test_lease_zombie_pass_runs_after_grace_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired grace window (or one never armed) restores the pass — a row
    whose lease lapsed under normal operation is still collected."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(respawn_mod, "_select_local_restarting_ids", lambda *_a: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(respawn_mod, "_reap_local_dead_starting", lambda *_a: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(respawn_mod, "_reap_local_stale_allocated", lambda *_a: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(respawn_mod, "_revive_local_dead_running_idling", lambda *_a: [])  # pyright: ignore[reportUnknownArgumentType]

    def _collect(*_args: object, **_kwargs: object) -> list[int]:
        calls.append(_kwargs)
        return []

    monkeypatch.setattr(respawn_mod, "_collect_local_lease_zombies", _collect)

    controller = respawn_mod.RespawnController(cast(ConnectionPool, object()))
    controller.set_lease_zombie_grace_until(time.time() - 1.0)  # expired
    _reconcile_with_forced_reap(controller)

    assert len(calls) == 1, "lease-zombie pass must run once the grace has expired"
    assert calls[0] == {}, "no grace kwarg is passed down once the window is over"


def test_lease_zombie_grace_defaults_to_no_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    """A controller never armed (healthcheck path) runs the pass with no skip —
    the grace is opt-in from the daemon."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(respawn_mod, "_select_local_restarting_ids", lambda *_a: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(respawn_mod, "_reap_local_dead_starting", lambda *_a: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(respawn_mod, "_reap_local_stale_allocated", lambda *_a: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(respawn_mod, "_revive_local_dead_running_idling", lambda *_a: [])  # pyright: ignore[reportUnknownArgumentType]

    def _collect(*_args: object, **_kwargs: object) -> list[int]:
        calls.append(_kwargs)
        return []

    monkeypatch.setattr(respawn_mod, "_collect_local_lease_zombies", _collect)

    controller = respawn_mod.RespawnController(cast(ConnectionPool, object()))
    _reconcile_with_forced_reap(controller)

    assert len(calls) == 1
    assert calls[0] == {}
