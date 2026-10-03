"""/api/status cluster sub-section integration tests.

Agent-runners do not run a local gateway; probes dial the agent-runner's ops server directly
to run the status_probe op (same source as the CLI `ava cluster status`). These tests stub
that async fan-out path and only validate the SystemStatus.cluster data pipeline.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.db import Database
from gateway.app import app
from gateway.cluster import status as status_router
from gateway.cluster.status import StatusCache
from ops import cluster_rpc

_OPS_URL = "http://wsl:18121"


class _RemoteProbeResults(dict[str, tuple[bool, bool | None]]):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []


@pytest.fixture(autouse=True)
def _truncate_machines(db_conn: psycopg.Connection) -> None:
    """The conftest db_conn TRUNCATE list does not include machines — this test suite
    clears it itself. autouse so every test starts with an empty machines table."""
    with db_conn.cursor() as cur:
        cur.execute("TRUNCATE machines")
    db_conn.commit()


@pytest.fixture
def fake_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The pause signal, controllable via a tmp file. R1 (Task #1021) moved
    `is_paused` to the host_deploy_state posture row; the status router's bound
    name is shimmed to the file's existence so this suite keeps simulating the
    pause with a file."""
    flag = tmp_path / "cluster_paused"

    def paused(_db: Database) -> bool:
        return flag.exists()

    monkeypatch.setattr("gateway.cluster.status.cluster_is_paused", paused)
    return flag


@pytest.fixture
def stub_machine_identity(set_machine_identity) -> None:
    """Set this unit's identity (gateway, name 'cloud-test') at the source
    so every machine_name() / machine_role() call site resolves it without
    per-module patching."""
    set_machine_identity(role="gateway", name="cloud-test")


@pytest.fixture
def stub_remote_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> _RemoteProbeResults:
    """Replace _probe_agent_runner with a lookup table — key=name,
    val=(online, paused). Local rows in this test suite are all pure gateway
    (handled by lightweight local read, no probe), so the table only needs to
    cover agent-runner rows.
    """
    from datetime import datetime

    from base.api_contracts.status import MachineStatus

    results = _RemoteProbeResults()

    async def fake_probe(
        name: str,
        role: list[str],
        gateway_url: str | None,
        up_since_at: datetime,
        description: str | None,
        stopped_at: datetime | None,
        is_staging: bool = False,
    ) -> MachineStatus:
        results.calls.append(name)
        online, paused = results.get(name, (False, None))
        return MachineStatus(
            name=name,
            serve_gateway="gateway" in role,
            serve_agent_runner="agent-runner" in role,
            serve_observability_station="observability-station" in role,
            gateway_url=gateway_url or "",
            up_since_at=up_since_at,
            online=online,
            paused=paused,
            description=description,
            stopped_at=stopped_at,
            is_staging=is_staging,
        )

    monkeypatch.setattr(status_router, "_probe_agent_runner", fake_probe)
    return results


