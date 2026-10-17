---
type: doc
title: Agent-runner inventory
description: Request-owned SQL and remote RPC with a pure cross-machine matrix.
tags:
- gateway
- inventory
---

# Agent-runner inventory

`gateway/extensions/inventory.py` owns GET/PUT `/api/inventory`. The serving
request app supplies the database and pool explicitly to target validation,
roster/snapshot reads and remote RPC. These helpers do not import the assembled
`gateway.app`; app registration and lifespan remain the resource composition.

Inventory is agent-runner-only. A single-machine GET or PUT rejects unknown or
gateway-only machines with 404 before RPC. PUT requires `machine` (400 when
absent) and preserves the host's per-item verdict and atomic `applied` result.
Recognized remote transport/operation failures become 503 for single-machine
requests. Unexpected failures remain visible.

The aggregate reads registered runners and the existing heartbeat snapshots.
Intentionally stopped or known-down hosts stay columns and are marked
unreachable without dialing. Other runners fan out concurrently under the same
15-second total budget and one fast retry. Recognized remote failures become
unreachable hosts; unexpected exceptions are re-raised.

`gateway/extensions/inventory_matrix.py:collapse_inventory` owns only the pure
collapse of validated reads. It sorts columns and item rows, uses the first
non-empty description in sorted reachable-host order, emits absent cells on
reachable hosts lacking an item, excludes unreachable hosts from item cells,
and retains MCP capability verdicts. Its tests import the matrix owner and wire
models directly. HTTP contracts continue to exercise the assembled app, real
machines SQL and validated RPC results, stubbing the public transport boundary.
