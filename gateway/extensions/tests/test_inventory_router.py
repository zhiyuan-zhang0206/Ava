"""GET/PUT /api/inventory router tests — the cross-machine plugin + MCP matrix.

The aggregate fans out an inventory_read to every agent-runner, collapses the
results into a per-item matrix, and buckets a host whose read raised into
`unreachable`. These tests stub the public cluster RPC transport so they stay
deterministic without a real remote runner. Matrix contracts live in test_inventory_matrix.py.

Inventory is agent-runner-only: a gateway row is never a column and is
rejected as a `?machine=` target. `?machine=` selects a single agent-runner's
flat view; an unknown / non-agent-runner name 404s before any dispatch.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from base.config import settings
from base.db import Database
from gateway.app import app
from gateway.cluster import snapshots
from gateway.cluster.snapshots import Snapshot
from gateway.extensions import inventory as inventory_router
from ops.cluster import rpc as _cluster_rpc
from ops.rpc_schemas import FieldWriteResult, InventoryReadResult, InventoryWriteOpResult


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


def _stub_reads(
    monkeypatch: pytest.MonkeyPatch,
    read: Callable[..., Awaitable[InventoryReadResult]],
) -> None:
    async def dispatch(
        db: Database,
        *,
        target_machine: str,
        kind: str,
        payload: dict[str, object],
        timeout_s: float | None = None,
        retries: int | None = None,
    ) -> dict[str, object]:
        assert kind == "inventory_read"
        assert payload == {}
        result = await read(db, target_machine, timeout_s=timeout_s, retries=retries)
        return result.model_dump()

    monkeypatch.setattr(_cluster_rpc, "dispatch_to_machine", dispatch)


def _stub_writes(
    monkeypatch: pytest.MonkeyPatch,
    write: Callable[..., Awaitable[InventoryWriteOpResult]],
) -> None:
    async def dispatch(
        db: Database,
        *,
        target_machine: str,
        kind: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        assert kind == "inventory_write"
        result = await write(db, target_machine, payload["plugins"], payload["mcp_servers"])
        return result.model_dump()

    monkeypatch.setattr(_cluster_rpc, "dispatch_to_machine", dispatch)


# ── machines-table seeding helper ──


def _seed_machine(name: str, *, role: str = "agent-runner", stopped: bool = False) -> None:
    """Insert a minimal machine row so `_assert_inventory_target` / the aggregate
    fan-out find it. Defaults to agent-runner (the inventory-eligible role); pass
    role='gateway' to seed a row that inventory must ignore. The per-test
    TRUNCATE (conftest) clears it again."""
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO machines (name, gateway_url, role, stopped_at) "
            "VALUES (%s, %s, %s, CASE WHEN %s THEN now() ELSE NULL END) "
            "ON CONFLICT (name) DO NOTHING",
            (name, "http://remote.invalid:8000", [role], stopped),  # machines.role is TEXT[]
        )
        conn.commit()


REMOTE = "remote-host"


# ── GET aggregate ──


def test_aggregate_collapse_across_two_machines(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/inventory (no machine): two machines, A has plugin X enabled, B
    lacks X -> X row has hosts[A].present True+enabled, hosts[B].present False."""
    _seed_machine("A")
    _seed_machine("B")

    async def fake_read(
        db: Database, target: str, *, timeout_s: float = 0.0, retries: int | None = None
    ) -> InventoryReadResult:
        if target == "A":
            return _read({"X": _plugin(enabled=True, description="plugin X")}, {}, "A")
        return _read({}, {}, "B")

    _stub_reads(monkeypatch, fake_read)

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
    _seed_machine("A")
    _seed_machine("B")

    async def fake_read(
        db: Database, target: str, *, timeout_s: float = 0.0, retries: int | None = None
    ) -> InventoryReadResult:
        if target == "A":
            return _read({"X": _plugin(enabled=True)}, {}, "A")
        raise _cluster_rpc.ClusterOpUnreachable("no ack")

    _stub_reads(monkeypatch, fake_read)

    with TestClient(app) as client:
        resp = client.get("/api/inventory")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["unreachable"] == ["B"]
    x = {r["name"]: r for r in body["plugins"]}["X"]
    assert "B" not in x["hosts"]
    assert set(x["hosts"]) == {"A"}


def _snapshot(*, failures: int, age_s: float = 5.0) -> Snapshot:
    observed = datetime.now(UTC) - timedelta(seconds=age_s)
    return Snapshot(
        observed_at=observed,
        reachable=failures == 0,
        consecutive_failures=failures,
        status=None,
        status_at=None,
    )


