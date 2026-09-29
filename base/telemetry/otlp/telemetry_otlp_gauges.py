"""Thread-safe absolute-state helpers for OTLP ObservableGauge callbacks."""

from __future__ import annotations

from typing import Any

GaugeValues = dict[tuple[str, str], dict[tuple[tuple[str, Any], ...], tuple[float, dict[str, Any]]]]


def record_gauge(
    gauge_values: GaugeValues,
    gauge_lock: Any,
    key: tuple[str, str],
    value: int | float,
    attrs: dict[str, Any],
    *,
    max_only: bool = False,
) -> None:
    """Replace an absolute value, or retain the greatest observed timestamp."""

    series = tuple(sorted(attrs.items()))
    with gauge_lock:
        values = gauge_values.setdefault(key, {})
        if max_only and series in values:
            value = max(value, values[series][0])
        values[series] = (float(value), dict(attrs))


def observable_gauge_callback(
    gauge_values: GaugeValues, gauge_lock: Any, key: tuple[str, str]
) -> Any:
    """Build the OTel callback that snapshots one gauge's current series."""

    def observe(_options: Any) -> list[Any]:
        from opentelemetry.metrics import Observation

        with gauge_lock:
            values = list(gauge_values.get(key, {}).values())
        return [Observation(value, attributes=attrs) for value, attrs in values]

    return observe