def _insert_machine(
    conn: psycopg.Connection,
    name: str,
    gateway_url: str | None,
    role: str,
    description: str | None = None,
) -> None:
    """Direct INSERT of a machines row (bypassing register_self) — for test isolation.

    `role` is a comma-separated capability set ("gateway,agent-runner") — the
    machines.role column is a TEXT[] of capability tokens."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO machines (name, gateway_url, role, description) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET gateway_url=EXCLUDED.gateway_url, "
            "role=EXCLUDED.role, description=EXCLUDED.description, up_since_at=NOW()",
            (name, gateway_url, [t for t in role.split(",") if t], description),
        )
    conn.commit()


def _assert_local_machine_row(row: dict[str, Any]) -> None:
    # local row picks up status_snapshot() — online=True, paused=False
    assert row["online"] is True
    assert row["paused"] is False
    assert row["serve_gateway"] is True
    assert row["serve_agent_runner"] is False
    assert row["description"] == "central node"


def _assert_remote_machine_rows(by_name: dict[str, dict[str, Any]]) -> None:
    # remote rows come from the stub
    assert by_name["test-host"]["online"] is True
    assert by_name["test-host"]["paused"] is False
    assert by_name["test-host"]["serve_gateway"] is False
    assert by_name["test-host"]["serve_agent_runner"] is True
    assert by_name["wsl-test"]["online"] is False
    assert by_name["wsl-test"]["paused"] is None
    assert by_name["wsl-test"]["serve_gateway"] is False
    assert by_name["wsl-test"]["serve_agent_runner"] is True


class TestClusterPanel:
    def test_current_machine_role_paused_from_local(
        self,
        db_conn: psycopg.Connection,
        fake_flag: Path,
        stub_machine_identity: None,
        stub_remote_probe: dict[str, tuple[bool, bool | None]],
    ) -> None:
        """current_* fields reflect local machine perspective."""
        _ = fake_flag, stub_machine_identity, db_conn
        del stub_remote_probe
        with TestClient(app) as client:
            r = client.get("/api/status")
        assert r.status_code == 200
        data = r.json()
        # Scheduler panel removed in watcher-unify; shells folded per-machine into
        # cluster.machines — /api/status carries only services / cluster now.
        assert "scheduler" not in data
        assert set(data) >= {"services", "cluster"}
        c = data["cluster"]
        assert c["current_machine"] == "cloud-test"
        assert c["current_serve_gateway"] is True
        assert c["current_serve_agent_runner"] is False
        assert c["current_paused"] is False

    def test_current_paused_true_when_flag_exists(
        self,
        db_conn: psycopg.Connection,
        fake_flag: Path,
        stub_machine_identity: None,
        stub_remote_probe: dict[str, tuple[bool, bool | None]],
        database: Database,
    ) -> None:
        """During a pause /api/status is short-circuited to 503 by middleware; call the
        helper directly to verify the cluster sub-section correctly reflects the paused flag."""
        del stub_remote_probe
        _ = stub_machine_identity
        fake_flag.write_text("")
        with db_conn.cursor() as cur:
            panel = status_router._get_cluster_status(database, cur)
        assert panel.current_paused is True

    def test_machines_list_local_plus_remote(
        self,
        db_conn: psycopg.Connection,
        fake_flag: Path,
        stub_machine_identity: None,
        stub_remote_probe: dict[str, tuple[bool, bool | None]],
    ) -> None:
        """gateway row → status_snapshot() (always online);
        agent-runner row → stub_remote_probe lookup."""
        _ = fake_flag, stub_machine_identity
        _insert_machine(db_conn, "cloud-test", "https://ava.example.com", "gateway", "central node")
        _insert_machine(db_conn, "test-host", None, "agent-runner")
        _insert_machine(db_conn, "wsl-test", None, "agent-runner")
        stub_remote_probe["test-host"] = (True, False)
        stub_remote_probe["wsl-test"] = (False, None)  # probe failed

        with TestClient(app) as client:
            r = client.get("/api/status")
        assert r.status_code == 200
        machines = r.json()["cluster"]["machines"]
        assert len(machines) == 3
        by_name = {m["name"]: m for m in machines}
        _assert_local_machine_row(by_name["cloud-test"])
        _assert_remote_machine_rows(by_name)

    def test_station_machine_row_carries_station_capability(
        self,
        db_conn: psycopg.Connection,
        fake_flag: Path,
        stub_machine_identity: None,
        stub_remote_probe: dict[str, tuple[bool, bool | None]],
    ) -> None:
        """A pure observability-station row (machines.role carries only the
        station token) renders serve_observability_station=True and the two
        legacy flags False — the roster must not lose the station capability
        when deriving the flag triple from the role column."""
        _ = fake_flag, stub_machine_identity
        _insert_machine(db_conn, "station-a", None, "observability-station")
        _insert_machine(
            db_conn, "combo", "https://ava.example.com", "gateway,observability-station"
        )
        stub_remote_probe["station-a"] = (True, False)
        stub_remote_probe["combo"] = (True, False)

        with TestClient(app) as client:
            r = client.get("/api/status")
        assert r.status_code == 200
        by_name = {m["name"]: m for m in r.json()["cluster"]["machines"]}
        assert by_name["station-a"]["serve_observability_station"] is True
        assert by_name["station-a"]["serve_gateway"] is False
        assert by_name["station-a"]["serve_agent_runner"] is False
        assert by_name["combo"]["serve_observability_station"] is True
        assert by_name["combo"]["serve_gateway"] is True
        assert by_name["combo"]["serve_agent_runner"] is False

    def test_agent_runner_gateway_url_null_no_crash(
        self,
        db_conn: psycopg.Connection,
        fake_flag: Path,
        stub_machine_identity: None,
        stub_remote_probe: dict[str, tuple[bool, bool | None]],
    ) -> None:
        """Regression: PR #466 made machines.gateway_url nullable for agent-runner;
        pre-fix `_probe_machine(url.rstrip(...))` crashed with AttributeError
        and the outer try/except masked it as cluster.machines=[]."""
        _ = fake_flag, stub_machine_identity
        _insert_machine(db_conn, "wsl-test", None, "agent-runner")
        stub_remote_probe["wsl-test"] = (True, False)

        with TestClient(app) as client:
            r = client.get("/api/status")
        assert r.status_code == 200
        machines = r.json()["cluster"]["machines"]
        assert len(machines) == 1
        assert machines[0]["name"] == "wsl-test"
        assert machines[0]["online"] is True
        assert machines[0]["gateway_url"] == ""  # NULL → ""

    def test_empty_machines_table(
        self,
        db_conn: psycopg.Connection,
        fake_flag: Path,
        stub_machine_identity: None,
        stub_remote_probe: dict[str, tuple[bool, bool | None]],
    ) -> None:
        del stub_remote_probe
        _ = fake_flag, stub_machine_identity
        with TestClient(app) as client:
            r = client.get("/api/status")
        assert r.status_code == 200
        assert r.json()["cluster"]["machines"] == []

    def test_response_cache_reuses_remote_probe(
        self,
        db_conn: psycopg.Connection,
        fake_flag: Path,
        stub_machine_identity: None,
        stub_remote_probe: _RemoteProbeResults,
    ) -> None:
        _ = fake_flag, stub_machine_identity
        _insert_machine(db_conn, "wsl-test", None, "agent-runner")
        stub_remote_probe["wsl-test"] = (True, False)

        with TestClient(app) as client:
            first = client.get("/api/status")
            second = client.get("/api/status")

        assert first.status_code == 200
        assert second.status_code == 200
        assert stub_remote_probe.calls == ["wsl-test"]

    def test_response_cache_ttl_zero_reprobes(
        self,
        db_conn: psycopg.Connection,
        fake_flag: Path,
        stub_machine_identity: None,
        stub_remote_probe: _RemoteProbeResults,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _ = fake_flag, stub_machine_identity
        monkeypatch.setattr("gateway.app.StatusCache", lambda: StatusCache(ttl_s=0.0))
        _insert_machine(db_conn, "wsl-test", None, "agent-runner")
        stub_remote_probe["wsl-test"] = (True, False)

        with TestClient(app) as client:
            first = client.get("/api/status")
            second = client.get("/api/status")

        assert first.status_code == 200
        assert second.status_code == 200
        assert stub_remote_probe.calls == ["wsl-test", "wsl-test"]

    def test_response_cache_keeps_retry_recovered_result(
        self,
        db_conn: psycopg.Connection,
        fake_flag: Path,
        stub_machine_identity: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A transport reset recovered by the roster's retry is cached as the
        successful final verdict, not as a transient offline row."""
        _ = fake_flag, stub_machine_identity
        _insert_machine(db_conn, "wsl-test", _OPS_URL, "agent-runner")
        calls: list[dict[str, object]] = []

        async def retry_aware_dispatch(**kw: object) -> dict[str, object]:
            calls.append(kw)
            assert kw["ops_url"] == _OPS_URL
            if kw["retries"] != 1:
                raise cluster_rpc.ClusterOpUnreachable("connection reset was not retried")
            return {
                "machine_name": "wsl-test",
                "serve_gateway": False,
                "serve_agent_runner": True,
                "paused": False,
            }

        monkeypatch.setattr(cluster_rpc, "dispatch_to_url", retry_aware_dispatch)
        with TestClient(app) as client:
            first = client.get("/api/status")
            second = client.get("/api/status")

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["cluster"]["machines"][0]["online"] is True
        assert second.json()["cluster"]["machines"][0]["online"] is True
        assert len(calls) == 1


