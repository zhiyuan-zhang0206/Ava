"""GET/PUT /api/inventory router tests — the cross-machine plugin + MCP matrix.

The aggregate fans out an inventory_read to every agent-runner, collapses the
results into a per-item matrix, and buckets a host whose read raised into
`unreachable`. These tests stub the per-host dispatch (`_dispatch_inventory_read`
/ `_dispatch_inventory_write`) so they stay deterministic without a real remote
runner; the pure `_collapse` helper is also unit-tested directly.

Inventory is agent-runner-only: a gateway row is never a column and is
rejected as a `?machine=` target. `?machine=` selects a single agent-runner's
flat view; an unknown / non-agent-runner name 404s before any dispatch.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.routers import inventory as inventory_router
from ops import cluster_rpc as _cluster_rpc
from ops.rpc_schemas import FieldWriteResult, InventoryReadResult, InventoryWriteOpResult
from shared.config import settings


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


# ── _collapse pure helper ──


def test_collapse_present_and_absent_cells() -> None:
    """A plugin on machine A but not machine B -> A cell present+enabled, B cell
    present=False; description is the first non-empty across hosts."""
    reads = {
        "A": _read({"X": _plugin(enabled=True, description="plugin X")}, {}, "A"),
        "B": _read({}, {}, "B"),
    }
    agg = inventory_router._collapse(reads, machines=["A", "B"], unreachable=[])

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
    agg = inventory_router._collapse(reads, machines=["A", "B"], unreachable=["B"])

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
    agg = inventory_router._collapse(reads, machines=["A"], unreachable=[])
    srv = {r.name: r for r in agg.mcp_servers}["srv"]
    assert srv.kind == "mcp"
    assert srv.hosts["A"].enabled is False
    assert srv.hosts["A"].can_enable is False
    assert srv.hosts["A"].reason == "needs token"


# ── machines-table seeding helper ──


def _seed_machine(name: str, *, role: str = "agent-runner") -> None:
    """Insert a minimal machine row so `_assert_inventory_target` / the aggregate
    fan-out find it. Defaults to agent-runner (the inventory-eligible role); pass
    role='gateway' to seed a row that inventory must ignore. The per-test
    TRUNCATE (conftest) clears it again."""
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO machines (name, gateway_url, role) VALUES (%s, %s, %s) "
            "ON CONFLICT (name) DO NOTHING",
            (name, "http://remote.invalid:8000", [role]),  # machines.role is TEXT[]
        )
        conn.commit()


REMOTE = "remote-host"


# ── GET aggregate ──


def test_aggregate_collapse_across_two_machines(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/inventory (no machine): two machines, A has plugin X enabled, B
    lacks X -> X row has hosts[A].present True+enabled, hosts[B].present False."""
    monkeypatch.setattr(inventory_router, "_agent_runner_names", lambda: ["A", "B"])

    async def fake_read(target: str, *, timeout_s: float = 0.0) -> InventoryReadResult:
        if target == "A":
            return _read({"X": _plugin(enabled=True, description="plugin X")}, {}, "A")
        return _read({}, {}, "B")

    monkeypatch.setattr(inventory_router, "_dispatch_inventory_read", fake_read)

    with TestClient(app) as client:
        resp = client.get("/api/inventory")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["machines"] == ["A", "B"]
    assert body["unreachable"] == []
    x = {r["name"]: r for r in body["plugins"]}["X"]
    assert x["hosts"]["A"]["present"] is True
    assert x["hosts"]["A"]["enabled"] is True
    assert x["hosts"]["B"]["present"] is False


def test_aggregate_buckets_unreachable_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine whose inventory_read raises ClusterOpUnreachable -> in `unreachable`,
    excluded from every item's `hosts`."""
    monkeypatch.setattr(inventory_router, "_agent_runner_names", lambda: ["A", "B"])

    async def fake_read(target: str, *, timeout_s: float = 0.0) -> InventoryReadResult:
        if target == "A":
            return _read({"X": _plugin(enabled=True)}, {}, "A")
        raise _cluster_rpc.ClusterOpUnreachable("no ack")

    monkeypatch.setattr(inventory_router, "_dispatch_inventory_read", fake_read)

    with TestClient(app) as client:
        resp = client.get("/api/inventory")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["unreachable"] == ["B"]
    x = {r["name"]: r for r in body["plugins"]}["X"]
    assert "B" not in x["hosts"]
    assert set(x["hosts"]) == {"A"}


# ── GET single machine ──


