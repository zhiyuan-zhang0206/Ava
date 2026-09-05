"""Gateway per-endpoint latency metering (Task #1091).

Unit coverage of the accumulator / percentile math / aggregate emission,
plus integration through the real app: a request lands under its matched
route *pattern* (not the concrete URL), and an unmatched path falls back to
its raw URL.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway import _latency
from gateway.app import app


@pytest.fixture(autouse=True)
def _clean_accumulator() -> Iterator[None]:
    """The middleware is registered on the shared app — reset the module
    accumulator around every test so samples never leak across tests."""
    _latency._accumulator.clear()
    yield
    _latency._accumulator.clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


# ── percentile math ────────────────────────────────────────────────────────


def test_percentiles_nearest_rank() -> None:
    samples = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    p50, p95, p99, p100 = _latency.percentiles(samples, 50.0, 95.0, 99.0, 100.0)
    assert (p50, p95, p99, p100) == (5.0, 10.0, 10.0, 10.0)


def test_percentiles_single_sample() -> None:
    assert _latency.percentiles([42.0], 50.0, 95.0) == [42.0, 42.0]


def test_percentiles_empty_bucket_is_zero() -> None:
    assert _latency.percentiles([], 50.0, 95.0) == [0.0, 0.0]


def test_percentiles_unsorted_input() -> None:
    assert _latency.percentiles([10.0, 1.0, 5.0], 50.0) == [5.0]


# ── accumulator ────────────────────────────────────────────────────────────


def test_record_and_drain_roundtrip() -> None:
    _latency.record("/api/health", 1.5)
    _latency.record("/api/health", 2.5)
    _latency.record("/api/agents", 9.0)
    batch = _latency.drain()
    assert batch == {"/api/health": [1.5, 2.5], "/api/agents": [9.0]}
    # drain() swaps the whole dict — the accumulator is empty afterwards
    assert _latency.drain() == {}


def test_record_caps_samples_per_route() -> None:
    route = "/api/health"
    for _ in range(_latency.MAX_SAMPLES_PER_ROUTE + 500):
        _latency.record(route, 1.0)
    batch = _latency.drain()
    assert len(batch[route]) == _latency.MAX_SAMPLES_PER_ROUTE


def test_record_folds_unmatched_routes_beyond_cap() -> None:
    for i in range(_latency.MAX_DISTINCT_ROUTES):
        _latency.record(f"/scan/{i}", 1.0)
    # One more distinct route than the cap — folds into the shared bucket.
    _latency.record("/scan/overflow", 1.0)
    batch = _latency.drain()
    assert "/scan/overflow" not in batch
    assert batch["_unmatched:other"] == [1.0]


# ── aggregate emission ─────────────────────────────────────────────────────


def test_emit_bucket_computes_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        _latency.telemetry,
        "emit",
        lambda category, event_name, **kwargs: captured.update(  # pyright: ignore[reportUnknownArgumentType]
            category=category, event_name=event_name, **kwargs
        ),
    )
    _latency.emit_bucket("/api/health", [10.0, 20.0, 30.0])
    assert captured["category"] == "telemetry"
    assert captured["event_name"] == "gateway_latency"
    attrs = captured["attributes"]
    assert attrs == {
        "route": "/api/health",
        "route_class": "fast",
        "p50_ms": 20.0,
        "p95_ms": 30.0,
        "p99_ms": 30.0,
        "max_ms": 30.0,
        "count": 3,
    }


def test_emit_bucket_distinguishes_tail_percentiles(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_emit(_category: str, _event_name: str, **kwargs: Any) -> None:
        captured.update(kwargs["attributes"])

    monkeypatch.setattr(_latency.telemetry, "emit", fake_emit)
    _latency.emit_bucket("/api/health", [float(i) for i in range(1, 101)])

    assert captured["p95_ms"] == 95.0
    assert captured["p99_ms"] == 99.0
    assert captured["max_ms"] == 100.0


@pytest.mark.parametrize(
    ("route", "expected_class"),
    [
        ("/api/agents/42/messages", "llm"),
        ("/api/agents/42/shell/3", "llm"),
        ("/api/agents/42/traces/abc", "llm"),
        ("/api/memory/search", "slow"),
        ("/api/agents/42/inspect", "slow"),
        ("/api/stats/dashboard", "slow"),
        ("/api/metrics/agents", "slow"),
        ("/api/events", "slow"),
        ("/api/fleet/graph", "slow"),
        ("/api/agents/42/terminate", "slow"),
        ("/api/agents/42/resurrect", "slow"),
        ("/api/agents/42/neighbors", "slow"),
        ("/api/health", "fast"),
        ("/api/agents", "fast"),
        ("/api/metrics/summary", "fast"),
    ],
)
def test_route_classification_matches_alert_eligibility(route: str, expected_class: str) -> None:
    assert _latency.classify_route(route) == expected_class


def test_emit_bucket_empty_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _fail(*args: Any, **kwargs: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(_latency.telemetry, "emit", _fail)
    _latency.emit_bucket("/api/health", [])
    assert not called


# ── middleware through the real app ────────────────────────────────────────


def test_middleware_records_matched_route_pattern(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    batch = _latency.drain()
    assert "/api/health" in batch
    assert len(batch["/api/health"]) == 1


def test_middleware_records_path_param_route_as_pattern(client: TestClient) -> None:
    # Unknown agent id — the request still routes (404 handled in-router), and
    # must be keyed by the route pattern, not the concrete id in the URL.
    resp = client.get("/api/agents/987654/messages")
    assert resp.status_code == 404
    batch = _latency.drain()
    assert "/api/agents/987654/messages" not in batch
    assert any("messages" in route for route in batch), batch


def test_middleware_records_unmatched_path_raw(client: TestClient) -> None:
    resp = client.get("/definitely-not-a-route-xyz")
    assert resp.status_code == 404
    batch = _latency.drain()
    assert "/definitely-not-a-route-xyz" in batch
