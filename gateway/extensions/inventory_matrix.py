"""Collapse validated host inventory into the cross-machine item matrix."""

from collections.abc import Callable

from gateway.extensions.schemas import (
    InventoryAggregate,
    InventoryItemAggregate,
    InventoryItemHostState,
)
from ops.rpc_schemas import InventoryReadItem, InventoryReadResult


def collapse_inventory(
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