def test_get_single_machine_view(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET ?machine=<remote>: the op's plugin/MCP dicts become sorted flat lists."""
    _seed_machine(REMOTE)

    async def fake_read(target: str, *, timeout_s: float = 0.0) -> InventoryReadResult:
        return _read(
            {"b_plug": _plugin(enabled=True), "a_plug": _plugin(enabled=False)},
            {"srv": _mcp(enabled=True, can_enable=True, reason=None)},
            REMOTE,
        )

    monkeypatch.setattr(inventory_router, "_dispatch_inventory_read", fake_read)

    with TestClient(app) as client:
        resp = client.get(f"/api/inventory?machine={REMOTE}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["machine"] == REMOTE
    assert [p["name"] for p in body["plugins"]] == ["a_plug", "b_plug"]
    assert body["plugins"][0]["kind"] == "plugin"
    assert body["mcp_servers"][0]["name"] == "srv"
    assert body["mcp_servers"][0]["kind"] == "mcp"


def test_get_unknown_machine_404() -> None:
    """GET ?machine=<unknown> (no row in machines) -> 404 before any dispatch."""
    with TestClient(app) as client:
        resp = client.get("/api/inventory?machine=ghost-host")
    assert resp.status_code == 404, resp.text
    assert "unknown agent-runner" in resp.json()["detail"]


def test_get_gateway_machine_404() -> None:
    """GET ?machine=<gateway>: a registered gateway row is not an
    inventory target -> 404, never reaching dispatch. Guards the leak where the
    gateway's own host showed plugins/MCP."""
    _seed_machine("gw-host", role="gateway")
    with TestClient(app) as client:
        resp = client.get("/api/inventory?machine=gw-host")
    assert resp.status_code == 404, resp.text
    assert "unknown agent-runner" in resp.json()["detail"]


def test_aggregate_excludes_gateway_column(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/inventory exercises the real `_agent_runner_names` SQL: a seeded
    gateway row is never a column; only the agent-runner fans out."""
    _seed_machine(REMOTE, role="agent-runner")
    _seed_machine("gw-host", role="gateway")

    async def fake_read(target: str, *, timeout_s: float = 0.0) -> InventoryReadResult:
        assert target == REMOTE  # the gateway must never be dispatched to
        return _read({"X": _plugin(enabled=True)}, {}, REMOTE)

    monkeypatch.setattr(inventory_router, "_dispatch_inventory_read", fake_read)

    with TestClient(app) as client:
        resp = client.get("/api/inventory")
    assert resp.status_code == 200, resp.text
    assert resp.json()["machines"] == [REMOTE]


def test_get_single_machine_offline_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET ?machine=<known>: the inventory_read times out -> 503."""
    _seed_machine(REMOTE)

    async def fake_read(target: str, *, timeout_s: float = 0.0) -> InventoryReadResult:
        raise _cluster_rpc.ClusterOpUnreachable("no ack")

    monkeypatch.setattr(inventory_router, "_dispatch_inventory_read", fake_read)

    with TestClient(app) as client:
        resp = client.get(f"/api/inventory?machine={REMOTE}")
    assert resp.status_code == 503, resp.text
    assert REMOTE in resp.json()["detail"]


# ── PUT ──


def test_put_capability_rejection_surfaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """PUT where the host-side op rejects a toggle -> applied=False echoed with
    the per-item verdict."""
    _seed_machine(REMOTE)

    async def fake_write(
        target: str, plugins: dict[str, bool], mcp_servers: dict[str, bool]
    ) -> InventoryWriteOpResult:
        return InventoryWriteOpResult(
            machine=target,
            plugin_results={
                "no_such": FieldWriteResult(ok=False, reason="not installed on this host")
            },
            mcp_results={},
            applied=False,
        )

    monkeypatch.setattr(inventory_router, "_dispatch_inventory_write", fake_write)

    with TestClient(app) as client:
        resp = client.put(
            f"/api/inventory?machine={REMOTE}",
            json={"plugins": {"no_such": True}, "mcp_servers": {}},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is False
    assert body["plugin_results"]["no_such"]["ok"] is False
    assert body["plugin_results"]["no_such"]["reason"]


def test_put_unknown_machine_404() -> None:
    """PUT ?machine=<unknown> -> 404 before any dispatch."""
    with TestClient(app) as client:
        resp = client.put("/api/inventory?machine=ghost-host", json={"plugins": {"x": True}})
    assert resp.status_code == 404, resp.text
    assert "unknown agent-runner" in resp.json()["detail"]


def test_put_gateway_machine_404() -> None:
    """PUT ?machine=<gateway> -> 404: the gateway has no inventory to
    write to."""
    _seed_machine("gw-host", role="gateway")
    with TestClient(app) as client:
        resp = client.put("/api/inventory?machine=gw-host", json={"plugins": {"x": True}})
    assert resp.status_code == 404, resp.text
    assert "unknown agent-runner" in resp.json()["detail"]


def test_put_missing_machine_400() -> None:
    """PUT with no ?machine= -> 400: inventory writes have no gateway
    default target."""
    with TestClient(app) as client:
        resp = client.put("/api/inventory", json={"plugins": {"x": True}})
    assert resp.status_code == 400, resp.text
    assert "require ?machine=" in resp.json()["detail"]


def test_put_non_dict_half_422() -> None:
    """PUT with a non-dict 'plugins' half -> 422: the InventoryWriteRequest body
    model rejects the malformed shape at FastAPI validation, before the handler."""
    _seed_machine(REMOTE)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/inventory?machine={REMOTE}", json={"plugins": ["not", "a", "dict"]}
        )
    assert resp.status_code == 422, resp.text
