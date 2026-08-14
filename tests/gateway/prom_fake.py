"""In-memory Prometheus stand-in for the gateway ops-monitor tests.

`gateway/prom_metrics.query` / `query_range` are monkeypatched onto an
instance. Register synthetic series by expression substring:

- `add_range("increase(ava_llm_usage_in_total", [(ts_s, v), ...])` answers
  every range query whose expression contains the substring (the ops module
  builds one expression per instrument);
- `add_instant("histogram_quantile(0.5", 123.0)` answers instant queries
  (the window p50/p95 totals).

An expression matching nothing yields no series — the ops module zero-fills,
so empty tests exercise the empty path.
"""

from __future__ import annotations

from datetime import datetime


class FakePrometheus:
    def __init__(self) -> None:
        self.range_series: list[tuple[str, list[tuple[int, float]]]] = []
        self.instant_values: list[tuple[str, float]] = []

    def add_range(self, expr: str, values: list[tuple[int, float]]) -> None:
        self.range_series.append((expr, values))

    def add_instant(self, expr: str, value: float) -> None:
        self.instant_values.append((expr, value))

    def query_range(
        self, expr: str, *, start: datetime, end: datetime, step_s: int
    ) -> list[tuple[dict[str, str], list[tuple[int, float]]]]:
        for sub, values in self.range_series:
            if sub in expr:
                return [({}, list(values))]
        return []

    def query(self, expr: str) -> list[tuple[dict[str, str], float]]:
        for sub, value in self.instant_values:
            if sub in expr:
                return [({}, value)]
        return []

    # convenience: register all llm instruments at once
    def add_llm_bucket(
        self,
        *,
        calls: float,
        tokens_in: float,
        tokens_out: float,
        tokens_reasoning: float,
        lat_sum: float,
        p50: float,
        p95: float,
        point: tuple[int, float],
    ) -> None:
        """One step's worth of Prometheus llm_usage series, all at `point`."""
        self.add_range("increase(ava_llm_usage_latency_ms_milliseconds_count", [(point[0], calls)])
        self.add_range("increase(ava_llm_usage_in_total", [(point[0], tokens_in)])
        self.add_range("increase(ava_llm_usage_out_total", [(point[0], tokens_out)])
        self.add_range("increase(ava_llm_usage_reasoning_total", [(point[0], tokens_reasoning)])
        self.add_range("increase(ava_llm_usage_latency_ms_milliseconds_sum", [(point[0], lat_sum)])
        self.add_range("histogram_quantile(0.5", [(point[0], p50)])
        self.add_range("histogram_quantile(0.95", [(point[0], p95)])
        self.add_instant("histogram_quantile(0.5", p50)
        self.add_instant("histogram_quantile(0.95", p95)