def test_aggregate_skips_stopped_and_known_down_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host already known down is never dialed: an intentionally stopped row
    (`stopped_at`) and a host the heartbeat liveness pass has just failed twice
    (the roster's snapshot) land in `unreachable` directly; the columns stay
    (task #4127). A host that failed once, or whose snapshot is stale (the pass is
    not running), is still dialed."""
    for machine in ["A", "B", "C", "D", "E"]:
        _seed_machine(machine, stopped=machine == "B")

    def read_snapshots(_pool: ConnectionPool) -> dict[str, Snapshot]:
        return {
            "C": _snapshot(failures=2),  # known down: skipped
            "D": _snapshot(failures=1),  # one dropped probe: still dialed
            "E": _snapshot(failures=5, age_s=3600.0),  # stale: no evidence, dialed
        }

    monkeypatch.setattr(snapshots, "read_all_blocking", read_snapshots)

    dialed: list[str] = []

    async def fake_read(
        db: Database, target: str, *, timeout_s: float = 0.0, retries: int | None = None
    ) -> InventoryReadResult:
        dialed.append(target)
        return _read({"X": _plugin(enabled=True)}, {}, target)

    _stub_reads(monkeypatch, fake_read)

    with TestClient(app) as client:
        resp = client.get("/api/inventory")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert sorted(dialed) == ["A", "D", "E"]
    assert body["machines"] == ["A", "B", "C", "D", "E"]
    assert body["unreachable"] == ["B", "C"]


def test_aggregate_dials_run_under_the_aggregate_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gateway keeps no failure memory: every dialed host gets the one bounded
    budget and one fast retry, and a dial that comes back unreachable is only bucketed."""
    _seed_machine("A")
    _seed_machine("B")
    seen: dict[str, tuple[float, int | None]] = {}

    async def fake_read(
        db: Database, target: str, *, timeout_s: float = 0.0, retries: int | None = None
    ) -> InventoryReadResult:
        seen[target] = (timeout_s, retries)
        if target == "A":
            return _read({"X": _plugin(enabled=True)}, {}, "A")
        raise _cluster_rpc.ClusterOpUnreachable("no ack")

    _stub_reads(monkeypatch, fake_read)

    with TestClient(app) as client:
        resp = client.get("/api/inventory")
    assert resp.status_code == 200, resp.text
    assert resp.json()["unreachable"] == ["B"]
    budget = inventory_router._AGGREGATE_READ_TIMEOUT_S
    assert seen["A"] == (budget, 1) and seen["B"] == (budget, 1)


# ── GET single machine ──


def test_get_single_machine_view(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET ?machine=<remote>: the op's plugin/MCP dicts become sorted flat lists."""
    _seed_machine(REMOTE)

    async def fake_read(
        db: Database, target: str, *, timeout_s: float = 0.0, retries: int | None = None
    ) -> InventoryReadResult:
        return _read(
            {"b_plug": _plugin(enabled=True), "a_plug": _plugin(enabled=False)},
            {"srv": _mcp(enabled=True, can_enable=True, reason=None)},
            REMOTE,
        )

    _stub_reads(monkeypatch, fake_read)

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

    async def fake_read(
        db: Database, target: str, *, timeout_s: float = 0.0, retries: int | None = None
    ) -> InventoryReadResult:
        assert target == REMOTE  # the gateway must never be dispatched to
        return _read({"X": _plugin(enabled=True)}, {}, REMOTE)

    _stub_reads(monkeypatch, fake_read)

    with TestClient(app) as client:
        resp = client.get("/api/inventory")
    assert resp.status_code == 200, resp.text
    assert resp.json()["machines"] == [REMOTE]


def test_get_single_machine_offline_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET ?machine=<known>: the inventory_read times out -> 503."""
    _seed_machine(REMOTE)

    async def fake_read(
        db: Database, target: str, *, timeout_s: float = 0.0, retries: int | None = None
    ) -> InventoryReadResult:
        raise _cluster_rpc.ClusterOpUnreachable("no ack")

    _stub_reads(monkeypatch, fake_read)

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
        db: Database, target: str, plugins: dict[str, bool], mcp_servers: dict[str, bool]
    ) -> InventoryWriteOpResult:
        return InventoryWriteOpResult(
            machine=target,
            plugin_results={
                "no_such": FieldWriteResult(ok=False, reason="not installed on this host")
            },
            mcp_results={},
            applied=False,
        )

    _stub_writes(monkeypatch, fake_write)

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


def test_inventory_uses_each_request_apps_database_and_pool(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two serving apps route SQL and RPC through their own attached resources."""
    _seed_machine(REMOTE)
    seen_databases: list[Database] = []

    async def dispatch(
        db: Database,
        *,
        target_machine: str,
        kind: str,
        payload: dict[str, object],
        timeout_s: float | None = None,
        retries: int | None = None,
    ) -> dict[str, object]:
        seen_databases.append(db)
        assert target_machine == REMOTE
        if kind == "inventory_read":
            return _read({"X": _plugin(enabled=True)}, {}, REMOTE).model_dump()
        assert kind == "inventory_write"
        assert payload == {"plugins": {"X": False}, "mcp_servers": {}}
        return InventoryWriteOpResult(
            machine=REMOTE,
            plugin_results={"X": FieldWriteResult(ok=True)},
            mcp_results={},
            applied=True,
        ).model_dump()

    monkeypatch.setattr(_cluster_rpc, "dispatch_to_machine", dispatch)
    other_database = Database.from_settings()
    with database.pool(max_size=2) as first_pool, other_database.pool(max_size=2) as second_pool:
        for db, pool in [(database, first_pool), (other_database, second_pool)]:
            pool_requests = pool.get_stats().get("requests_num", 0)
            serving_app = FastAPI()
            serving_app.state.db = db
            serving_app.state.db_pool = pool
            serving_app.include_router(inventory_router.router)
            with TestClient(serving_app) as client:
                for url in ["/api/inventory", f"/api/inventory?machine={REMOTE}"]:
                    response = client.get(url)
                    assert response.status_code == 200, response.text
                response = client.put(
                    f"/api/inventory?machine={REMOTE}", json={"plugins": {"X": False}}
                )
                assert response.status_code == 200, response.text
                assert response.json()["applied"] is True
            assert pool.get_stats()["requests_num"] - pool_requests == 4
            assert seen_databases == [db] * 3
            seen_databases.clear()
