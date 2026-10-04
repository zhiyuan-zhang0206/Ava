"""Read-side heartbeat guard for the telemetry served from `telemetry_events`.

The heartbeat is the gateway's own ``gateway_latency`` telemetry event. Its
60-second flusher advances whenever request traffic exists, regardless of agent
activity, unlike ``llm_usage`` counters that legitimately idle. It also isolates
the gateway exporter: during the 2026-08-23 incident, gateway metrics stopped
while agent LLM metrics continued, so whole-event-stream freshness stayed green. The
heartbeat's newest row in `telemetry_events` says whether the gateway's own events still reach
the record.

Five minutes is five times the heartbeat cadence. It is deliberately not three
times the 15-second metric export interval: a 45-second deadline for a 60-second
heartbeat would alert during healthy operation. Missing or old samples mark
read responses stale and emit transition events; check failures themselves fail
open because each read path owns backend-outage degradation separately.

The five-minute threshold is coarse enough that checking once per minute cannot
miss it. The check cadence keeps this guard off the fleet graph's cold-path hot
loop while still detecting staleness before the next threshold-sized window.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from base.log import logger

HEARTBEAT_EVENT = "gateway_latency"
STALENESS_THRESHOLD_S = 300
CHECK_INTERVAL_S = 60


@dataclass
class _SourceState:
    stale_since: float
    last_reported: float


def heartbeat_age(pool: Any, *, now: datetime) -> float | None:
    """Age in seconds of the newest heartbeat row in `telemetry_events`, if any.

    Only the last `STALENESS_THRESHOLD_S * 2` seconds are read, so the scan stays on the newest
    rows; a heartbeat older than that is as stale as a missing one.
    """
    with pool.connection() as conn:
        conn.execute("SET LOCAL statement_timeout = '3s'")
        row = conn.execute(
            "SELECT max(ts) FROM telemetry_events WHERE event_name = %s AND ts > %s",
            (
                HEARTBEAT_EVENT,
                datetime.fromtimestamp(now.timestamp() - 2 * STALENESS_THRESHOLD_S, UTC),
            ),
        ).fetchone()
    newest = row[0] if row is not None else None
    return None if newest is None else now.timestamp() - newest.timestamp()


def _emit(event_name: str, attributes: dict[str, Any]) -> None:
    """Best-effort status event; the JSONL mirror survives an OTLP outage."""
    from base import telemetry

    with telemetry.failure_isolated("telemetry staleness status event"):
        telemetry.emit("telemetry", event_name, attributes=attributes)


class TelemetryStaleness:
    """The read-side heartbeat guard: transition state plus the check cadence.

    One instance per gateway process (built by the app lifespan). `check_and_report`
    is the only entry point; `read_heartbeat_age` reads the newest heartbeat's age and
    `monotonic` is the cadence clock.
    """

    def __init__(
        self,
        *,
        check_interval_s: float = CHECK_INTERVAL_S,
        read_heartbeat_age: Callable[..., float | None] = heartbeat_age,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._check_interval_s = check_interval_s
        self._monotonic = monotonic
        self._read_heartbeat_age = read_heartbeat_age
        self._source_states: dict[str, _SourceState] = {}
        self._last_check_monotonic: float | None = None
        self._last_stale = False
        self._lock = threading.Lock()

    def _report_source(self, *, source: str, age_s: float | None, now_s: float) -> bool:
        stale = age_s is None or age_s > STALENESS_THRESHOLD_S
        state = self._source_states.get(source)
        if stale:
            reason = "heartbeat missing" if age_s is None else "heartbeat older than threshold"
            if state is None:
                state = _SourceState(stale_since=now_s, last_reported=now_s)
                self._source_states[source] = state
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
            self._source_states.pop(source, None)
            _emit(
                "telemetry_read_recovered",
                {
                    "source": source,
                    "signal": HEARTBEAT_EVENT,
                    "stale_duration_s": now_s - state.stale_since,
                },
            )
        return False

    def check_and_report(self, pool: Any, *, now: datetime | None = None) -> bool:
        """Return whether a successful telemetry read should be marked stale.

        A heartbeat-query exception is not a staleness verdict: it is logged at debug,
        left out of this poll's result, and does not mutate transition state. The
        fail-open verdict is cached on a monotonic cadence alongside successful checks.
        """
        with self._lock:
            checked_at = self._monotonic()
            if (
                self._last_check_monotonic is not None
                and checked_at - self._last_check_monotonic < self._check_interval_s
            ):
                return self._last_stale
            moment = now or datetime.now(UTC)
            stale = False
            try:
                age_s = self._read_heartbeat_age(pool, now=moment)
                stale = self._report_source(
                    source="postgres", age_s=age_s, now_s=moment.timestamp()
                )
            except Exception:
                logger.opt(exception=True).warning(
                    "telemetry heartbeat check failed; the verdict falls back to not-stale"
                )
            self._last_check_monotonic = checked_at
            self._last_stale = stale
            return stale
