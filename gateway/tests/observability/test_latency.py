"""Gateway per-endpoint latency metering (Task #1091).

Unit coverage of the accumulator / percentile math / aggregate emission,
plus integration through the real app: a request lands under its matched
route *pattern* (not the concrete URL), and an unmatched path falls back to
its raw URL.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from gateway.http.middleware import latency
from tests.fixtures.gateway_config import gateway_test_client


@pytest.fixture(autouse=True)
def _clean_accumulator() -> Iterator[None]:
    """The middleware is registered on the shared app — reset the module
    accumulator around every test so samples never leak across tests."""
    latency.accumulator.clear()
    yield
    latency.accumulator.clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with gateway_test_client() as c:
        yield c


# ── percentile math ────────────────────────────────────────────────────────


def test_percentiles_nearest_rank() -> None:
    samples = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    p50, p95, p99, p100 = latency.percentiles(samples, 50.0, 95.0, 99.0, 100.0)
    assert (p50, p95, p99, p100) == (5.0, 10.0, 10.0, 10.0)


def test_percentiles_single_sample() -> None:
    assert latency.percentiles([42.0], 50.0, 95.0) == [42.0, 42.0]


def test_percentiles_empty_bucket_is_zero() -> None:
    assert latency.percentiles([], 50.0, 95.0) == [0.0, 0.0]


def test_percentiles_unsorted_input() -> None:
    assert latency.percentiles([10.0, 1.0, 5.0], 50.0) == [5.0]


# ── accumulator ────────────────────────────────────────────────────────────


def test_record_and_drain_roundtrip() -> None:
    latency.record("/api/health", 1.5)
    latency.record("/api/health", 2.5)
    latency.record("/api/agents", 9.0)
    batch = latency.drain()
    assert batch == {"/api/health": [1.5, 2.5], "/api/agents": [9.0]}
    # drain() swaps the whole dict — the accumulator is empty afterwards
    assert latency.drain() == {}


def test_record_caps_samples_per_route() -> None:
    route = "/api/health"
    for _ in range(latency.MAX_SAMPLES_PER_ROUTE + 500):
        latency.record(route, 1.0)
    batch = latency.drain()
    assert len(batch[route]) == latency.MAX_SAMPLES_PER_ROUTE


def test_record_folds_unmatched_routes_beyond_cap() -> None:
    for i in range(latency.MAX_DISTINCT_ROUTES):
        latency.record(f"/scan/{i}", 1.0)
    # One more distinct route than the cap — folds into the shared bucket.
    latency.record("/scan/overflow", 1.0)
    batch = latency.drain()
    assert "/scan/overflow" not in batch
    assert batch["_unmatched:other"] == [1.0]


# ── aggregate emission ─────────────────────────────────────────────────────


def test_emit_bucket_computes_stats(emitted_events: EmittedEvents) -> None:
    latency.emit_bucket("/api/health", [10.0, 20.0, 30.0])
    [event] = emitted_events
    assert event.category == "telemetry"
    assert event.event_name == "gateway_latency"
    assert event.attributes == {
        "route": "/api/health",
        "route_class": "fast",
        "p50_ms": 20.0,
        "p95_ms": 30.0,
        "p99_ms": 30.0,
        "max_ms": 30.0,
        "count": 3,
    }


def test_emit_bucket_distinguishes_tail_percentiles(emitted_events: EmittedEvents) -> None:
    latency.emit_bucket("/api/health", [float(i) for i in range(1, 101)])

    [captured] = emitted_events.attributes("gateway_latency")
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
        ("/api/agents/42/inspect/statistics", "slow"),
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
    assert latency.classify_route(route) == expected_class


def test_emit_bucket_empty_is_noop(emitted_events: EmittedEvents) -> None:
    latency.emit_bucket("/api/health", [])
    assert emitted_events.names() == []


# ── middleware through the real app ────────────────────────────────────────


def test_middleware_records_matched_route_pattern(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    batch = latency.drain()
    assert "/api/health" in batch
    assert len(batch["/api/health"]) == 1


def test_middleware_records_path_param_route_as_pattern(client: TestClient) -> None:
    # Unknown agent id — the request still routes (404 handled in-router), and
    # must be keyed by the route pattern, not the concrete id in the URL.
    resp = client.get("/api/agents/987654/messages")
    assert resp.status_code == 404
    batch = latency.drain()
    assert "/api/agents/987654/messages" not in batch
    assert any("messages" in route for route in batch), batch


def test_middleware_records_unmatched_path_raw(client: TestClient) -> None:
    resp = client.get("/definitely-not-a-route-xyz")
    assert resp.status_code == 404
    batch = latency.drain()
    assert "/definitely-not-a-route-xyz" in batch
