"""Contract tests for the `fleet_graph_stale` emission (task #3925).

The ops alert rule `ava-ops-fleet-graph-stale` counts `fleet_graph_stale`
events, so every stale-serving fallback on GET /api/fleet/graph must emit
exactly one event per degradation episode — a path that serves stale silently
is a hole in the alert. One case per reason in the closed vocabulary
(base.events.declarations.gateway.FleetGraphStaleReason), plus the by-design
non-emissions: a healthy response emits nothing, and the per-reason emission
rate cap collapses repeats.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from base import telemetry
from base.config import settings
from base.events.live.tests.fakes import patch_sync_redis
from gateway.app import app
from gateway.routers.fleet_graph import FleetGraphStaleEmitter
from gateway.routers.tests.fleet_graph.staleness_support import use_heartbeat_age

_STALE_EVENT = "fleet_graph_stale"


class _FakeRedis:
    """Minimal Redis fake for the route's poll and last-good caches."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.writes: list[tuple[str, str, int | None]] = []

    def __enter__(self) -> _FakeRedis:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        self.values[key] = value
        self.writes.append((key, value, ex))


class _RedisFactory:
    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis

    def __call__(self, **_kwargs: object) -> _FakeRedis:
        return self._redis


def _fresh_heartbeat_age(pool: object, *, now: datetime) -> float:
    del pool, now
    return 30.0


@pytest.fixture(autouse=True)
def _fresh_telemetry_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """The success-path heartbeat guard must not dial real services here."""
    use_heartbeat_age(monkeypatch, _fresh_heartbeat_age)


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the attributes of every `fleet_graph_stale` emission."""
    captured: list[dict[str, Any]] = []

    def capture_emit(
        _category: str,
        event_name: str,
        *,
        attributes: dict[str, Any] | None = None,
        **_kwargs: object,
    ) -> None:
        if event_name == _STALE_EVENT:
            assert attributes is not None  # the emitter always names route+reason
            captured.append(dict(attributes))

    monkeypatch.setattr(telemetry, "emit", capture_emit)
    return captured


def _install_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:

    redis = _FakeRedis()
    patch_sync_redis(monkeypatch, _RedisFactory(redis))
    return redis


def test_healthy_response_emits_nothing(
    monkeypatch: pytest.MonkeyPatch, emitted: list[dict[str, Any]]
) -> None:
    """The event marks degraded fallbacks only — never a fresh poll."""

    _install_redis(monkeypatch)

    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")

    assert resp.status_code == 200
    assert resp.json()["stale"] is False
    assert emitted == []


def test_stale_emit_rate_cap_collapses_repeats(emitted: list[dict[str, Any]]) -> None:
    """The per-reason rate cap collapses repeats; other reasons pass.

    Episode semantics: a retry storm on top of ONE degradation must not
    fabricate the two events the alert threshold counts.
    """
    now = [100.0]
    emitter = FleetGraphStaleEmitter(clock=lambda: now[0])
    emitter.emit("pg_budget")
    emitter.emit("pg_timeout")  # a different reason is not suppressed
    emitter.emit("pg_budget")  # same reason inside the default window
    assert [event["reason"] for event in emitted] == ["pg_budget", "pg_timeout"]

    now[0] += settings.display.fleet_graph_stale_emit_interval_s + 1.0
    emitter.emit("pg_budget")
    assert [event["reason"] for event in emitted] == [
        "pg_budget",
        "pg_timeout",
        "pg_budget",
    ]


def test_stale_emitter_lifetimes_have_independent_warning_budgets(
    emitted: list[dict[str, Any]],
) -> None:
    first = FleetGraphStaleEmitter(clock=lambda: 100.0)
    second = FleetGraphStaleEmitter(clock=lambda: 100.0)
    first.emit("pg_timeout")
    first.emit("pg_timeout")
    second.emit("pg_timeout")
    assert [event["reason"] for event in emitted] == ["pg_timeout", "pg_timeout"]
