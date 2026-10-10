"""Cluster control + admin endpoints — /api/cluster/*.

Covers maintenance / status / roster / admin events query / machines
DELETE. These paths are exempt from the paused-host 503 middleware through
their CONTROL_PLANE route contracts (`base/api_contracts/contracts.py`)
because they are the tools the gateway uses during pause.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from loguru import logger
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from base.api_contracts.status import MachineStatus
from base.cluster import machines
from base.cluster.machine import (
    is_agent_runner,
    is_gateway,
    is_observability_station,
    machine_name,
)
from base.config import settings
from base.db import Database
from base.db.transaction import write_transaction
from base.deploy.git.cluster_drift import prod_source_head_sha
from base.native_process.loaded_commit import LoadedCommit
from gateway.cluster import snapshots
from gateway.cluster.roster_probe import IdentityMismatchLog
from gateway.cluster.schemas import AgentMachineRow, MachineDeleteResponse
from gateway.cluster.status import gather_cluster_status
from gateway.events import telemetry_rows
from gateway.events.schemas import AgentEventRow, AgentEventsResponse
from ops.cluster import operations as _ops
from ops.cluster import rpc as _cluster_rpc
from ops.cluster.pause import is_paused as cluster_is_paused
from ops.cluster_status import ClusterStatus
from ops.cluster_status.schema_mismatch import status as schema_mismatch_status

router = APIRouter()
_log = logging.getLogger(__name__)


def _local_snapshot_blocking(db: Database, *, image: LoadedCommit) -> ClusterStatus:
    """Sync local snapshot for a pure gateway (no ops server) — via to_thread:
    paused flag (file), orchestration liveness (session probe) and the
    prod-source HEAD (git rev-parse) are all child-process / disk reads that
    must not run on the event loop."""
    paused = cluster_is_paused(db)
    return ClusterStatus(
        machine_name=machine_name(),
        serve_gateway=is_gateway(),
        serve_agent_runner=is_agent_runner(),
        serve_observability_station=is_observability_station(),
        paused=paused,
        # This local snapshot resolves paused from the posture row alone, so a
        # true verdict here has exactly one possible cause.
        paused_reason="business_pause" if paused else None,
        head_sha=prod_source_head_sha(),
        # This gateway process's own frozen commit — not a disk bookmark, so a
        # gateway that outlived a checkout advance reports the old commit and the
        # roster shows the drift.
        running_sha=image.sha,
        schema_mismatch=schema_mismatch_status(db),
    )


def _machines_rows_blocking(pool: ConnectionPool) -> list[tuple[Any, ...]]:
    """Sync machines-table read — via to_thread (used by roster + machines)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT name, gateway_url, role, up_since_at, description, stopped_at, is_staging "
            "FROM machines WHERE paused_at IS NULL ORDER BY name"
        )
        return cur.fetchall()


async def _roster_statuses(
    pool: ConnectionPool,
    db: Database,
    identity_log: IdentityMismatchLog,
    *,
    fresh: bool,
    image: LoadedCommit,
) -> list[MachineStatus]:
    """The roster of every unpaused machine: from the heartbeat liveness pass's
    snapshot, or — when `fresh` — by dialing every runner now."""
    rows = await asyncio.to_thread(_machines_rows_blocking, pool)
    if not rows:
        return []
    found = None if fresh else await asyncio.to_thread(snapshots.read_all_blocking, pool)
    return await gather_cluster_status(
        db, rows, machine_name(), identity_log=identity_log, snapshots=found, image=image
    )


async def _dispatch_op(
    db: Database, target: str, kind: _cluster_rpc.OpKind, payload: dict[str, Any]
) -> dict[str, Any]:
    """POST one op to `target`'s ops server, mapping transport outcomes to HTTP.

    The single path every per-host cluster op takes — the local machine
    included (its ops server is dialed at its registered localhost URL); the
    gateway never runs session/pidfile operations itself.

    Raises:
        HTTPException 503: the target's ops server was unreachable.
        HTTPException 502: the op ran on the target but reported failure
            (e.g. an update already in flight there).
    """
    try:
        return await _cluster_rpc.dispatch_to_machine(
            db, target_machine=target, kind=kind, payload=payload
        )
    except _cluster_rpc.ClusterOpUnreachable as exc:
        raise HTTPException(
            status_code=503,
            detail=f"machine {target!r} ops server unreachable for {kind}: {exc!s}",
        ) from exc
    except _cluster_rpc.ClusterOpFailed as exc:
        raise HTTPException(
            status_code=502,
            detail=f"machine {target!r} {kind} failed: {exc.result!r}",
        ) from exc


