"""Gateway agent-registry max-id gauge (task #2010).

The fleet grows by spawning agents; the registry high-water mark
(``max(id)`` of the ``agents`` table) is a lossless growth curve — it never
resets and needs no retention window, so a spurt (e.g. +300 in a day, a batch
spawn) shows up as a clean slope on the ops dashboard.

The gateway samples it once per `FLUSH_INTERVAL_S` and emits ONE
``agent_registry`` telemetry event carrying ``max_id``. The OTLP exporter maps
an int payload field to a Counter by default, but this value is absolute
state, never a sum — the ``_METRIC_DISPOSITION`` override in
``shared/telemetry_otlp.py`` records it as an ObservableGauge, exported to
Prometheus as ``ava_agent_registry_max_id_ratio`` (the unit-"1" gauge suffix,
the same naming as ``resolution_status`` / ``checkpoint_table_sizes``).

Wiring mirrors the task #1712 auth-401 aggregate (``gateway/_auth401_log.py``
+ ``gateway/_latency.py``): bounded row rate (1/min), never per event; and
the flusher never raises out of the loop — a dropped sample is only a
monitoring gap, and the emit pipeline itself is already best-effort.
"""

from __future__ import annotations

import asyncio
import logging

from psycopg_pool import ConnectionPool

from shared import telemetry

_log = logging.getLogger(__name__)

# One sample per minute — bounded row rate, same cadence as the auth-401 and
# latency flushers. A 60s gauge is plenty for a growth curve that changes
# only when agents are spawned.
FLUSH_INTERVAL_S = 60.0


def read_max_agent_id_blocking(pool: ConnectionPool) -> int | None:
    """The registry high-water mark: ``SELECT max(id) FROM agents``.

    Runs on a connection borrowed from the pool (blocking — call via
    ``asyncio.to_thread`` from the flusher). Returns ``None`` when the table
    is empty; an empty registry is not a number worth emitting.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT max(id) FROM agents")
        row = cur.fetchone()
    value = row[0] if row is not None else None
    return int(value) if value is not None else None


def emit_max_agent_id(max_id: int) -> None:
    """Emit one ``agent_registry`` telemetry event carrying ``max_id``.

    Exposed separately from the flusher so tests can drive it directly
    (mirrors ``_latency.emit_bucket`` / ``_auth401_log.emit_auth401_count``).
    """
    telemetry.emit("telemetry", "agent_registry", attributes={"max_id": max_id})


async def max_agent_id_flusher(pool: ConnectionPool) -> None:
    """Sample the registry max id every `FLUSH_INTERVAL_S` and emit.

    Runs as a lifespan background task. A failed read never kills the loop —
    the next tick retries; the DB work is dispatched to a worker thread so
    the gateway event loop never blocks on psycopg.
    """
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_S)
        try:
            max_id = await asyncio.to_thread(read_max_agent_id_blocking, pool)
            if max_id is not None:
                emit_max_agent_id(max_id)
        except Exception:
            _log.warning("agent max-id flush failed", exc_info=True)
