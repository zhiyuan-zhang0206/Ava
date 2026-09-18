"""Contract tests for the `fleet_graph_stale` emission (task #3925).

The ops alert rule `ava-ops-fleet-graph-stale` counts `fleet_graph_stale`
events, so every stale-serving fallback on GET /api/fleet/graph must emit
exactly one event per degradation episode — a path that serves stale silently
is a hole in the alert. One case per reason in the closed vocabulary
(shared.events.contract.FleetGraphStaleReason), plus the by-design
non-emissions: a healthy response emits nothing, re-serving the same episode
from the archive's negative cache does not re-emit, and the per-reason
emission rate cap collapses repeats.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from psycopg import errors as pg_errors

from gateway import loki_query_budget, prom_metrics, telemetry_staleness
from gateway.app import app
from shared import telemetry

_STALE_EVENT = "fleet_graph_stale"


class _FakeRedis:
    """Minimal Redis fake for the route's poll/last-good/frozen caches."""

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


def _fresh_heartbeat_age(*, timeout_s: float | None = None) -> float:
    del timeout_s
    return 30.0


@pytest.fixture(autouse=True)
def _fresh_telemetry_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """The success-path heartbeat guard must not dial real services here."""
    monkeypatch.setattr(telemetry_staleness, "prometheus_heartbeat_age", _fresh_heartbeat_age)
    monkeypatch.setattr(telemetry_staleness, "loki_heartbeat_age", _fresh_heartbeat_age)
    monkeypatch.setattr(telemetry_staleness, "_source_states", {})
    monkeypatch.setattr(telemetry_staleness, "CHECK_INTERVAL_S", 0, raising=False)


@pytest.fixture(autouse=True)
def _reset_stale_emitter() -> Iterator[None]:
    """The emitter's per-reason rate cap is process-global state."""
    import gateway.routers.fleet_graph as fg

    fg._stale_emit_at.clear()
    yield
    fg._stale_emit_at.clear()


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
    import gateway.routers.fleet_graph as fg

    redis = _FakeRedis()
    monkeypatch.setattr(fg, "sync_redis", _RedisFactory(redis))
    return redis


def _empty_pg_phase(*_args: object, **_kwargs: object) -> Any:
    """Stand-in for `_fetch_pg_graph`: empty node rows, no Postgres."""
    import gateway.routers.fleet_graph as fg

    return fg._PgGraphData([])


def _empty_prom_tokens(*_args: object, **_kwargs: object) -> dict[str, float]:
    return {}


def _empty_loki_tail(**_kwargs: object) -> tuple[list[Any], bool]:
    return [], False


def _empty_archive_rows() -> tuple[list[Any], bool]:
    return [], False


def _stub_pg(monkeypatch: pytest.MonkeyPatch) -> None:
    """The node phase answers empty without touching Postgres."""
    import gateway.routers.fleet_graph as fg

    monkeypatch.setattr(fg, "_fetch_pg_graph", _empty_pg_phase)


def _stub_archive_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    import gateway.routers.fleet_graph as fg

    monkeypatch.setattr(fg, "_cached_archive_edges", _empty_archive_rows)


def _stub_prom_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prom_metrics, "sum_by", _empty_prom_tokens)


def _get_stale() -> tuple[int, bool]:
    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")
    return resp.status_code, bool(resp.json()["stale"])


def test_pg_timeout_emits(monkeypatch: pytest.MonkeyPatch, emitted: list[dict[str, Any]]) -> None:
    """PG statement timeout (QueryCanceled) -> pg_timeout."""
    import gateway.routers.fleet_graph as fg

    _install_redis(monkeypatch)

    def canceled(*_a: object, **_k: object) -> object:
        raise pg_errors.QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(fg, "_fetch_pg_graph", canceled)

    status, stale = _get_stale()
    assert status == 200
    assert stale is True
    assert emitted == [{"route": "fleet_graph", "reason": "pg_timeout"}]


def test_pg_phase_budget_emits(
    monkeypatch: pytest.MonkeyPatch, emitted: list[dict[str, Any]]
) -> None:
    """PG phase crossing the route deadline -> pg_budget."""
    import gateway.routers.fleet_graph as fg

    _install_redis(monkeypatch)
    _stub_pg(monkeypatch)
    monotonic = iter((0.0, fg._ROUTE_TIMEOUT_S + 0.1))
    monkeypatch.setattr(fg, "_monotonic", lambda: next(monotonic))

    status, stale = _get_stale()
    assert status == 200
    assert stale is True
    assert emitted == [{"route": "fleet_graph", "reason": "pg_budget"}]