@router.post("/api/cluster/stopping", status_code=200)
async def post_cluster_stopping(machine: str, home: str, request: Request) -> dict[str, str]:
    """Record that the (machine, home) unit is shutting down intentionally.

    `ava stop` POSTs this (best-effort) just before local teardown so the
    cluster view distinguishes a deliberate stop from a crash — the live probe
    cannot. Stamps the unit's `stopped_at` and recomputes the composed
    `machines` row; `ava start` clears it. `home` is the stopping unit's
    $AVA_HOME so a co-located peer unit keeps its capability.

    `machine`/`home` are caller-asserted, not verified against the auth
    principal (same trust model as the other cluster control endpoints).
    Low-stakes: stopped_at is cosmetic and a spuriously-stamped live host still
    probes online=True.
    """
    return await asyncio.to_thread(_ops.cluster_stopping_op, request.app.state.db, machine, home)


@router.get("/api/cluster/status")
async def get_cluster_status(request: Request) -> ClusterStatus:
    """This host's own snapshot (name / role / paused).

    On an agent-runner-capable host the snapshot comes from this host's ops
    server (a status_probe op dialed at its registered localhost URL) — the
    gateway never probes sessions/pidfiles itself. A pure gateway runs no ops
    server, so it assembles a lightweight local snapshot (paused flag +
    orchestration session + prod-source HEAD; no shell/daemon probes — that is
    agent-runner data a pure gateway does not have).

    Consumed by `ava status`'s gateway supplement. For the full multi-machine
    roster use `/api/cluster/roster`. Bypasses 503 mode so status stays visible
    during pause — observability is always online.
    """
    return await cluster_status_snapshot(
        request.app.state.db, image=request.app.state.process_image
    )


async def cluster_status_snapshot(db: Database, *, image: LoadedCommit) -> ClusterStatus:
    """The `/api/cluster/status` body, also read by the MCP `cluster_status` tool."""
    if is_agent_runner():
        result = await _dispatch_op(db, machine_name(), "status_probe", {})
        return ClusterStatus.model_validate(result)
    return await asyncio.to_thread(_local_snapshot_blocking, db, image=image)


@router.get("/api/cluster/roster", response_model=list[MachineStatus])
async def get_cluster_roster(
    request: Request,
    *,
    fresh: Annotated[
        bool, Query(description="Dial every runner now instead of reading the last probe.")
    ] = False,
) -> list[MachineStatus]:
    """The full multi-machine roster — every registered machine + status.

    Backs the thin-client `ava cluster status`: the gateway's own row is
    resolved locally; each agent-runner renders from the heartbeat liveness
    pass's last status_probe (`MachineStatus.observed_at` says how old), so the
    read dials nothing and a down host cannot slow it. `fresh=true` probes every
    runner in parallel via the status_probe op instead (total wall ≈ the probe
    timeout regardless of N) — for a caller that must see a restart land, such as
    the fleet update. Same fan-out the `/api/status` cluster panel uses. Bypasses
    503 mode so the roster stays visible during pause.
    """
    return await _roster_statuses(
        request.app.state.control_db_pool,
        request.app.state.db,
        request.app.state.identity_mismatch_log,
        fresh=fresh,
        image=request.app.state.process_image,
    )


