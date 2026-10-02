"""Read-side telemetry heartbeat staleness tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from base import telemetry
from gateway.lgtm import telemetry_staleness


@pytest.fixture(autouse=True)
def _isolated_source_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telemetry_staleness, "_source_states", {})
    monkeypatch.setattr(telemetry_staleness, "_check_state", telemetry_staleness._CheckState())
    monkeypatch.setattr(telemetry_staleness, "CHECK_INTERVAL_S", 0, raising=False)


def test_prometheus_heartbeat_age_reads_newest_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_query(
        expr: str, *, timeout_s: float | None = None
    ) -> list[tuple[dict[str, str], float]]:
        seen.update(expr=expr, timeout_s=timeout_s)
        return [({}, 1_699_999_970.0)]

    monkeypatch.setattr(telemetry_staleness.prom_metrics, "query", fake_query)
    monkeypatch.setattr(telemetry_staleness.time, "time", lambda: 1_700_000_000.0)

    assert telemetry_staleness.prometheus_heartbeat_age(timeout_s=1.25) == 30.0
    assert seen == {
        "expr": "max(timestamp(ava_gateway_latency_count_total))",
        "timeout_s": 1.25,
    }

    def empty_query(
        _expr: str, *, timeout_s: float | None = None
    ) -> list[tuple[dict[str, str], float]]:
        del timeout_s
        return []

    monkeypatch.setattr(telemetry_staleness.prom_metrics, "query", empty_query)
    assert telemetry_staleness.prometheus_heartbeat_age() is None


def test_check_reports_stale_rate_limits_and_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    ages: dict[str, float | None] = {"prometheus": 30.0}
    emitted: list[tuple[str, dict[str, Any]]] = []

    def prometheus_age(*, timeout_s: float | None = None) -> float | None:
        del timeout_s
        return ages["prometheus"]

    monkeypatch.setattr(telemetry_staleness, "prometheus_heartbeat_age", prometheus_age)

    def capture_emit(
        _category: str, event_name: str, *, attributes: dict[str, Any], **_kwargs: Any
    ) -> None:
        emitted.append((event_name, attributes))

    monkeypatch.setattr(telemetry, "emit", capture_emit)
    started = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)

    assert telemetry_staleness.check_and_report(now=started) is False
    assert emitted == []

    ages["prometheus"] = None
    assert telemetry_staleness.check_and_report(now=started) is True
    assert emitted == [
        (
            "telemetry_read_stale",
            {
                "source": "prometheus",
                "signal": "gateway_latency",
                "threshold_s": 300,
                "age_s": None,
                "action": "entered",
                "reason": "heartbeat missing",
            },
        )
    ]

    assert telemetry_staleness.check_and_report(now=datetime(2026, 8, 23, 12, 4, 59, tzinfo=UTC))
    assert len(emitted) == 1

    assert telemetry_staleness.check_and_report(now=datetime(2026, 8, 23, 12, 5, tzinfo=UTC))
    assert emitted[-1][0] == "telemetry_read_stale"
    assert emitted[-1][1]["action"] == "ongoing"

    ages["prometheus"] = 30.0
    assert (
        telemetry_staleness.check_and_report(now=datetime(2026, 8, 23, 12, 6, tzinfo=UTC)) is False
    )
    assert emitted[-1] == (
        "telemetry_read_recovered",
        {
            "source": "prometheus",
            "signal": "gateway_latency",
            "stale_duration_s": 360.0,
        },
    )


def test_check_throttles_heartbeat_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"prometheus": 0}
    monotonic_times = iter((100.0, 100.1, 160.1))

    def heartbeat_age(source: str) -> float:
        calls[source] += 1
        return 30.0

    def prometheus_age(*, timeout_s: float | None = None) -> float:
        del timeout_s
        return heartbeat_age("prometheus")

    monkeypatch.setattr(telemetry_staleness, "CHECK_INTERVAL_S", 60)
    monkeypatch.setattr(telemetry_staleness.time, "monotonic", lambda: next(monotonic_times))
    monkeypatch.setattr(telemetry_staleness, "prometheus_heartbeat_age", prometheus_age)

    assert telemetry_staleness.check_and_report() is False
    assert telemetry_staleness.check_and_report() is False
    assert calls == {"prometheus": 1}

    assert telemetry_staleness.check_and_report() is False
    assert calls == {"prometheus": 2}


def test_check_fail_open_when_heartbeat_queries_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted: list[str] = []

    def boom(*, timeout_s: float | None = None) -> float | None:
        raise RuntimeError(f"backend failed at timeout {timeout_s}")

    monkeypatch.setattr(telemetry_staleness, "prometheus_heartbeat_age", boom)

    def capture_emit(_category: str, event_name: str, **_kwargs: Any) -> None:
        emitted.append(event_name)

    monkeypatch.setattr(telemetry, "emit", capture_emit)

    assert telemetry_staleness.check_and_report(timeout_s=0.25) is False
    assert emitted == []
