"""Pure cross-machine inventory matrix contracts."""

from typing import Any

from gateway.extensions.inventory_matrix import collapse_inventory
from ops.rpc_schemas import InventoryReadResult


def _read(
    plugins: dict[str, Any], mcp_servers: dict[str, Any], machine: str
) -> InventoryReadResult:
    """Build a canned inventory_read result for one host (the model the per-host
    dispatch now returns; the plugin/MCP item dicts are coerced to InventoryReadItem)."""
    return InventoryReadResult(machine=machine, plugins=plugins, mcp_servers=mcp_servers)


def _plugin(*, enabled: bool, description: str = "") -> dict[str, Any]:
    return {"enabled": enabled, "can_enable": None, "reason": None, "description": description}


def _mcp(
    *, enabled: bool, can_enable: bool | None, reason: str | None, description: str = ""
) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "can_enable": can_enable,
        "reason": reason,
        "description": description,
    }


# Matrix collapse contracts


def test_collapse_present_and_absent_cells() -> None:
    """A plugin on machine A but not machine B -> A cell present+enabled, B cell
    present=False; description is the first non-empty across hosts."""
    reads = {
        "A": _read({"X": _plugin(enabled=True, description="plugin X")}, {}, "A"),
        "B": _read({}, {}, "B"),
    }
    agg = collapse_inventory(reads, machines=["A", "B"], unreachable=[])

    assert agg.machines == ["A", "B"]
    assert agg.unreachable == []
    rows = {r.name: r for r in agg.plugins}
    x = rows["X"]
    assert x.kind == "plugin"
    assert x.description == "plugin X"
    assert x.hosts["A"].present is True
    assert x.hosts["A"].enabled is True
    assert x.hosts["B"].present is False
    assert x.hosts["B"].enabled is False


def test_collapse_excludes_unreachable_from_cells() -> None:
    """An unreachable machine appears in `machines` + `unreachable` but in no
    item's `hosts` (it has no read to collapse)."""
    reads = {"A": _read({"X": _plugin(enabled=True)}, {}, "A")}
    agg = collapse_inventory(reads, machines=["A", "B"], unreachable=["B"])

    assert agg.machines == ["A", "B"]
    assert agg.unreachable == ["B"]
    x = {r.name: r for r in agg.plugins}["X"]
    assert "B" not in x.hosts
    assert set(x.hosts) == {"A"}


def test_collapse_mcp_carries_capability_verdict() -> None:
    """MCP rows carry kind='mcp' and the host's can_enable/reason verdict."""
    reads = {
        "A": _read({}, {"srv": _mcp(enabled=False, can_enable=False, reason="needs token")}, "A"),
    }
    agg = collapse_inventory(reads, machines=["A"], unreachable=[])
    srv = {r.name: r for r in agg.mcp_servers}["srv"]
    assert srv.kind == "mcp"
    assert srv.hosts["A"].enabled is False
    assert srv.hosts["A"].can_enable is False
    assert srv.hosts["A"].reason == "needs token"