# --- Admin ops (token-only ops, ssh-free) -------------------------------------
# Replaces what used to require SSH to the gateway host:
#   - Reading service logs: query the `events` PG table directly. Daemons
#     route stdlib logging through loguru's PG sink (see base/log/__init__.py's
#     `_StdlibInterceptHandler` + `_postgres_sink`), so every INFO+ line from
#     gateway / scheduler / labeler / agent-host / watchdog / memory-
#     indexer lands here. agent processes also write here.
#   - Cleaning up a stale machines row after a host is decommissioned or
#     renamed (e.g. `laminar`→`cloud` 2026-05-25): a one-shot DELETE endpoint
#     replaces hand-running `psql` on the gateway host.
# Both endpoints bypass the paused-host 503 (so they keep working during an `ava
# update` window). The gateway is unauthenticated (the private network is the boundary).


# Protective ceiling for the admin-events limit (the handler's range check);
# the default *window* is display.cluster_events_default_limit
# (task #3696 exception inventory: KEEP).
_EVENTS_MAX_LIMIT = 1000


@router.get("/api/cluster/admin/events", response_model=AgentEventsResponse)
def get_cluster_admin_events(
    request: Request,
    agent_id: int | None = None,
    service_only: bool = False,  # noqa: FBT001, FBT002 — FastAPI query param, always passed by name
    level: str | None = None,
    since: str | None = None,
    event: str | None = None,
    grep: str | None = None,
    limit: int | None = None,
) -> AgentEventsResponse:
    """Slice the unified event stream (category=telemetry/log, read from
    `telemetry_events`) for ops debugging without SSH.

    Filters compose (AND):
      - `agent_id=N`: only this agent's events (gateway / daemon rows excluded).
      - `service_only=true`: only events with no agent_id (gateway / daemons).
      - `level=ERROR`: minimum level (DEBUG/INFO/WARNING/ERROR/CRITICAL).
      - `since=2h` / `since=2026-05-25T00:00Z`: relative window or absolute
        timestamp. Relative format `<int><unit>` with unit `s/m/h/d`.
      - `event=spawn,terminate`: comma-separated event names.
      - `grep=<substring>`: case-insensitive substring match on the event
        name, source and payload (which includes the `msg` text).
      - `limit`: max rows to return, capped at 1000 (protective constant).
        Omitted returns the configured default
        (``display.cluster_events_default_limit`` - 200 out of the box).

    Returns newest-first; the client paginates by passing
    `since=<oldest_ts_seen>` on the next call.
    """
    if limit is None:
        limit = settings.display.cluster_events_default_limit
    if limit < 1 or limit > _EVENTS_MAX_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"limit must be in [1, {_EVENTS_MAX_LIMIT}], got {limit}",
        )

    if agent_id is not None and service_only:
        raise HTTPException(
            status_code=400,
            detail="pass either `agent_id` or `service_only=true`, not both",
        )

    level_min = None
    if level:
        level_upper = level.upper()
        order = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
        if level_upper not in order:
            raise HTTPException(status_code=400, detail=f"unknown level: {level!r}")
        level_min = order[order.index(level_upper) :][0].lower()

    since_dt = _parse_since(since) if since else None
    events = [e.strip() for e in event.split(",") if e.strip()] if event else None

    # A lower bound is always in effect; the default is the last 24 hours.
    window_from = since_dt or datetime.now(UTC) - timedelta(hours=24)
    with request.app.state.db_pool.connection() as conn:
        rows, _ = telemetry_rows.query_events(
            conn,
            agent_id=agent_id,
            service_only=service_only,
            categories=["telemetry", "log"],
            event_names=events,
            level_min=level_min,
            grep=grep,
            from_=window_from,
            limit=limit,
        )
    return AgentEventsResponse(
        items=[
            AgentEventRow(
                id=row["id"],
                line_sha256=row["line_sha256"],
                ts=row["ts"],
                agent_id=row["agent_id"],
                level=row["level"],
                event=row["event_name"],
                payload=row["attributes"],
            )
            for row in rows
        ]
    )


def _parse_since(s: str) -> datetime:
    """Accept either `<int><s|m|h|d>` (relative) or an ISO-8601 timestamp.

    Relative form is treated as "this much time ago"; the float is the
    quantity, the suffix the unit. Absolute form falls back to
    `datetime.fromisoformat` which handles both `Z` suffix and offset
    forms in 3.12.
    """
    s = s.strip()
    if not s:
        raise HTTPException(status_code=400, detail="since= empty")
    unit = s[-1]
    if unit in ("s", "m", "h", "d"):
        try:
            n = float(s[:-1])
        except ValueError:
            logger.debug(
                "Parsing 'since' window as relative duration failed for '{}', falling back to ISO-8601 parse",
                s,
            )
        else:
            multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
            return datetime.now(UTC) - timedelta(seconds=n * multiplier)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"since={s!r} not a relative window (e.g. '2h') or ISO-8601 timestamp",
        ) from exc