class TestProbeAgentRunner:
    """_probe_agent_runner maps transport and reached-host outcomes distinctly."""

    @pytest.mark.asyncio
    async def test_timeout_budget_comes_from_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The probe deadline is `settings.gateway.status_probe_timeout_seconds`,
        not a hardcoded literal — a slow-but-healthy runner must not read
        offline because its snapshot outgrew a fixed 3s budget (task #1200:
        wsl's status_snapshot measured 3.07-3.27s vs the old 3.0s, flipping
        it offline while /healthz answered in ~15ms)."""
        from datetime import UTC, datetime

        from base.config import settings

        monkeypatch.setattr(settings.gateway, "status_probe_timeout_seconds", 11.0)
        seen: dict[str, object] = {}

        async def fake_enqueue(*_a: object, **_kw: object) -> dict[str, object]:
            seen["timeout_s"] = _kw.get("timeout_s")
            seen["ops_url"] = _kw.get("ops_url")
            seen["retries"] = _kw.get("retries")
            return {
                "machine_name": "wsl",
                "serve_gateway": False,
                "serve_agent_runner": True,
                "paused": False,
            }

        monkeypatch.setattr(cluster_rpc, "dispatch_to_url", fake_enqueue)
        r = await status_router._probe_agent_runner(
            "wsl", ["agent-runner"], _OPS_URL, datetime(2026, 5, 24, tzinfo=UTC), None, None
        )
        assert seen["timeout_s"] == 11.0
        assert seen["ops_url"] == _OPS_URL
        assert seen["retries"] == 1
        assert r.online is True

    @pytest.mark.asyncio
    async def test_missing_registered_url_is_offline_without_dispatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import UTC, datetime

        called = False

        async def must_not_dispatch(**_kw: object) -> dict[str, object]:
            nonlocal called
            called = True
            raise AssertionError("missing roster URL must not trigger a DB re-lookup")

        monkeypatch.setattr(cluster_rpc, "dispatch_to_url", must_not_dispatch)
        row = await status_router._probe_agent_runner(
            "wsl", ["agent-runner"], None, datetime(2026, 5, 24, tzinfo=UTC), None, None
        )

        assert row.online is False
        assert called is False

    @pytest.mark.asyncio
    async def test_blackhole_stays_inside_single_total_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Retry is inside one roster deadline: a blackholed first attempt is
        cancelled at the configured budget rather than getting a second full
        timeout window."""
        from datetime import UTC, datetime

        from base.config import settings

        monkeypatch.setattr(settings.gateway, "status_probe_timeout_seconds", 0.01)
        cancelled = asyncio.Event()

        async def blackhole(**_kw: object) -> dict[str, object]:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise AssertionError("unreachable")

        monkeypatch.setattr(cluster_rpc, "dispatch_to_url", blackhole)
        row = await asyncio.wait_for(
            status_router._probe_agent_runner(
                "wsl",
                ["agent-runner"],
                _OPS_URL,
                datetime(2026, 5, 24, tzinfo=UTC),
                None,
                None,
            ),
            timeout=0.2,
        )

        assert row.online is False
        assert cancelled.is_set()

    @pytest.mark.asyncio
    async def test_timeout_returns_offline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from datetime import UTC, datetime

        async def fake_enqueue(*_a: object, **_kw: object) -> dict[str, object]:
            raise cluster_rpc.ClusterOpUnreachable("ops server unreachable")

        monkeypatch.setattr(cluster_rpc, "dispatch_to_url", fake_enqueue)
        stopped = datetime(2026, 5, 25, tzinfo=UTC)
        r = await status_router._probe_agent_runner(
            "wsl",
            ["agent-runner"],
            _OPS_URL,
            datetime(2026, 5, 24, tzinfo=UTC),
            "voice IO + browser",
            stopped,
        )
        assert r.online is False
        assert r.paused is None
        assert r.description == "voice IO + browser"
        # offline + stopped_at threads through so the UI can show "stopped" not "offline"
        assert r.stopped_at == stopped

    @pytest.mark.asyncio
    async def test_success_returns_paused_from_ops(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from datetime import UTC, datetime

        async def fake_enqueue(*_a: object, **_kw: object) -> dict[str, object]:
            # A full status_probe response (the shape ClusterStatus.model_dump emits).
            return {
                "machine_name": "wsl",
                "serve_gateway": False,
                "serve_agent_runner": True,
                "paused": True,
            }

        monkeypatch.setattr(cluster_rpc, "dispatch_to_url", fake_enqueue)
        r = await status_router._probe_agent_runner(
            "wsl",
            ["agent-runner"],
            _OPS_URL,
            datetime(2026, 5, 24, tzinfo=UTC),
            "voice IO + browser",
            None,
        )
        assert r.online is True
        assert r.paused is True
        assert r.description == "voice IO + browser"

    @pytest.mark.asyncio
    async def test_success_threads_head_sha(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The runner's status_probe response carries head_sha; the probe threads
        it onto MachineStatus so the roster can compute drift."""
        from datetime import UTC, datetime

        async def fake_enqueue(*_a: object, **_kw: object) -> dict[str, object]:
            return {
                "machine_name": "wsl",
                "serve_gateway": False,
                "serve_agent_runner": True,
                "paused": False,
                "head_sha": "def5678",
            }

        monkeypatch.setattr(cluster_rpc, "dispatch_to_url", fake_enqueue)
        r = await status_router._probe_agent_runner(
            "wsl", ["agent-runner"], _OPS_URL, datetime(2026, 5, 24, tzinfo=UTC), None, None
        )
        assert r.head_sha == "def5678"

    @pytest.mark.asyncio
    async def test_malformed_response_is_online_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reachable host whose status_probe body does NOT validate as
        ClusterStatus (a version-skewed / wrong server) lands in the documented
        online=True + paused=None abnormal state — never disguised as a
        determinate paused verdict, and distinct from offline."""
        from datetime import UTC, datetime

        async def fake_enqueue(*_a: object, **_kw: object) -> dict[str, object]:
            # Missing the required serve_gateway / serve_agent_runner / serve_observability_station fields.
            return {"machine_name": "wsl", "paused": True}

        monkeypatch.setattr(cluster_rpc, "dispatch_to_url", fake_enqueue)
        r = await status_router._probe_agent_runner(
            "wsl", ["agent-runner"], _OPS_URL, datetime(2026, 5, 24, tzinfo=UTC), None, None
        )
        assert r.online is True
        assert r.paused is None


class TestPanelCarriesNoFrozenPin:
    def test_panel_reports_the_checkout_without_a_pin_verdict(
        self,
        db_conn: psycopg.Connection,
        fake_flag: Path,
        stub_machine_identity: None,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
    ) -> None:
        """The panel reports each node's checkout and carries no cluster target or
        known-good verdict."""
        _ = fake_flag, stub_machine_identity
        _insert_machine(db_conn, "cloud-test", "https://ava.example.com", "gateway")

        monkeypatch.setattr(status_router, "prod_source_head_sha", lambda: "abc1234")
        with db_conn.cursor() as cur:
            panel = status_router._get_cluster_status(database, cur)
        body = panel.model_dump()
        assert "cluster_target_sha" not in body
        assert "cluster_last_known_good_sha" not in body
        (machine,) = panel.machines
        assert machine.head_sha == "abc1234"
        assert "on_pin" not in machine.model_dump()
