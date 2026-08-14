"""Cross-machine plugin + MCP enable/disable panel — /api/inventory GET/PUT.

Inventory (plugin + MCP enable state) is an AGENT-HOST-ONLY surface: a
gateway runs no agent process, so it loads no plugins and connects to no
MCP servers, and must never appear here — not as an aggregate column, not as a
`?machine=` target. Every read/write is POSTed to a remote agent-runner's
ops server (which executes it in-process).

`?machine=` selects the host viewed/written:
- GET without `machine` -> the cross-machine AGGREGATE: fan out an inventory_read
  to every registered agent-runner concurrently, collapse into a per-item matrix;
  a host that times out / fails lands in `unreachable`, not in any item's cells.
- GET with `machine` -> that one host's plugins + MCP servers (404 unknown /
  non-agent-runner machine, 503 offline / timed out).
- PUT writes toggles to `machine` (required; must be an agent-runner).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from gateway.schemas import (
    InventoryAggregate,
    InventoryItem,
    InventoryItemAggregate,
    InventoryItemHostState,
    InventoryItemWriteResult,
    InventoryMachineView,
    InventoryWriteResult,
)
from ops import cluster_rpc as _cluster_rpc
from ops.rpc_schemas import InventoryReadItem, InventoryReadResult, InventoryWriteOpResult

router = APIRouter()
_log = logging.getLogger(__name__)

# Bounded per-host wait for a remote inventory_read in the aggregate fan-out, so
# one offline host doesn't stall the whole matrix on the default deadline.
_AGGREGATE_READ_TIMEOUT_S = 15.0


def _assert_inventory_target(target: str) -> None:
    """404 unless `target` is a registered agent-runner.

    Inventory is agent-runner-only: a gateway name — including this gateway's
    own — has no plugin/MCP inventory and is rejected the same as a typo'd one. A
    registered-but-offline agent-runner passes here and 503s later on timeout.
    """
    from gateway.app import app

    with app.state.db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM machines WHERE name = %s AND 'agent-runner' = ANY(role)", (target,)
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"unknown agent-runner: {target!r}")


async def _dispatch_inventory_read(
    target: str, *, timeout_s: float | None = None
) -> InventoryReadResult:
    """Run inventory_read on agent-runner `target` by POSTing to its ops server.

    Targets are always remote agent-runners (the gateway is rejected upstream
    by `_assert_inventory_target`); the agent-runner's ops server executes
    inventory_read_op in-process. The wire dict is validated into an
    InventoryReadResult before it is returned.

    Does NOT convert the remote failure into an HTTPException: the aggregate
    catches the raw ClusterOpUnreachable/ClusterOpFailed per host (to bucket it
    into `unreachable`), and the single-machine GET converts it to 503 at its
    own call site.
    """
    return InventoryReadResult.model_validate(
        await _cluster_rpc.dispatch_to_machine(
            target_machine=target,
            kind="inventory_read",
            payload={},
            timeout_s=timeout_s,
        )
    )


async def _dispatch_inventory_write(
    target: str, plugins: dict[str, bool], mcp_servers: dict[str, bool]
) -> InventoryWriteOpResult:
    """Run inventory_write on agent-runner `target` by POSTing to its ops server.

    Caller has already verified the target is a registered agent-runner."""
    try:
        wire = await _cluster_rpc.dispatch_to_machine(
            target_machine=target,
            kind="inventory_write",
            payload={"plugins": plugins, "mcp_servers": mcp_servers},
        )
    except (_cluster_rpc.ClusterOpUnreachable, _cluster_rpc.ClusterOpFailed) as exc:
        raise HTTPException(
            status_code=503,
            detail=f"machine {target!r} unreachable or failed the inventory request: {exc!s}",
        ) from exc
    return InventoryWriteOpResult.model_validate(wire)


def _collapse(
    reads: dict[str, InventoryReadResult],
    machines: list[str],
    unreachable: list[str],
) -> InventoryAggregate:
    """Collapse per-host inventory_read results into the cross-machine matrix.

    `reads` maps each REACHABLE machine name to its InventoryReadResult.
    `machines` is every name considered (column set), `unreachable` the subset
    whose read failed.

    For each item, the row's `description` is the first non-empty description
    seen across reachable hosts; a reachable host lacking the item gets a
    present=False cell. Unreachable hosts are excluded from every item's cells.
    """
    reachable = sorted(reads)

    def _rows(
        items_of: Callable[[InventoryReadResult], dict[str, InventoryReadItem]], kind: str
    ) -> list[InventoryItemAggregate]:
        names = sorted({name for m in reachable for name in items_of(reads[m])})
        rows: list[InventoryItemAggregate] = []
        for name in names:
            description = ""
            hosts: dict[str, InventoryItemHostState] = {}
            for m in reachable:
                item = items_of(reads[m]).get(name)
                if item is None:
                    hosts[m] = InventoryItemHostState(present=False, enabled=False)
                    continue
                if not description and item.description:
                    description = item.description
                hosts[m] = InventoryItemHostState(
                    present=True,
                    enabled=item.enabled,
                    can_enable=item.can_enable,
                    reason=item.reason,
                )
            rows.append(
                InventoryItemAggregate(name=name, kind=kind, description=description, hosts=hosts)
            )
        return rows

    return InventoryAggregate(
        machines=sorted(machines),
        unreachable=sorted(unreachable),
        plugins=_rows(lambda r: r.plugins, "plugin"),
        mcp_servers=_rows(lambda r: r.mcp_servers, "mcp"),
    )


def _agent_runner_names() -> list[str]:
    """Agent-runner machine names — the aggregate's column set.

    The gateway this gateway runs on is excluded by the role filter even
    though it is the local machine: inventory is agent-runner-only. An agent-runner
    that has not finished its startup UPSERT simply doesn't appear until it
    registers.
    """
    from gateway.app import app

    with app.state.db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT name FROM machines WHERE 'agent-runner' = ANY(role) ORDER BY name")
        return [row[0] for row in cur.fetchall()]


async def _aggregate() -> InventoryAggregate:
    """Fan out inventory_read to every agent-runner concurrently and collapse the
    results; a host whose read raised goes in `unreachable`."""
    machines = _agent_runner_names()
    results = await asyncio.gather(
        *(_dispatch_inventory_read(m, timeout_s=_AGGREGATE_READ_TIMEOUT_S) for m in machines),
        return_exceptions=True,
    )
    reads: dict[str, InventoryReadResult] = {}
    unreachable: list[str] = []
    for m, res in zip(machines, results, strict=True):
        if isinstance(res, (_cluster_rpc.ClusterOpUnreachable, _cluster_rpc.ClusterOpFailed)):
            # Genuine "host couldn't serve its inventory" — bucket it.
            _log.warning("inventory_read on %s failed: %s", m, res)
            unreachable.append(m)
        elif isinstance(res, BaseException):
            # An unexpected exception (a bug in the dispatch helper, a
            # CancelledError) — do NOT relabel it as infrastructure flakiness.
            raise res
        else:
            reads[m] = res
    return _collapse(reads, machines, unreachable)


@router.get("/api/inventory")
async def get_inventory(machine: str | None = None) -> InventoryAggregate | InventoryMachineView:
    """Return the plugin + MCP enable inventory.

    Without `machine`: the cross-machine AGGREGATE matrix (one row per plugin /
    MCP server, one cell per reachable agent-runner; failed hosts in `unreachable`).
    With `machine`: that one agent-runner's flat plugin + MCP lists (404 unknown /
    non-agent-runner machine, 503 offline / timed out).
    """
    if machine is None:
        return await _aggregate()

    await asyncio.to_thread(_assert_inventory_target, machine)
    try:
        read = await _dispatch_inventory_read(machine)
    except (_cluster_rpc.ClusterOpUnreachable, _cluster_rpc.ClusterOpFailed) as exc:
        raise HTTPException(
            status_code=503,
            detail=f"machine {machine!r} unreachable or failed the inventory request: {exc!s}",
        ) from exc

    plugins = [
        InventoryItem(
            name=name,
            kind="plugin",
            enabled=item.enabled,
            can_enable=item.can_enable,
            reason=item.reason,
            description=item.description,
        )
        for name, item in sorted(read.plugins.items())
    ]
    mcp_servers = [
        InventoryItem(
            name=name,
            kind="mcp",
            enabled=item.enabled,
            can_enable=item.can_enable,
            reason=item.reason,
            description=item.description,
        )
        for name, item in sorted(read.mcp_servers.items())
    ]
    return InventoryMachineView(machine=read.machine, plugins=plugins, mcp_servers=mcp_servers)


class InventoryWriteRequest(BaseModel):
    """PUT /api/inventory body. Each half is genuinely optional (the frontend may
    toggle only a plugin or only an MCP server), so a missing half defaults to
    empty — not the contract-required-field fallback CLAUDE.md forbids. FastAPI
    422s a present-but-non-object half before the handler runs."""

    plugins: dict[str, bool] = {}
    mcp_servers: dict[str, bool] = {}


@router.put("/api/inventory")
async def put_inventory(
    body: InventoryWriteRequest, machine: str | None = None
) -> InventoryWriteResult:
    """Apply plugin + MCP enable toggles to agent-runner `machine` (required).

    `machine` is required: inventory lives only on agent-runners, so there is no
    gateway default to write to (400 if absent, 404 if not an agent-runner).
    Body shape `{"plugins": {name: bool}, "mcp_servers": {name: bool}}` — the
    request model validates each half's shape (422 on a non-object half). The
    host-side op is the authoritative per-item gate; the result surfaces its
    verdicts + atomic `applied` flag.
    """
    if machine is None:
        raise HTTPException(
            status_code=400, detail="inventory writes require ?machine=<agent-runner>"
        )
    await asyncio.to_thread(_assert_inventory_target, machine)

    result = await _dispatch_inventory_write(machine, body.plugins, body.mcp_servers)
    return InventoryWriteResult(
        applied=result.applied,
        plugin_results={
            name: InventoryItemWriteResult(ok=r.ok, reason=r.reason)
            for name, r in result.plugin_results.items()
        },
        mcp_results={
            name: InventoryItemWriteResult(ok=r.ok, reason=r.reason)
            for name, r in result.mcp_results.items()
        },
    )
