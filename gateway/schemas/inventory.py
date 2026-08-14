"""cross-machine plugin + MCP enable matrix.

Split out of the former monolithic ops/schemas.py; FastAPI registers these
unchanged, so the OpenAPI codegen is byte-identical to the wire before.
"""

from pydantic import (
    BaseModel,
)


class InventoryItemHostState(BaseModel):
    """One plugin/MCP server's state on ONE host, in the cross-machine aggregate.

    `present` is False when the host doesn't have this plugin/server at all
    (installed elsewhere but not here) — the cell renders "not installed" and
    offers no toggle. When present, `enabled` is the effective state and
    can_enable/reason are the host's read-time capability verdict (None = no gate).
    """

    present: bool
    enabled: bool
    can_enable: bool | None = None
    reason: str | None = None


class InventoryItemAggregate(BaseModel):
    """One plugin or MCP server, collapsed across every reachable host.

    `kind` is "plugin" | "mcp". `hosts` maps each reachable machine name to its
    per-host state (a machine lacking this item -> present=False).
    """

    name: str
    kind: str  # "plugin" | "mcp"
    description: str
    hosts: dict[str, InventoryItemHostState]


class InventoryAggregate(BaseModel):
    """GET /api/inventory response — the cross-machine plugin + MCP matrix.

    `machines` is every machine name considered (the column set). `unreachable`
    is the subset whose inventory read timed out / failed (their cells are
    unknown). `plugins` / `mcp_servers` are the collapsed rows.
    """

    machines: list[str]
    unreachable: list[str]
    plugins: list[InventoryItemAggregate]
    mcp_servers: list[InventoryItemAggregate]


class InventoryItem(BaseModel):
    """One plugin/MCP server on a SINGLE host (the ?machine= view)."""

    name: str
    kind: str  # "plugin" | "mcp"
    enabled: bool
    can_enable: bool | None = None
    reason: str | None = None
    description: str


class InventoryMachineView(BaseModel):
    """GET /api/inventory?machine= response — one host's plugins + MCP servers."""

    machine: str
    plugins: list[InventoryItem]
    mcp_servers: list[InventoryItem]


class InventoryItemWriteResult(BaseModel):
    """Per-item verdict of a PUT /api/inventory write (`ok` + reason when rejected)."""

    ok: bool
    reason: str | None = None


class InventoryWriteResult(BaseModel):
    """PUT /api/inventory response — per-item results + atomic `applied` flag."""

    applied: bool
    plugin_results: dict[str, InventoryItemWriteResult]
    mcp_results: dict[str, InventoryItemWriteResult]
