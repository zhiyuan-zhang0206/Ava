"""Cluster control + admin endpoints — /api/cluster/*.

Covers stop / update / status / roster / admin events query / machines
DELETE. These paths are exempt from the paused-host 503 middleware (see
app.py `_PAUSE_BYPASS_PREFIXES`) because they are the recovery tools the
gateway uses during pause.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Body, HTTPException, Request
from loguru import logger
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from gateway import loki_events
from gateway.routers.status import gather_cluster_status
from gateway.schemas import (
    AgentEventRow,
    AgentEventsResponse,
    AgentMachineRow,
    ClusterOpRequest,
    MachineDeleteResponse,
    MachineStatus,
)
from ops import cluster_rpc as _cluster_rpc
from ops import ops_cluster as _ops
from ops.cluster import (
    ClusterStatus,
    ClusterUpdateInProgress,
    NothingToUpdate,
    OrchestrationSpawnFailed,
    UpdateCheck,
    current_orchestration,
)
from ops.cluster import is_paused as cluster_is_paused
from ops.rpc_schemas import ClusterSpawnSession, ClusterTransitionPayload
from shared import machines
from shared.cluster_drift import prod_source_head_sha
from shared.config import settings
from shared.db_transaction import write_transaction
from shared.live_events import ClusterUpdateStarted
from shared.machine import is_agent_runner, is_gateway, is_observability_station, machine_name
from shared.redis_client import publish_best_effort_sync

router = APIRouter()
_log = logging.getLogger(__name__)


# Only the whole-cluster endpoints below emit this hint: the single-host update
# relay and watchdog self-heal paths do not interrupt this gateway, and the
# cluster-status poll remains their fallback signal.
def _publish_cluster_update_started(kind: Literal["rollout", "restart"], origin: str) -> None:
    event = ClusterUpdateStarted(agent_id=0, kind=kind, origin=origin)
    publish_best_effort_sync(
        settings.data_plane.events_channel,
        event.model_dump_json(),
        context="cluster_update_started",
    )


def _local_snapshot_blocking() -> ClusterStatus:
    """Sync local snapshot for a pure gateway (no ops server) — via to_thread:
    paused flag (file), orchestration liveness (session probe) and the
    prod-source HEAD (git rev-parse) are all child-process / disk reads that
    must not run on the event loop."""
    from shared import process_sha as _process_sha

    return ClusterStatus(
        machine_name=machine_name(),
        serve_gateway=is_gateway(),
        serve_agent_runner=is_agent_runner(),
        serve_observability_station=is_observability_station(),
        paused=cluster_is_paused(),
        current_orchestration=current_orchestration(),
        head_sha=prod_source_head_sha(),
        # This gateway process's own frozen commit — not a disk bookmark, so a
        # gateway that outlived a checkout advance reports the old commit and the
        # roster shows the drift.
        running_sha=_process_sha.get(),
    )


def _machines_rows_blocking(pool: ConnectionPool) -> list[tuple[Any, ...]]:
    """Sync machines-table read — via to_thread (used by roster + machines)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT name, gateway_url, role, up_since_at, description, stopped_at, is_staging "
            "FROM machines WHERE paused_at IS NULL ORDER BY name"
        )
        return cur.fetchall()


def _cluster_globals_blocking() -> tuple[Any, Any, Any, Any]:
    """Sync cluster-global markers (pin / deploy lease / last-update / known-good)
    — all small file reads, but grouped under one to_thread so a slow disk never
    stalls the roster fan-out."""
    from gateway.routers.status import (
        _read_cluster_pin,
        _read_deploy_lease,
        _read_known_good,
        _read_last_update,
    )

    return (
        _read_cluster_pin(),
        _read_deploy_lease(),
        _read_last_update(),
        _read_known_good(),
    )