@router.get("/api/cluster/machines", response_model=list[AgentMachineRow])
async def get_cluster_machines(
    request: Request,
    *,
    fresh: Annotated[
        bool, Query(description="Dial every runner now instead of reading the last probe.")
    ] = False,
) -> list[AgentMachineRow]:
    """List every registered machine with its description + live status.

    Backs ava.agents.list_machines(). Live status comes from the same source the
    roster uses: the heartbeat liveness pass's last status_probe, or with
    `fresh=true` a probe of every runner now (gateway is live from its own
    perspective). role / gateway_url are intentionally omitted — agents reason
    over the free-text description, not ops topology.
    """
    statuses = await _roster_statuses(
        request.app.state.control_db_pool,
        request.app.state.db,
        request.app.state.identity_mismatch_log,
        fresh=fresh,
        image=request.app.state.process_image,
    )
    # This is the AGENT view: it lists only machines that can run agent processes
    # (carry the agent-runner capability). A gateway-only node is intentionally
    # invisible here; a single-box gateway,agent-runner node shows up because it
    # carries agent-runner. The operator view that shows every node is
    # `/api/status`.
    return [
        AgentMachineRow(
            name=m.name,
            description=m.description,
            # Reached-but-unknown is diagnostic visibility, not a determinate
            # liveness verdict for the SDK/config projection.
            live=m.online and m.paused is not None,
            is_staging=m.is_staging,
        )
        for m in statuses
        if m.serve_agent_runner
    ]


class MachineStagingRequest(BaseModel):
    """Body for POST /api/cluster/machines/{name}/staging."""

    is_staging: bool


@router.post("/api/cluster/machines/{name}/staging", response_model=MachineDeleteResponse)
def set_machine_staging(
    name: str, req: MachineStagingRequest, request: Request
) -> MachineDeleteResponse:
    """Set or clear a machine's operator staging flag (`is_staging`).

    The staging latch is what keeps a registered staging host out of the
    agent-runner target set — `ava start` on it clears its `stopped_at` like any
    host, and this flag is the exclusion (`base.cluster.machines.list_agent_runners`
    skips is_staging rows). Backed by `base.cluster.machines.set_staging`; the CLI
    verbs `ava cluster mark-staging` / `unmark-staging` call this endpoint.
    """
    changed = machines.set_staging(request.app.state.db, name, is_staging=req.is_staging)
    if not changed:
        raise HTTPException(status_code=404, detail=f"no machine named {name!r}")
    return MachineDeleteResponse(deleted=True)


@router.delete("/api/cluster/machines/{name}", response_model=MachineDeleteResponse)
def delete_cluster_machine(name: str, request: Request) -> MachineDeleteResponse:
    """Remove a row from the `machines` table.

    Used to retire a decommissioned agent-runner or clean up after a rename
    (e.g. `laminar`→`cloud`: the new name's `register_self()` INSERTs the
    new row at startup, then ops calls this endpoint to drop the now-stale
    old-name row). Idempotent — calling on a missing name returns
    `deleted=false`.

    Refuses to delete the row corresponding to this gateway's own
    `machine_name()` — the live process needs its registration to remain
    intact (cluster status probe targets it; `register_self` only writes
    on startup, so a runtime DELETE wouldn't be repaired until next
    restart).
    """
    if name == machine_name():
        raise HTTPException(
            status_code=400,
            detail=f"refusing to delete this host's own machines row ({name!r}); "
            "stop the gateway first if you really want to retire it.",
        )
    with write_transaction(request.app.state.control_db_pool) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM machines WHERE name = %s", (name,))
        deleted = cur.rowcount > 0
    return MachineDeleteResponse(deleted=deleted)