def test_archive_fetch_failure_emits_and_negative_caches(
    monkeypatch: pytest.MonkeyPatch, emitted: list[dict[str, Any]]
) -> None:
    """A failed frozen-archive scan -> fetch_failed + the 60s negative entry."""
    import gateway.routers.fleet_graph as fg

    redis = _install_redis(monkeypatch)
    _stub_pg(monkeypatch)

    def fail(*_a: object, **_k: object) -> object:
        raise httpx.ConnectError("loki archive unreachable")

    monkeypatch.setattr(fg, "_fetch_archive_edges", fail)

    status, stale = _get_stale()
    assert status == 200
    assert stale is True
    assert emitted == [{"route": "fleet_graph", "reason": "fetch_failed"}]
    negative = json.loads(redis.values[fg._FROZEN_ARCHIVE_CACHE_KEY])
    assert negative["degraded"] is True


def test_archive_negative_cache_reserve_does_not_reemit(
    monkeypatch: pytest.MonkeyPatch, emitted: list[dict[str, Any]]
) -> None:
    """Re-serving the same episode from the negative cache is not a new event.

    With the archive scan still failing, the second poll within the 60s
    negative window must serve stale WITHOUT calling the fetch again — one
    event per degradation episode, never per poll.
    """
    import gateway.routers.fleet_graph as fg

    _install_redis(monkeypatch)
    _stub_pg(monkeypatch)

    def fail(*_a: object, **_k: object) -> object:
        raise httpx.ConnectError("loki archive unreachable")

    monkeypatch.setattr(fg, "_fetch_archive_edges", fail)

    with TestClient(app) as client:
        first = client.get("/api/fleet/graph")
        second = client.get("/api/fleet/graph")

    assert first.json()["stale"] is True
    assert second.json()["stale"] is True
    assert emitted == [{"route": "fleet_graph", "reason": "fetch_failed"}]


def test_archive_lock_wait_emits(
    monkeypatch: pytest.MonkeyPatch, emitted: list[dict[str, Any]]
) -> None:
    """A waiter that cannot enter the single-flight archive scan -> lock_wait."""
    import gateway.routers.fleet_graph as fg

    _install_redis(monkeypatch)
    _stub_pg(monkeypatch)
    monkeypatch.setattr(fg, "_ARCHIVE_FETCH_WAIT_S", 0.05)

    assert fg._ARCHIVE_FETCH_LOCK.acquire(timeout=1)
    try:
        status, stale = _get_stale()
    finally:
        fg._ARCHIVE_FETCH_LOCK.release()

    assert status == 200
    assert stale is True
    assert emitted == [{"route": "fleet_graph", "reason": "lock_wait"}]


def test_archive_read_escape_emits_fetch_failed(
    monkeypatch: pytest.MonkeyPatch, emitted: list[dict[str, Any]]
) -> None:
    """An exception escaping the cached archive read -> fetch_failed."""
    import gateway.routers.fleet_graph as fg

    _install_redis(monkeypatch)
    _stub_pg(monkeypatch)

    def boom() -> object:
        raise httpx.ConnectError("archive read exploded")

    monkeypatch.setattr(fg, "_cached_archive_edges", boom)

    status, stale = _get_stale()
    assert status == 200
    assert stale is True
    assert emitted == [{"route": "fleet_graph", "reason": "fetch_failed"}]


def test_prom_admission_budget_emits(
    monkeypatch: pytest.MonkeyPatch, emitted: list[dict[str, Any]]
) -> None:
    """A refused Prometheus query admission -> prom_budget."""
    import gateway.routers.fleet_graph as fg

    _install_redis(monkeypatch)
    _stub_pg(monkeypatch)
    _stub_archive_ok(monkeypatch)
    monotonic = iter((0.0, 0.0))
    monkeypatch.setattr(fg, "_monotonic", lambda: next(monotonic))

    def refused(*_a: object, **_k: object) -> dict[str, float]:
        raise prom_metrics.PromQueryBudgetError("queue_full")

    monkeypatch.setattr(prom_metrics, "sum_by", refused)

    status, stale = _get_stale()
    assert status == 200
    assert stale is True
    assert emitted == [{"route": "fleet_graph", "reason": "prom_budget"}]


def test_prom_query_failure_emits(
    monkeypatch: pytest.MonkeyPatch, emitted: list[dict[str, Any]]
) -> None:
    """A Prometheus transport failure -> prom_failed."""
    import gateway.routers.fleet_graph as fg

    _install_redis(monkeypatch)
    _stub_pg(monkeypatch)
    _stub_archive_ok(monkeypatch)
    monotonic = iter((0.0, 0.0))
    monkeypatch.setattr(fg, "_monotonic", lambda: next(monotonic))

    def fail(*_a: object, **_k: object) -> dict[str, float]:
        raise httpx.ConnectError("prometheus unreachable")

    monkeypatch.setattr(prom_metrics, "sum_by", fail)

    status, stale = _get_stale()
    assert status == 200
    assert stale is True
    assert emitted == [{"route": "fleet_graph", "reason": "prom_failed"}]


