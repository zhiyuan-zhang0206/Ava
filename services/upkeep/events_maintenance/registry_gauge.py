"""Agent-registry max-id gauge (task #2010), a loop of the events-maintenance service.

The fleet grows by spawning agents; the registry high-water mark
(``max(id)`` of the ``agents`` table) is a lossless growth curve — it never
resets and needs no retention window, so a spurt (e.g. +300 in a day, a batch
spawn) shows up as a clean slope on the ops dashboard.

The `registry_gauge` loop samples it once per `FLUSH_INTERVAL_S` and emits ONE
``agent_registry`` telemetry event carrying ``max_id``. The OTLP exporter maps
an int payload field to a Counter by default, but this value is absolute
state, never a sum — the ``_METRIC_DISPOSITION`` override in
``base/telemetry/otlp/telemetry_otlp.py`` records it as an ObservableGauge, exported to
Prometheus as ``ava_agent_registry_max_id_ratio`` (the unit-"1" gauge suffix,
the same naming as ``resolution_status`` / ``checkpoint_table_sizes``).

Wiring mirrors the task #1712 auth-401 aggregate (``gateway/auth/rejection_log.py``
+ ``gateway/middleware/latency.py``): bounded row rate (1/min), never per event.
The gauge is absolute state sampled from the database, so it needs no gateway
process; it runs in the service that already owns the data plane's periodic reads.
An unreachable database skips the sample; any other exception ends the loop and,
through the service's `TaskGroup`, the process (the supervisor restarts it).
"""

from __future__ import annotations

import asyncio

from psycopg_pool import ConnectionPool

from base import telemetry
from base.daemon import round_loop
from base.daemon.loop_health import LoopProgress

# One sample per minute — bounded row rate, same cadence as the auth-401 and
# latency flushers. A 60s gauge is plenty for a growth curve that changes
# only when agents are spawned.
FLUSH_INTERVAL_S = 60.0


def read_max_agent_id_blocking(pool: ConnectionPool) -> int | None:
    """The registry high-water mark: ``SELECT max(id) FROM agents``.

    Runs on a connection borrowed from the pool (blocking — call via
    ``asyncio.to_thread`` from the loop). Returns ``None`` when the table
    is empty; an empty registry is not a number worth emitting.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT max(id) FROM agents")
        row = cur.fetchone()
    value = row[0] if row is not None else None
    return int(value) if value is not None else None


def emit_max_agent_id(max_id: int) -> None:
    """Emit one ``agent_registry`` telemetry event carrying ``max_id``.

    Exposed separately from the loop so tests can drive it directly
    (mirrors ``latency.emit_bucket`` / ``rejection_log.emit_auth401_count``).
    """
    telemetry.emit("telemetry", "agent_registry", attributes={"max_id": max_id})


async def registry_gauge_round(pool: ConnectionPool) -> None:
    """Sample the registry max id once and emit it (nothing for an empty registry)."""
    max_id = await asyncio.to_thread(read_max_agent_id_blocking, pool)
    if max_id is not None:
        emit_max_agent_id(max_id)


async def registry_gauge_loop(pool: ConnectionPool, progress: LoopProgress) -> None:
    """Sample the registry max id every `FLUSH_INTERVAL_S` and emit, as a resident
    sequential loop (one sample at once, so a restart leaves no gap)."""

    async def one_round() -> None:
        await registry_gauge_round(pool)

    await round_loop.run_rounds("registry-gauge", progress, FLUSH_INTERVAL_S, one_round)
