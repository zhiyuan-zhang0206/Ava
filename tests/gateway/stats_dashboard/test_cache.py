"""Dashboard cache policy with deterministic clocks and blocked refresh workers."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from gateway import _loki_transport, loki_query_budget
from gateway.app import app
from gateway.routers import _stats_dashboard, status
from gateway.schemas import StatsDashboard, StatsTokens, StatsWindowHours
from shared import telemetry
from shared.config import settings
from shared.config.display import DisplaySettings
from tests.gateway.stats_dashboard.test_routes import _CacheClock


def _payload(hours: StatsWindowHours = StatsWindowHours.H24) -> StatsDashboard:
    return StatsDashboard(
        live_count=1,
        window_hours=hours,
        tokens=StatsTokens(input=10, output=5, cache_read=0, cache_hit_pct=0),
        cost_usd=1.0,
        avg_turn_seconds=None,
        warnings=0,
        errors=0,
        warnings_dismissed=0,
        warnings_net=0,
        errors_dismissed=0,
        errors_net=0,
        total_events=0,
        plugin_stats=[],
        as_of=datetime.now(UTC),
    )


def _read(hours: StatsWindowHours = StatsWindowHours.H24) -> StatsDashboard:
    request = cast(
        Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=None)))
    )
    return status.get_stats_dashboard(request, hours)


def _wait_refresh(hours: StatsWindowHours = StatsWindowHours.H24) -> None:
    lock = _stats_dashboard._refresh_locks[hours]
    assert lock.acquire(timeout=3), "background refresh did not finish"
    lock.release()


@pytest.fixture(autouse=True)
def cache_clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[_CacheClock]:
    clock = _CacheClock()
    monkeypatch.setattr(_stats_dashboard, "_monotonic", clock)
    _stats_dashboard.cache_clear()
    _stats_dashboard._stale_emit_at.clear()
    yield clock
    for hours in StatsWindowHours:
        _wait_refresh(hours)
    _stats_dashboard.cache_clear()
    _stats_dashboard._stale_emit_at.clear()


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def capture(_category: str, event_name: str, **kwargs: Any) -> None:
        if event_name == "stats_dashboard_stale":
            events.append(kwargs["attributes"])

    monkeypatch.setattr(telemetry, "emit", capture)
    return events


@pytest.mark.parametrize("ttl", [60.0, 120.0])
def test_fresh_hit_returns_identical_payload_without_refresh(
    monkeypatch: pytest.MonkeyPatch, cache_clock: _CacheClock, ttl: float
) -> None:
    monkeypatch.setattr(settings.display, "stats_dashboard_cache_ttl_s", ttl)
    payload = _payload()
    _stats_dashboard.cache_put(StatsWindowHours.H24, payload)
    cache_clock.advance(ttl - 0.01)

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("fresh hit reached refresh scheduling or backend computation")

    monkeypatch.setattr(_stats_dashboard, "refresh_or_serve", unexpected)
    monkeypatch.setattr(status, "_compute_stats_dashboard", unexpected)
    assert _read() is payload


@pytest.mark.parametrize(("ttl", "cap", "age"), [(60.0, 300.0, 60.0), (10.0, 20.0, 20.0)])
def test_expired_concurrent_reads_return_before_one_refresh_finishes(
    monkeypatch: pytest.MonkeyPatch, cache_clock: _CacheClock, ttl: float, cap: float, age: float
) -> None:
    monkeypatch.setattr(settings.display, "stats_dashboard_cache_ttl_s", ttl)
    monkeypatch.setattr(settings.display, "stats_dashboard_swr_max_s", cap)
    original = _payload()
    updated = original.model_copy(update={"cost_usd": 2.0, "as_of": datetime.now(UTC)})
    _stats_dashboard.cache_put(StatsWindowHours.H24, original)
    cache_clock.advance(age)
    entered, release = threading.Event(), threading.Event()
    workers: list[threading.Thread] = []

    def compute(*_args: object) -> StatsDashboard:
        workers.append(threading.current_thread())
        entered.set()
        assert release.wait(3)
        return updated

    monkeypatch.setattr(status, "_compute_stats_dashboard", compute)
    with ThreadPoolExecutor(max_workers=8) as executor:
        pending = [executor.submit(_read) for _ in range(16)]
        try:
            assert entered.wait(1)
            results = [future.result(timeout=1) for future in pending]
            assert len(workers) == 1
            assert workers[0].daemon
            assert all(result is original for result in results)
            assert original.stale is False
        finally:
            release.set()
    _wait_refresh()
    assert _read() is updated


@pytest.mark.parametrize(
    ("age", "cap"), [(None, 300.0), (301.0, 300.0), (121.0, 120.0), (61.0, 0.0)]
)
def test_cold_over_cap_and_disabled_swr_wait_for_recompute(
    monkeypatch: pytest.MonkeyPatch, cache_clock: _CacheClock, age: float | None, cap: float
) -> None:
    monkeypatch.setattr(settings.display, "stats_dashboard_swr_max_s", cap)
    if age is not None:
        _stats_dashboard.cache_put(StatsWindowHours.H24, _payload())
        cache_clock.advance(age)
    updated = _payload()
    entered, release = threading.Event(), threading.Event()
    callers: list[int | None] = []

    def compute(*_args: object) -> StatsDashboard:
        callers.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return updated

    def read() -> StatsDashboard:
        callers.append(threading.get_ident())
        return _read()

    monkeypatch.setattr(status, "_compute_stats_dashboard", compute)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(read)
        try:
            assert entered.wait(1)
            assert not pending.done()
        finally:
            release.set()
        assert pending.result(timeout=1) is updated
    assert callers[0] == callers[1]
    assert _read() is updated


def test_refresh_is_independent_per_window(
    monkeypatch: pytest.MonkeyPatch, cache_clock: _CacheClock
) -> None:
    windows = (StatsWindowHours.H24, StatsWindowHours.D7)
    entered = {hours: threading.Event() for hours in windows}
    release = threading.Event()
    for hours in windows:
        _stats_dashboard.cache_put(hours, _payload(hours))
    cache_clock.advance(61)

    def compute(_pool: object, hours: StatsWindowHours) -> StatsDashboard:
        entered[hours].set()
        assert release.wait(3)
        return _payload(hours)

    monkeypatch.setattr(status, "_compute_stats_dashboard", compute)
    try:
        results = [_read(hours) for hours in windows]
        assert all(signal.wait(1) for signal in entered.values())
        assert [result.window_hours for result in results] == list(windows)
        assert all(not result.stale for result in results)
    finally:
        release.set()


@pytest.mark.parametrize("reason", ["loki_failed", "loki_budget"])
def test_failed_refresh_retains_payload_and_age_and_rate_caps_events(
    monkeypatch: pytest.MonkeyPatch,
    cache_clock: _CacheClock,
    emitted: list[dict[str, Any]],
    reason: str,
) -> None:
    original = _payload()
    _stats_dashboard.cache_put(StatsWindowHours.H24, original)
    attempts = 0
    cache_clock.advance(61)

    def compute(*_args: object) -> StatsDashboard:
        nonlocal attempts
        attempts += 1
        if reason == "loki_budget":
            raise loki_query_budget.LokiQueryBudgetError("queue_full")
        raise httpx.ReadTimeout("test refresh timeout")

    monkeypatch.setattr(status, "_compute_stats_dashboard", compute)
    assert _read() is original
    _wait_refresh()
    assert _read() == original.model_copy(update={"stale": True})
    assert _read().stale
    _wait_refresh()
    assert attempts == 1  # polls during backoff cannot start another failed attempt
    cache_clock.advance(60)
    assert _read().stale
    _wait_refresh()
    assert attempts == 2  # TTL expiry permits a retry
    assert emitted == [{"route": "/api/stats/dashboard", "reason": reason}]
    assert _stats_dashboard.cache_get_last_good(StatsWindowHours.H24, max_age_s=300) is original
    cache_clock.advance(180)
    assert _stats_dashboard.cache_get_last_good(StatsWindowHours.H24, max_age_s=300) is None

    def recover(*_args: object) -> StatsDashboard:
        return _payload()

    monkeypatch.setattr(status, "_compute_stats_dashboard", recover)
    assert _read().stale is False  # failed worker released its lock; sync recovery succeeds


def test_background_pipeline_obeys_real_loki_admission(
    monkeypatch: pytest.MonkeyPatch, cache_clock: _CacheClock, emitted: list[dict[str, Any]]
) -> None:
    original = _payload()
    _stats_dashboard.cache_put(StatsWindowHours.H24, original)
    cache_clock.advance(61)
    monkeypatch.setattr(_loki_transport, "_read_gate", lambda: None)
    loki_query_budget.reset_for_tests(capacity=1, max_waiters=1, wait_timeout_s=0.01)
    try:
        with TestClient(app) as client, loki_query_budget.query_budget.slot():
            response = client.get("/api/stats/dashboard")
            _wait_refresh()
            assert response.status_code == 200
            assert response.json()["as_of"] == original.model_dump(mode="json")["as_of"]
            assert response.json()["stale"] is False
            assert emitted == [{"route": "/api/stats/dashboard", "reason": "loki_budget"}]
    finally:
        loki_query_budget.reset_for_tests()


def test_thread_start_failure_releases_refresh_lock(
    monkeypatch: pytest.MonkeyPatch, cache_clock: _CacheClock
) -> None:
    original = _payload()
    _stats_dashboard.cache_put(StatsWindowHours.H24, original)
    cache_clock.advance(61)

    def fail_start(_self: threading.Thread) -> None:
        raise RuntimeError("cannot start thread")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="cannot start thread"):
        _read()
    _wait_refresh()
    assert _stats_dashboard.cache_get_last_good(StatsWindowHours.H24, max_age_s=300) is original


def test_cache_settings_defaults_and_env_aliases() -> None:
    defaults = DisplaySettings()
    assert defaults.stats_dashboard_cache_ttl_s == 60.0
    assert defaults.stats_dashboard_swr_max_s == 300.0
    configured = DisplaySettings(
        AVA_STATS_DASHBOARD_CACHE_TTL_S=12.5, AVA_STATS_DASHBOARD_SWR_MAX_S=45.5
    )
    assert configured.stats_dashboard_cache_ttl_s == 12.5
    assert configured.stats_dashboard_swr_max_s == 45.5


def test_failed_refresh_respects_existing_failure_serve_cap(
    monkeypatch: pytest.MonkeyPatch, cache_clock: _CacheClock
) -> None:
    monkeypatch.setattr(settings.display, "stats_dashboard_stale_max_s", 120.0)
    original = _payload()
    _stats_dashboard.cache_put(StatsWindowHours.H24, original)
    cache_clock.advance(61)

    def fail(*_args: object) -> StatsDashboard:
        raise httpx.ReadTimeout("test refresh timeout")

    monkeypatch.setattr(status, "_compute_stats_dashboard", fail)
    assert _read() is original
    _wait_refresh()
    assert _read().stale
    cache_clock.advance(60)  # inside SWR cap, outside the failure fallback cap
    with pytest.raises(HTTPException) as rejected:
        _read()
    assert rejected.value.status_code == 503


def test_over_cap_request_joins_existing_refresh(
    monkeypatch: pytest.MonkeyPatch, cache_clock: _CacheClock
) -> None:
    original, updated = _payload(), _payload()
    _stats_dashboard.cache_put(StatsWindowHours.H24, original)
    cache_clock.advance(61)
    entered, release = threading.Event(), threading.Event()
    attempts = 0

    def compute(*_args: object) -> StatsDashboard:
        nonlocal attempts
        attempts += 1
        entered.set()
        assert release.wait(3)
        return updated

    monkeypatch.setattr(status, "_compute_stats_dashboard", compute)
    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            assert _read() is original
            assert entered.wait(1)
            cache_clock.advance(240)
            pending = executor.submit(_read)
            assert not pending.done()
        finally:
            release.set()
        assert pending.result(timeout=1) is updated
    assert attempts == 1