def test_prom_phase_budget_emits(
    monkeypatch: pytest.MonkeyPatch, emitted: list[dict[str, Any]]
) -> None:
    """Prometheus phase crossing the route deadline -> prom_budget."""
    import gateway.routers.fleet_graph as fg

    _install_redis(monkeypatch)
    _stub_pg(monkeypatch)
    _stub_archive_ok(monkeypatch)
    _stub_prom_ok(monkeypatch)
    monotonic = iter((0.0, 0.0, fg._ROUTE_TIMEOUT_S + 0.1))
    monkeypatch.setattr(fg, "_monotonic", lambda: next(monotonic))

    status, stale = _get_stale()
    assert status == 200
    assert stale is True
    assert emitted == [{"route": "fleet_graph", "reason": "prom_budget"}]


def test_loki_admission_budget_emits(
    monkeypatch: pytest.MonkeyPatch, emitted: list[dict[str, Any]]
) -> None:
    """A refused Loki query admission -> loki_budget."""
    import gateway.routers.fleet_graph as fg

    _install_redis(monkeypatch)
    _stub_pg(monkeypatch)
    _stub_archive_ok(monkeypatch)
    _stub_prom_ok(monkeypatch)

    def refused(**_kwargs: object) -> tuple[list[Any], bool]:
        raise loki_query_budget.LokiQueryBudgetError("queue_full")

    monkeypatch.setattr(fg, "_fetch_loki_edges", refused)

    status, stale = _get_stale()
    assert status == 200
    assert stale is True
    assert emitted == [{"route": "fleet_graph", "reason": "loki_budget"}]


def test_loki_query_failure_emits(
    monkeypatch: pytest.MonkeyPatch, emitted: list[dict[str, Any]]
) -> None:
    """A Loki transport failure -> loki_failed."""
    import gateway.routers.fleet_graph as fg

    _install_redis(monkeypatch)
    _stub_pg(monkeypatch)
    _stub_archive_ok(monkeypatch)
    _stub_prom_ok(monkeypatch)

    def fail(**_kwargs: object) -> tuple[list[Any], bool]:
        raise httpx.ConnectError("loki unreachable")

    monkeypatch.setattr(fg, "_fetch_loki_edges", fail)

    status, stale = _get_stale()
    assert status == 200
    assert stale is True
    assert emitted == [{"route": "fleet_graph", "reason": "loki_failed"}]


def test_loki_phase_budget_emits(
    monkeypatch: pytest.MonkeyPatch, emitted: list[dict[str, Any]]
) -> None:
    """Loki phase crossing the route deadline -> loki_budget."""
    import gateway.routers.fleet_graph as fg

    _install_redis(monkeypatch)
    _stub_pg(monkeypatch)
    _stub_archive_ok(monkeypatch)
    _stub_prom_ok(monkeypatch)
    monotonic = iter((0.0, 0.0, 0.0, fg._ROUTE_TIMEOUT_S + 0.1))
    monkeypatch.setattr(fg, "_monotonic", lambda: next(monotonic))
    monkeypatch.setattr(fg, "_fetch_loki_edges", _empty_loki_tail)

    status, stale = _get_stale()
    assert status == 200
    assert stale is True
    assert emitted == [{"route": "fleet_graph", "reason": "loki_budget"}]


def test_healthy_response_emits_nothing(
    monkeypatch: pytest.MonkeyPatch, emitted: list[dict[str, Any]]
) -> None:
    """The event marks degraded fallbacks only — never a fresh poll."""
    import gateway.routers.fleet_graph as fg

    _install_redis(monkeypatch)
    _stub_pg(monkeypatch)
    _stub_archive_ok(monkeypatch)
    _stub_prom_ok(monkeypatch)
    monkeypatch.setattr(fg, "_fetch_loki_edges", _empty_loki_tail)

    with TestClient(app) as client:
        resp = client.get("/api/fleet/graph")

    assert resp.status_code == 200
    assert resp.json()["stale"] is False
    assert emitted == []


def test_stale_emit_rate_cap_collapses_repeats(
    monkeypatch: pytest.MonkeyPatch, emitted: list[dict[str, Any]]
) -> None:
    """The per-reason rate cap collapses repeats; other reasons pass.

    Episode semantics: a retry storm on top of ONE degradation must not
    fabricate the two events the alert threshold counts.
    """
    import gateway.routers.fleet_graph as fg

    fg._emit_stale("loki_failed")
    fg._emit_stale("pg_timeout")  # a different reason is not suppressed
    fg._emit_stale("loki_failed")  # same reason inside the default window
    assert [event["reason"] for event in emitted] == ["loki_failed", "pg_timeout"]

    monkeypatch.setattr(fg, "_stale_emit_interval_s", lambda: 0.0)
    fg._emit_stale("loki_failed")
    assert [event["reason"] for event in emitted] == [
        "loki_failed",
        "pg_timeout",
        "loki_failed",
    ]
