"""Read-side heartbeat guard for telemetry served from Loki and Prometheus.

The heartbeat is the gateway's own ``gateway_latency`` telemetry event. Its
60-second flusher advances whenever request traffic exists, regardless of agent
activity, unlike ``llm_usage`` counters that legitimately idle. It also isolates
the gateway exporter: during the 2026-08-23 incident, gateway metrics stopped
while agent LLM metrics continued, so whole-event-stream freshness stayed green.

Five minutes is five times the heartbeat cadence. It is deliberately not three
times the 15-second metric export interval: a 45-second deadline for a 60-second
heartbeat would alert during healthy operation. Missing or old samples mark
read responses stale and emit transition events; check failures themselves fail
open because each read path owns backend-outage degradation separately.
"""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from gateway import loki_events, prom_metrics
from shared.log import logger

HEARTBEAT_EVENT = "gateway_latency"
HEARTBEAT_METRIC = "ava_gateway_latency_count_total"
STALENESS_THRESHOLD_S = 300


@dataclass
class _SourceState:
    stale_since: float
    last_reported: float


_source_states: dict[str, _SourceState] = {}
_state_lock = threading.Lock()


def prometheus_heartbeat_age(timeout_s: float | None = None) -> float | None:
    """Age in seconds of the newest Prometheus heartbeat sample, if any."""
    rows = prom_metrics.query(
        f"max(timestamp({HEARTBEAT_METRIC}))",
        timeout_s=timeout_s,
    )
    if not rows:
        return None
    newest_sample_s = max(value for _labels, value in rows)
    return time.time() - newest_sample_s


def loki_heartbeat_age(timeout_s: float | None = None) -> float | None:
    """Age in seconds of the newest Loki heartbeat event, if any."""
    now_s = time.time()
    now = datetime.fromtimestamp(now_s, UTC)
    rows, _has_more = loki_events.query_events(
        event_names=[HEARTBEAT_EVENT],
        categories=["telemetry"],
        from_=now - timedelta(seconds=3 * STALENESS_THRESHOLD_S),
        to=now,
        limit=1,
        direction="backward",
        timeout_s=timeout_s,
    )
    if not rows:
        return None
    newest_ts = rows[0]["ts"]
    if not isinstance(newest_ts, datetime):
        raise TypeError(f"Loki heartbeat timestamp is not a datetime: {newest_ts!r}")
    return now_s - newest_ts.timestamp()


def _emit(event_name: str, attributes: dict[str, Any]) -> None:
    """Best-effort status event; the JSONL mirror survives an OTLP outage."""
    with contextlib.suppress(Exception):
        from shared import telemetry

        telemetry.emit("telemetry", event_name, attributes=attributes)


def _report_source(*, source: str, age_s: float | None, now_s: float) -> bool:
    stale = age_s is None or age_s > STALENESS_THRESHOLD_S
    state = _source_states.get(source)
    if stale:
        reason = "heartbeat missing" if age_s is None else "heartbeat older than threshold"
        if state is None:
            state = _SourceState(stale_since=now_s, last_reported=now_s)
            _source_states[source] = state
            action = "entered"
        elif now_s - state.last_reported >= STALENESS_THRESHOLD_S:
            state.last_reported = now_s
            action = "ongoing"
        else:
            return True
        _emit(
            "telemetry_read_stale",
            {
                "source": source,
                "signal": HEARTBEAT_EVENT,
                "threshold_s": STALENESS_THRESHOLD_S,
                "age_s": age_s,
                "action": action,
                "reason": reason,
            },
        )
        return True

    if state is not None:
        _source_states.pop(source, None)
        _emit(
            "telemetry_read_recovered",
            {
                "source": source,
                "signal": HEARTBEAT_EVENT,
                "stale_duration_s": now_s - state.stale_since,
            },
        )
    return False


def check_and_report(*, now: datetime | None = None, timeout_s: float = 3.0) -> bool:
    """Return whether a successful telemetry read should be marked stale.

    Each source is checked independently. A heartbeat-query exception is not a
    staleness verdict: it is logged at debug, left out of this poll's result,
    and does not mutate transition state.
    """
    try:
        now_s = (now or datetime.now(UTC)).timestamp()
    except Exception as exc:
        logger.debug("telemetry heartbeat check could not read the clock: {}", exc)
        return False
    stale = False
    checks = (
        ("prometheus", prometheus_heartbeat_age),
        ("loki", loki_heartbeat_age),
    )
    for source, heartbeat_age in checks:
        try:
            age_s = heartbeat_age(timeout_s=timeout_s)
            with _state_lock:
                stale = _report_source(source=source, age_s=age_s, now_s=now_s) or stale
        except Exception as exc:
            logger.debug("telemetry heartbeat check failed for {}: {}", source, exc)
    return stale