async def _dispatch_op(
    target: str, kind: _cluster_rpc.OpKind, payload: dict[str, Any]
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
            target_machine=target, kind=kind, payload=payload
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


@router.post("/api/cluster/stop", status_code=200)
async def post_cluster_stop(body: ClusterTransitionPayload) -> dict[str, bool]:
    """Phase A handler: drain native agent controls while SDK dependencies
    remain available, then stop local services through cluster_stop.
    """
    await _dispatch_op(machine_name(), "cluster_stop", body.model_dump(mode="json"))
    return {"paused": True}


@router.post("/api/cluster/resume", status_code=200)
async def post_cluster_resume(body: ClusterTransitionPayload) -> dict[str, bool]:
    """Compensating unpause: restore posture and release native admission holds,
    executed by this host's ops server via a cluster_resume op.

    Symmetric inverse of `/api/cluster/stop`. The orchestration's failure path
    fans this out (by dialing each host's ops server) to every host it had paused.
    Operators recover a stranded host through `/api/cluster/recover`; this route
    requires the opaque exact capability of the deploy that created the pause.
    """
    await _dispatch_op(machine_name(), "cluster_resume", body.model_dump(mode="json"))
    return {"paused": False}


@router.post("/api/cluster/recover", status_code=200)
async def post_cluster_recover() -> dict[str, Any]:
    """Operator stranded-cluster recovery — force-clear a pause + update lock left
    behind by a hard-killed rollout, so the UI/SDK unblock and the next rollout can
    run without waiting out the lock TTL.

    Bypasses the cluster-paused 503 middleware (`/api/cluster/*`), so it is callable
    exactly when the cluster is wedged paused. Refuses (409) if an orchestration is
    actually in flight on this host — recovery is only for the no-session strand.

    Returns {"unlocked_holder": <prior lock holder or None>}.
    """
    try:
        return await asyncio.to_thread(_ops.cluster_recover_op)
    except ClusterUpdateInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OrchestrationSpawnFailed as exc:
        raise HTTPException(
            status_code=503, detail=f"could not start the orchestration session: {exc}"
        ) from exc


@router.post("/api/cluster/stopping", status_code=200)
async def post_cluster_stopping(machine: str, home: str) -> dict[str, str]:
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
    return await asyncio.to_thread(_ops.cluster_stopping_op, machine, home)


@router.post("/api/cluster/update", status_code=202)
async def post_cluster_update(
    target: str | None = None, target_sha: str | None = None
) -> dict[str, str]:
    """Trigger an update.

    `target` selects which machine to update (omitted means this host). Either
    way the op POSTs to the target's ops server, which calls cluster_update_op
    in-process there — spawning a detached updater and returning quickly. This
    host is no special case: its own ops server is dialed at its registered
    localhost URL.

    `target_sha` pins the force-checkout commit (the watchdog off-pin self-heal
    passes the cluster pin so the host converges to exactly it, not the moving
    origin/main tip); threaded into the op payload.

    Returns the orchestration session name + tee'd log path.

    503 when the target's ops server is unreachable; 502 when the op ran but
    reported failure (e.g. an updater session already in flight there — the
    caller waits for paused=false then retries).
    """
    target_machine = target if target is not None else machine_name()
    result = await _dispatch_op(
        target_machine,
        "cluster_update",
        {"target_sha": target_sha} if target_sha is not None else {},
    )
    # Validate the wire result at the boundary, then return the same {session, log}
    # shape the frontend already consumes (no new named response model — the
    # endpoint's dict[str, str] contract is unchanged).
    return ClusterSpawnSession.model_validate(result).model_dump()


@router.post("/api/cluster/rollout", status_code=202)
async def post_cluster_rollout(
    body: ClusterOpRequest = Body(default_factory=ClusterOpRequest),  # noqa: B008 — FastAPI's Body() must appear in the signature
) -> dict[str, str | bool]:
    """Launch the whole-cluster `ava cluster update` rollout, detached.

    Gateway only. Spawns a detached session running the full
    orchestration — Phase A pauses every agent-runner, the gateway stops /
    pulls / syncs / migrates, Phase B fans out the agent-runner self-updates,
    then polls each host back to healthy. Returns 202 immediately; clients
    poll `GET /api/cluster/status` per host to observe progress.

    Body is fully optional; `origin` (default "user") names the trigger and
    heads the rollout log + the cluster pin's `updated_by`.

    Returns the orchestration session name + tee'd log path, plus
    `backend_changed` (whether this rollout restarts agent processes) and
    `needs_replay` (whether the installed commit is ahead of the running
    bookmark). The frontend uses the first to describe agent restarts; the CLI
    uses the second to identify a half-deployed state. The SDK initiator that
    once waited on the restart signal, `ava.self.update()`, was removed 2026-08.

    Errors:
    - 400 if called on an agent-runner (rollout is a gateway operation).
    - 409 if a rollout / update is already in flight.
    - 422 if the cluster is already up to date (behind==0 and no replay is
      needed) — nothing to roll out, so the fleet is not bounced. Use
      /api/cluster/restart to bounce on the current code.
    - 503 if the session backend could not start the orchestration session.
    """
    if not is_gateway():
        raise HTTPException(
            status_code=400,
            detail="rollout must be triggered on the gateway",
        )
    try:
        result = await asyncio.to_thread(
            _ops.cluster_rollout_op,
            body.origin,
            mode=body.mode,
            force=body.force,
            dry_run=body.dry_run,
        )
        if not body.dry_run:
            _publish_cluster_update_started("rollout", body.origin)
        return result
    except ClusterUpdateInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NothingToUpdate as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OrchestrationSpawnFailed as exc:
        raise HTTPException(
            status_code=503, detail=f"could not start the orchestration session: {exc}"
        ) from exc


@router.post("/api/cluster/restart", status_code=202)
async def post_cluster_restart(
    body: ClusterOpRequest = Body(default_factory=ClusterOpRequest),  # noqa: B008 — FastAPI's Body() must appear in the signature
) -> dict[str, str]:
    """Launch a whole-cluster restart (no pull), detached.

    Gateway only. Same three-phase orchestration as rollout — pause every
    agent-runner, gracefully quiesce agents, bounce this host, fan out the
    agent-runner bounces — but skips git pull / uv sync / migration. Use it to
    apply config changes (or unwedge a service) without changing the checked-out
    code. Returns 202 immediately; clients poll `GET /api/cluster/status`.

    Body is fully optional; `origin` (default "user") names the trigger and
    heads the restart log.

    Returns the orchestration session name + tee'd log path.

    Errors:
    - 400 if called on an agent-runner (restart is a gateway operation).
    - 409 if a restart / rollout / update is already in flight.
    - 503 if the session backend could not start the orchestration session.
    """
    if not is_gateway():
        raise HTTPException(
            status_code=400,
            detail="restart must be triggered on the gateway",
        )
    try:
        result = await asyncio.to_thread(_ops.cluster_restart_op, body.origin, mode=body.mode)
        _publish_cluster_update_started("restart", body.origin)
        return result
    except ClusterUpdateInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OrchestrationSpawnFailed as exc:
        raise HTTPException(
            status_code=503, detail=f"could not start the orchestration session: {exc}"
        ) from exc


@router.get("/api/cluster/update-check")
async def get_cluster_update_check() -> UpdateCheck:
    """Read-only preflight for the Update button — how far behind origin/main and
    what a rollout would restart, or whether an interrupted rollout needs replay.

    Gateway only (it inspects the gateway checkout). Does a
    `git fetch` but never pulls or mutates the tree, so the UI can poll it.
    A clean `behind == 0` → the UI shows "no updates" and does not launch a
    rollout. An installed commit ahead of the running bookmark is instead
    returned as `needs_replay`.
    """
    if not is_gateway():
        raise HTTPException(
            status_code=400,
            detail="update-check must be queried on the gateway",
        )
    # update_check() shells out to `git fetch` (bounded by _GIT_TIMEOUT_S in
    # ops/update_check.py) — off the event loop so a slow GitHub dial cannot
    # stall the gateway (the frontend polls this endpoint every 30s).
    return await asyncio.to_thread(_ops.cluster_update_check_op)


@router.get("/api/cluster/status")
async def get_cluster_status() -> ClusterStatus:
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
    if is_agent_runner():
        result = await _dispatch_op(machine_name(), "status_probe", {})
        return ClusterStatus.model_validate(result)
    return await asyncio.to_thread(_local_snapshot_blocking)


@router.get("/api/cluster/roster", response_model=list[MachineStatus])
async def get_cluster_roster(request: Request) -> list[MachineStatus]:
    """The full multi-machine roster — every registered machine + live status.

    Backs the thin-client `ava cluster status`: the gateway's own row is
    resolved locally; each agent-runner is probed in parallel via the
    status_probe op (total wall ≈ the probe timeout regardless
    of N). Same fan-out the `/api/status` cluster panel uses. Bypasses 503 mode
    so the roster stays visible during pause.
    """
    rows = await asyncio.to_thread(_machines_rows_blocking, request.app.state.control_db_pool)
    if not rows:
        return []

    # Pin, lease and last-update outcome are all cluster-global: read once here,
    # stamped per row by the fan-out. The lease is what makes a refused deploy
    # explainable from the roster (`hold` column + its banner) instead of only from
    # the cron log; the last-update record is what makes a FAILED one explainable
    # without reading a pin/head mismatch as a riddle (#1012).
    cluster_target_sha, deploy_lease, last_update, last_known_good_sha = await asyncio.to_thread(
        _cluster_globals_blocking
    )
    return await gather_cluster_status(
        rows,
        machine_name(),
        cluster_target_sha=cluster_target_sha,
        deploy_lease=deploy_lease,
        last_update=last_update,
        last_known_good_sha=last_known_good_sha,
    )


# --- Admin ops (token-only ops, ssh-free) -------------------------------------
# Replaces what used to require SSH to the gateway host:
#   - Reading service logs: query the `events` PG table directly. Daemons
#     route stdlib logging through loguru's PG sink (see shared/log.py's
#     `_StdlibInterceptHandler` + `_postgres_sink`), so every INFO+ line from
#     gateway / scheduler / labeler / agent-host / watchdog / memory-
#     indexer lands here. agent processes also write here.
#   - Cleaning up a stale machines row after a host is decommissioned or
#     renamed (e.g. `laminar`→`cloud` 2026-05-25): a one-shot DELETE endpoint
#     replaces hand-running `psql` on the gateway host.
# Both endpoints bypass the paused-host 503 (so they keep working during an `ava
# update` window). The gateway is unauthenticated (the private network is the boundary).


_EVENTS_MAX_LIMIT = 1000


@router.get("/api/cluster/admin/events", response_model=AgentEventsResponse)
def get_cluster_admin_events(
    agent_id: int | None = None,
    service_only: bool = False,  # noqa: FBT001, FBT002 — FastAPI query param, always passed by name
    level: str | None = None,
    since: str | None = None,
    event: str | None = None,
    grep: str | None = None,
    limit: int = 200,
) -> AgentEventsResponse:
    """Slice the unified event stream from Loki (category=telemetry/log) for
    ops debugging without SSH — the LGTM replacement for the PG `events` read
    (task #1197).

    Filters compose (AND):
      - `agent_id=N`: only this agent's events (gateway / daemon rows excluded).
      - `service_only=true`: only events with no agent_id (gateway / daemons).
      - `level=ERROR`: minimum level (DEBUG/INFO/WARNING/ERROR/CRITICAL).
      - `since=2h` / `since=2026-05-25T00:00Z`: relative window or absolute
        timestamp. Relative format `<int><unit>` with unit `s/m/h/d`.
      - `event=spawn,terminate`: comma-separated event names.
      - `grep=<substring>`: substring match on the raw log line (the JSON
        body includes the `msg` payload).
      - `limit`: max rows to return, capped at 1000. Default 200.

    Returns newest-first; the client paginates by passing
    `since=<oldest_ts_seen>` on the next call.
    """
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

    rows, _ = loki_events.query_events(
        agent_id=agent_id,
        service_only=service_only,
        categories=["telemetry", "log"],
        event_names=events,
        level_min=level_min,
        grep=grep,
        from_=since_dt,
        limit=limit,
    )
    return AgentEventsResponse(
        items=[
            AgentEventRow(
                id=row["id"],
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
async def get_cluster_machines(request: Request) -> list[AgentMachineRow]:
    """List every registered machine with its description + live status.

    Backs ava.agents.list_machines(). Live status comes from the same
    status_probe op round-trip the status panel uses
    (gateway is live from its own perspective); total wall time is
    bounded by the probe timeout regardless of machine count. role /
    gateway_url are intentionally omitted — agents reason over the free-text
    description, not ops topology.
    """
    rows = await asyncio.to_thread(_machines_rows_blocking, request.app.state.control_db_pool)
    if not rows:
        return []
    statuses = await gather_cluster_status(rows, machine_name())
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
def set_machine_staging(name: str, req: MachineStagingRequest) -> MachineDeleteResponse:
    """Set or clear a machine's operator staging flag (`is_staging`).

    The staging latch is what keeps a registered staging host out of the
    rollout target set — `ava start` on it clears its `stopped_at` like any
    host, and this flag is the exclusion (`shared.machines.list_agent_runners`
    skips is_staging rows). Backed by `shared.machines.set_staging`; the CLI
    verbs `ava cluster mark-staging` / `unmark-staging` call this endpoint.
    """
    changed = machines.set_staging(name, is_staging=req.is_staging)
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
