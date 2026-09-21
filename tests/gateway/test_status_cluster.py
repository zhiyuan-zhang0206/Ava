"""/api/status cluster sub-section integration tests.

Agent-runners do not run a local gateway; probes dial the agent-runner's ops server directly
to run the status_probe op (same source as the CLI `ava cluster status`). These tests stub
that async fan-out path and only validate the SystemStatus.cluster data pipeline.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.routers import _roster_probe, _roster_rows
from gateway.routers import status as status_router

_OPS_URL = "http://wsl:18121"


class _RemoteProbeResults(dict[str, tuple[bool, bool | None]]):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []


@pytest.fixture(autouse=True)
def _clear_status_cache() -> Iterator[None]:
    status_router.cache_clear()
    yield
    status_router.cache_clear()


@pytest.fixture(autouse=True)
def _truncate_machines(db_conn: psycopg.Connection) -> None:
    """The conftest db_conn TRUNCATE list does not include machines — this test suite
    clears it itself. autouse so every test starts with an empty machines table."""
    with db_conn.cursor() as cur:
        cur.execute("TRUNCATE machines")
    db_conn.commit()


@pytest.fixture(autouse=True)
def _reset_probe_backoff() -> None:
    """The per-machine probe backoff (`gateway.routers._roster_probe`) is
    module-level mutable state; clear it before each test so a failure recorded
    by one test cannot defer a probe in the next (e.g. the offline/online cases
    both use name 'wsl'). The detached dial's in-flight registry is cleared the
    same way."""
    _roster_probe._probe_failures.clear()
    _roster_probe._recovery_inflight.clear()


@pytest.fixture
def fake_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The pause signal, controllable via a tmp file. R1 (Task #1021) moved
    `is_paused` to the host_deploy_state posture row; the status router's bound
    name is shimmed to the file's existence so this suite keeps simulating the
    pause with a file."""
    flag = tmp_path / "cluster_paused"
    monkeypatch.setattr("gateway.routers.status.cluster_is_paused", flag.exists)
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

    from gateway.schemas import MachineStatus

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
    ) -> None:
        """During a pause /api/status is short-circuited to 503 by middleware; call the
        helper directly to verify the cluster sub-section correctly reflects the paused flag."""
        del stub_remote_probe
        _ = stub_machine_identity
        fake_flag.write_text("")
        with db_conn.cursor() as cur:
            panel = status_router._get_cluster_status(cur)
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
        # local row picks up status_snapshot() — online=True, paused=False
        assert by_name["cloud-test"]["online"] is True
        assert by_name["cloud-test"]["paused"] is False
        assert by_name["cloud-test"]["serve_gateway"] is True
        assert by_name["cloud-test"]["serve_agent_runner"] is False
        assert by_name["cloud-test"]["description"] == "central node"
        # remote rows come from the stub
        assert by_name["test-host"]["online"] is True
        assert by_name["test-host"]["paused"] is False
        assert by_name["test-host"]["serve_gateway"] is False
        assert by_name["test-host"]["serve_agent_runner"] is True
        assert by_name["wsl-test"]["online"] is False
        assert by_name["wsl-test"]["paused"] is None
        assert by_name["wsl-test"]["serve_gateway"] is False
        assert by_name["wsl-test"]["serve_agent_runner"] is True

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
        monkeypatch.setattr(status_router, "_STATUS_CACHE_TTL_S", 0.0)
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
                from ops import cluster_rpc as cw

                raise cw.ClusterOpUnreachable("connection reset was not retried")
            return {
                "machine_name": "wsl-test",
                "serve_gateway": False,
                "serve_agent_runner": True,
                "paused": False,
            }

        monkeypatch.setattr(status_router._cluster_rpc, "dispatch_to_machine", retry_aware_dispatch)
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

        from shared.config import settings

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

        monkeypatch.setattr(status_router._cluster_rpc, "dispatch_to_machine", fake_enqueue)
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

        monkeypatch.setattr(status_router._cluster_rpc, "dispatch_to_machine", must_not_dispatch)
        row = await status_router._probe_agent_runner(
            "wsl", ["agent-runner"], None, datetime(2026, 5, 24, tzinfo=UTC), None, None
        )

        assert row.online is False
        assert called is False
        assert _roster_probe._probe_failures["wsl"][0] == 1

    @pytest.mark.asyncio
    async def test_blackhole_stays_inside_single_total_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Retry is inside one roster deadline: a blackholed first attempt is
        cancelled at the configured budget rather than getting a second full
        timeout window."""
        from datetime import UTC, datetime

        from shared.config import settings

        monkeypatch.setattr(settings.gateway, "status_probe_timeout_seconds", 0.01)
        cancelled = asyncio.Event()

        async def blackhole(**_kw: object) -> dict[str, object]:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise AssertionError("unreachable")

        monkeypatch.setattr(status_router._cluster_rpc, "dispatch_to_machine", blackhole)
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
        assert _roster_probe._probe_failures["wsl"][0] == 1

    @pytest.mark.asyncio
    async def test_timeout_returns_offline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from datetime import UTC, datetime

        from ops import cluster_rpc as cw

        async def fake_enqueue(*_a: object, **_kw: object) -> dict[str, object]:
            raise cw.ClusterOpUnreachable("ops server unreachable")

        monkeypatch.setattr(status_router._cluster_rpc, "dispatch_to_machine", fake_enqueue)
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

        monkeypatch.setattr(status_router._cluster_rpc, "dispatch_to_machine", fake_enqueue)
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
        assert "wsl" not in _roster_probe._probe_failures  # a clean first contact records nothing

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

        monkeypatch.setattr(status_router._cluster_rpc, "dispatch_to_machine", fake_enqueue)
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

        monkeypatch.setattr(status_router._cluster_rpc, "dispatch_to_machine", fake_enqueue)
        r = await status_router._probe_agent_runner(
            "wsl", ["agent-runner"], _OPS_URL, datetime(2026, 5, 24, tzinfo=UTC), None, None
        )
        assert r.online is True
        assert r.paused is None


class TestProbeBackoff:
    """P1 per-machine probe backoff: a down host is re-probed on an exponential
    schedule (min(base * 2**failures, cap)s — both bounds config since task
    #3507) instead of on every ~5s panel poll."""

    def test_no_record_not_in_backoff(self) -> None:
        assert _roster_probe._probe_in_backoff("wsl") is False

    def test_note_unreachable_increments_failures(self) -> None:
        _roster_probe._note_probe_unreachable("wsl")
        assert _roster_probe._probe_failures["wsl"][0] == 1
        _roster_probe._note_probe_unreachable("wsl")
        assert _roster_probe._probe_failures["wsl"][0] == 2

    def test_reachable_clears_backoff(self) -> None:
        _roster_probe._probe_failures["wsl"] = (3, 0.0)
        _roster_probe._note_probe_reachable("wsl")
        assert "wsl" not in _roster_probe._probe_failures

    def test_within_window_defers_past_window_reprobes(self) -> None:
        now = _roster_probe.time.monotonic()
        # 1 failure -> 10s window
        _roster_probe._probe_failures["wsl"] = (1, now - 5.0)
        assert _roster_probe._probe_in_backoff("wsl") is True
        _roster_probe._probe_failures["wsl"] = (1, now - 11.0)
        assert _roster_probe._probe_in_backoff("wsl") is False

    def test_window_escalates_with_consecutive_failures(self) -> None:
        now = _roster_probe.time.monotonic()
        # 2 failures -> 20s window (was 10s at 1 failure)
        _roster_probe._probe_failures["wsl"] = (2, now - 19.0)
        assert _roster_probe._probe_in_backoff("wsl") is True
        _roster_probe._probe_failures["wsl"] = (2, now - 21.0)
        assert _roster_probe._probe_in_backoff("wsl") is False

    def test_window_capped_at_300(self) -> None:
        now = _roster_probe.time.monotonic()
        # A large failure count would compute a huge window; it is clamped to 300s.
        _roster_probe._probe_failures["wsl"] = (100, now - 299.0)
        assert _roster_probe._probe_in_backoff("wsl") is True
        _roster_probe._probe_failures["wsl"] = (100, now - 301.0)
        assert _roster_probe._probe_in_backoff("wsl") is False

    def test_window_base_is_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The first re-probe gap follows the configured base (task #3507 lifted
        the schedule literals into config)."""
        from shared.config import settings

        monkeypatch.setattr(settings.gateway, "status_probe_backoff_base_seconds", 2.0)
        now = _roster_probe.time.monotonic()
        # 1 failure -> 2 * 2**1 = 4s window
        _roster_probe._probe_failures["wsl"] = (1, now - 3.9)
        assert _roster_probe._probe_in_backoff("wsl") is True
        _roster_probe._probe_failures["wsl"] = (1, now - 4.1)
        assert _roster_probe._probe_in_backoff("wsl") is False

    def test_window_cap_is_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The window ceiling follows the configured cap (task #3507 lifted the
        schedule literals into config)."""
        from shared.config import settings

        monkeypatch.setattr(settings.gateway, "status_probe_backoff_cap_seconds", 10.0)
        now = _roster_probe.time.monotonic()
        _roster_probe._probe_failures["wsl"] = (100, now - 9.0)
        assert _roster_probe._probe_in_backoff("wsl") is True
        _roster_probe._probe_failures["wsl"] = (100, now - 11.0)
        assert _roster_probe._probe_in_backoff("wsl") is False

    @pytest.mark.asyncio
    async def test_probe_skips_dispatch_while_in_backoff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """First unreachable probe records backoff; the immediate next probe is
        skipped without dialing the ops server."""
        from datetime import UTC, datetime

        from ops import cluster_rpc as cw

        calls: list[dict[str, object]] = []

        async def fake_dispatch(**kw: object) -> dict[str, object]:
            calls.append(kw)
            raise cw.ClusterOpUnreachable("down")

        monkeypatch.setattr(status_router._cluster_rpc, "dispatch_to_machine", fake_dispatch)
        last = datetime(2026, 5, 24, tzinfo=UTC)
        r1 = await status_router._probe_agent_runner(
            "wsl", ["agent-runner"], _OPS_URL, last, None, None
        )
        assert r1.online is False
        assert len(calls) == 1
        r2 = await status_router._probe_agent_runner(
            "wsl", ["agent-runner"], _OPS_URL, last, None, None
        )
        assert r2.online is False
        assert len(calls) == 1  # inside backoff window -> ops server NOT dialed again

    @pytest.mark.asyncio
    async def test_op_failed_is_reachable_no_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A reachable host whose status_probe op raised (ClusterOpFailed) is not
        backed off: the next poll dials it again so the real error keeps surfacing."""
        from datetime import UTC, datetime

        from ops import cluster_rpc as cw

        calls: list[dict[str, object]] = []

        async def fake_dispatch(**kw: object) -> dict[str, object]:
            calls.append(kw)
            raise cw.ClusterOpFailed({"error": "schema drift"})

        monkeypatch.setattr(status_router._cluster_rpc, "dispatch_to_machine", fake_dispatch)
        last = datetime(2026, 5, 24, tzinfo=UTC)
        first = await status_router._probe_agent_runner(
            "wsl", ["agent-runner"], _OPS_URL, last, None, None
        )
        assert first.online is True
        assert first.paused is None
        assert "wsl" not in _roster_probe._probe_failures
        second = await status_router._probe_agent_runner(
            "wsl", ["agent-runner"], _OPS_URL, last, None, None
        )
        assert second.online is True
        assert second.paused is None
        assert len(calls) == 2  # reachable -> dialed again, not skipped


class TestFastFailBudget:
    """Task #3507: a machine that already carries reachability failures gets the
    fast-fail budget for its detached recovery dial, so a blackholed re-dial
    cannot drag the whole-table read past the CLI/UI budget; first contact keeps
    the full anti-false-offline budget."""

    def test_budget_selection_is_state_driven(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from shared.config import settings

        monkeypatch.setattr(settings.gateway, "status_probe_timeout_seconds", 11.0)
        monkeypatch.setattr(settings.gateway, "status_probe_fastfail_timeout_seconds", 2.0)
        assert _roster_probe._probe_budget_s("wsl") == 11.0
        _roster_probe._probe_failures["wsl"] = (1, 0.0)
        assert _roster_probe._probe_budget_s("wsl") == 2.0
        _roster_probe._note_probe_reachable("wsl")
        assert _roster_probe._probe_budget_s("wsl") == 11.0

    @pytest.mark.asyncio
    async def test_first_contact_uses_full_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No failure record yet -> the dial goes out under the full budget (the
        anti-jitter margin, task #1200)."""
        from datetime import UTC, datetime

        from shared.config import settings

        monkeypatch.setattr(settings.gateway, "status_probe_timeout_seconds", 11.0)
        monkeypatch.setattr(settings.gateway, "status_probe_fastfail_timeout_seconds", 2.0)
        seen: dict[str, object] = {}

        async def fake_enqueue(*_a: object, **_kw: object) -> dict[str, object]:
            seen["timeout_s"] = _kw.get("timeout_s")
            return {
                "machine_name": "wsl",
                "serve_gateway": False,
                "serve_agent_runner": True,
                "paused": False,
            }

        monkeypatch.setattr(status_router._cluster_rpc, "dispatch_to_machine", fake_enqueue)
        r = await status_router._probe_agent_runner(
            "wsl", ["agent-runner"], _OPS_URL, datetime(2026, 5, 24, tzinfo=UTC), None, None
        )
        assert seen["timeout_s"] == 11.0
        assert r.online is True

    @pytest.mark.asyncio
    async def test_first_contact_blackhole_keeps_full_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A first-contact blackhole is cancelled at the full budget, not the
        fast-fail one — the anti-false-offline margin survives the change."""
        from datetime import UTC, datetime

        from shared.config import settings

        monkeypatch.setattr(settings.gateway, "status_probe_timeout_seconds", 0.05)
        monkeypatch.setattr(settings.gateway, "status_probe_fastfail_timeout_seconds", 0.005)

        async def blackhole(**_kw: object) -> dict[str, object]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        monkeypatch.setattr(status_router._cluster_rpc, "dispatch_to_machine", blackhole)
        started = time.monotonic()
        r = await asyncio.wait_for(
            status_router._probe_agent_runner(
                "wsl", ["agent-runner"], _OPS_URL, datetime(2026, 5, 24, tzinfo=UTC), None, None
            ),
            timeout=0.5,
        )
        assert r.online is False
        assert time.monotonic() - started >= 0.03  # the full budget, not the fast-fail one
        assert _roster_probe._probe_failures["wsl"][0] == 1


class TestDetachedRecoveryDial:
    """Task #3507: a known-down host is never dialed on the read path — the read
    serves the cached offline row while a single-flight detached dial carries the
    recovery check under the fast-fail budget, paced by the failure backoff."""

    def _record_spawns(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """Record detached-dial spawns without starting a thread, holding the
        in-flight slot exactly as a dial that has not landed yet would."""
        spawned: list[str] = []

        def fake_start(name: str, ops_url: str) -> None:
            spawned.append(name)

        monkeypatch.setattr(_roster_probe, "_start_recovery_thread", fake_start)
        return spawned

    @pytest.mark.asyncio
    async def test_in_window_read_serves_cached_offline_without_any_dial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A known-down host inside its backoff window: the read returns the
        cached offline row promptly, with neither an inline dial nor a detached
        one."""
        from datetime import UTC, datetime

        spawned = self._record_spawns(monkeypatch)

        async def must_not_dispatch(*_a: object, **_kw: object) -> dict[str, object]:
            raise AssertionError("a known-down host must not be dialed inline")

        monkeypatch.setattr(status_router._cluster_rpc, "dispatch_to_machine", must_not_dispatch)
        _roster_probe._probe_failures["wsl"] = (1, time.monotonic())  # fresh failure

        started = time.monotonic()
        row = await status_router._probe_agent_runner(
            "wsl", ["agent-runner"], _OPS_URL, datetime(2026, 5, 24, tzinfo=UTC), None, None
        )
        assert row.online is False
        assert spawned == []
        assert time.monotonic() - started < 0.5

    @pytest.mark.asyncio
    async def test_window_elapsed_reads_kick_exactly_one_detached_dial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N reads with the window elapsed: every read returns fast and the host
        gets exactly ONE detached dial — reads while it is in flight never stack
        a second (single-flight, task #4363)."""
        from datetime import UTC, datetime

        spawned = self._record_spawns(monkeypatch)

        async def must_not_dispatch(*_a: object, **_kw: object) -> dict[str, object]:
            raise AssertionError("a known-down host must not be dialed inline")

        monkeypatch.setattr(status_router._cluster_rpc, "dispatch_to_machine", must_not_dispatch)
        _roster_probe._probe_failures["wsl"] = (1, 0.0)  # window long elapsed

        started = time.monotonic()
        for _ in range(5):
            row = await status_router._probe_agent_runner(
                "wsl", ["agent-runner"], _OPS_URL, datetime(2026, 5, 24, tzinfo=UTC), None, None
            )
            assert row.online is False
        assert time.monotonic() - started < 1.0  # no read waited on any dial
        assert spawned == ["wsl"]
        assert _roster_probe._recovery_inflight == {"wsl"}

    def test_detached_success_clears_and_the_next_read_dials_first_contact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A landed detached dial that reached the host clears the record; the
        next read dials fresh as a first contact — full budget, and online."""
        from datetime import UTC, datetime

        from shared.config import settings

        monkeypatch.setattr(settings.gateway, "status_probe_timeout_seconds", 7.0)
        monkeypatch.setattr(settings.gateway, "status_probe_fastfail_timeout_seconds", 0.5)

        async def reachable(*_a: object, **_kw: object) -> dict[str, object]:
            return {
                "machine_name": "wsl",
                "serve_gateway": False,
                "serve_agent_runner": True,
                "paused": False,
            }

        monkeypatch.setattr(_roster_probe, "dispatch_status_probe", reachable)
        _roster_probe._probe_failures["wsl"] = (1, 0.0)
        _roster_probe._recovery_inflight.add("wsl")

        _roster_probe._run_recovery_dial("wsl", _OPS_URL)  # the detached dial lands

        assert "wsl" not in _roster_probe._probe_failures
        assert _roster_probe._recovery_inflight == set()

        seen: dict[str, object] = {}

        async def inline(
            name: str, ops_url: str, *, timeout_s: float | None = None
        ) -> dict[str, object]:
            seen["timeout_s"] = timeout_s
            return await reachable()

        monkeypatch.setattr(_roster_probe, "dispatch_status_probe", inline)
        row = asyncio.run(
            status_router._probe_agent_runner(
                "wsl", ["agent-runner"], _OPS_URL, datetime(2026, 5, 24, tzinfo=UTC), None, None
            )
        )
        assert row.online is True
        assert seen["timeout_s"] == 7.0  # first contact again — the full margin

    def test_detached_failure_widens_the_window_per_schedule(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A landed detached dial that timed out widens the record exactly as the
        inline re-dial did: failures+1 with the window at min(5 * 2**n, 300)."""
        from ops import cluster_rpc as rpc

        async def unreachable(*_a: object, **_kw: object) -> dict[str, object]:
            raise rpc.ClusterOpUnreachable("blackholed")

        monkeypatch.setattr(_roster_probe, "dispatch_status_probe", unreachable)
        _roster_probe._probe_failures["wsl"] = (1, 0.0)
        _roster_probe._recovery_inflight.add("wsl")

        _roster_probe._run_recovery_dial("wsl", _OPS_URL)

        failures, _last_attempt = _roster_probe._probe_failures["wsl"]
        assert failures == 2
        assert _roster_probe._recovery_inflight == set()
        # Freshly stamped: the 20s window (5 * 2**2) defers the next dial...
        assert _roster_probe._probe_in_backoff("wsl") is True
        # ...until it elapses.
        _roster_probe._probe_failures["wsl"] = (2, time.monotonic() - 21.0)
        assert _roster_probe._probe_in_backoff("wsl") is False
        # The schedule keeps doubling up to the 300s cap: min(5 * 2**6, 300) = 300.
        _roster_probe._probe_failures["wsl"] = (6, time.monotonic() - 299.0)
        assert _roster_probe._probe_in_backoff("wsl") is True
        _roster_probe._probe_failures["wsl"] = (6, time.monotonic() - 301.0)
        assert _roster_probe._probe_in_backoff("wsl") is False

    def test_detached_unexpected_failure_leaves_state_for_the_next_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unexpected (non-transport) failure is not a reachability verdict:
        it leaves the record untouched for the next window and still releases
        the in-flight slot."""

        async def boom(*_a: object, **_kw: object) -> dict[str, object]:
            raise ValueError("not a transport outcome")

        monkeypatch.setattr(_roster_probe, "dispatch_status_probe", boom)
        _roster_probe._probe_failures["wsl"] = (1, 0.0)
        _roster_probe._recovery_inflight.add("wsl")

        _roster_probe._run_recovery_dial("wsl", _OPS_URL)

        assert _roster_probe._probe_failures["wsl"][0] == 1  # untouched
        assert _roster_probe._recovery_inflight == set()  # slot always released

    def test_transition_window_simulation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Simulated mba-transition window (tasks #3506/#3507): one host
        blackholed beside one healthy peer. Assertions: (a) before the window
        elapses any number of reads never dial; (b) window expiry kicks exactly
        one detached dial while the reads stay dial-free; (c) a landed success
        clears the record and the next read is online as a first contact;
        (d) a landed failure renews the window per min(5 * 2**n, cap)."""
        from datetime import UTC, datetime

        from ops import cluster_rpc as rpc
        from shared.config import settings

        monkeypatch.setattr(settings.gateway, "status_probe_timeout_seconds", 0.05)
        monkeypatch.setattr(settings.gateway, "status_probe_fastfail_timeout_seconds", 0.05)

        clock = {"now": 1000.0}

        class _Clock:
            @staticmethod
            def monotonic() -> float:
                return clock["now"]

        monkeypatch.setattr(_roster_probe, "time", _Clock)
        spawned = self._record_spawns(monkeypatch)

        blackholed = {"on": True}
        calls: list[tuple[str, float | None]] = []

        async def probe(
            name: str, ops_url: str, *, timeout_s: float | None = None
        ) -> dict[str, object]:
            calls.append((name, timeout_s))
            if name == "mba" and blackholed["on"]:
                raise rpc.ClusterOpUnreachable("blackholed")
            return {
                "machine_name": name,
                "serve_gateway": False,
                "serve_agent_runner": True,
                "paused": False,
            }

        monkeypatch.setattr(_roster_probe, "dispatch_status_probe", probe)

        last = datetime(2026, 5, 24, tzinfo=UTC)
        rows: list[
            tuple[str, str | None, list[str], datetime, str | None, datetime | None, bool]
        ] = [
            ("mba", _OPS_URL, ["agent-runner"], last, None, None, False),
            ("wsl", _OPS_URL, ["agent-runner"], last, None, None, False),
        ]

        # Transition: the first read detects the blackholed mba (full budget),
        # records the failure, and still serves both rows.
        by_name = {
            m.name: m for m in asyncio.run(status_router.gather_cluster_status(rows, "cloud-test"))
        }
        assert by_name["mba"].online is False
        assert by_name["wsl"].online is True
        assert calls == [("mba", 0.05), ("wsl", 0.05)]
        assert _roster_probe._probe_failures["mba"][0] == 1

        # (a) Any number of reads inside the 10s window: not a single mba dial.
        clock["now"] += 3.0
        for _ in range(3):
            asyncio.run(status_router.gather_cluster_status(rows, "cloud-test"))
        assert [c for c in calls if c[0] == "mba"] == [("mba", 0.05)]
        assert spawned == []

        # (b) Window elapsed (11s > 10s): reads stay dial-free and exactly one
        # detached dial goes out, however many reads follow while it is in flight.
        clock["now"] += 8.0
        for _ in range(3):
            asyncio.run(status_router.gather_cluster_status(rows, "cloud-test"))
        assert [c for c in calls if c[0] == "mba"] == [("mba", 0.05)]
        assert spawned == ["mba"]
        assert _roster_probe._recovery_inflight == {"mba"}

        # (d) The landed dial fails (still down): window(2) = 20s is renewed and
        # the next kick is deferred until it elapses.
        _roster_probe._run_recovery_dial("mba", _OPS_URL)
        assert _roster_probe._probe_failures["mba"][0] == 2
        clock["now"] += 19.0
        asyncio.run(status_router.gather_cluster_status(rows, "cloud-test"))
        assert spawned == ["mba"]  # deferred inside the window
        clock["now"] += 2.0  # 21s past the failure now
        asyncio.run(status_router.gather_cluster_status(rows, "cloud-test"))
        assert spawned == ["mba", "mba"]
        assert _roster_probe._recovery_inflight == {"mba"}

        # (c) The host comes back: the landed dial clears the record and the
        # next read dials it fresh — first contact, full budget, online.
        blackholed["on"] = False
        _roster_probe._run_recovery_dial("mba", _OPS_URL)
        assert "mba" not in _roster_probe._probe_failures
        calls.clear()
        by_name = {
            m.name: m for m in asyncio.run(status_router.gather_cluster_status(rows, "cloud-test"))
        }
        assert by_name["mba"].online is True
        assert calls == [("mba", 0.05), ("wsl", 0.05)]

    @pytest.mark.asyncio
    async def test_window_elapsed_read_does_not_wait_for_the_recovery_dial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Behavioural guard: with the window elapsed, the read returns the
        cached row immediately — the recovery dial (fast-fail budget below) is
        not on this read's path. Before the fix the same read spent the full
        fast-fail budget inline."""
        from datetime import UTC, datetime

        from shared.config import settings

        monkeypatch.setattr(settings.gateway, "status_probe_timeout_seconds", 1.0)
        monkeypatch.setattr(settings.gateway, "status_probe_fastfail_timeout_seconds", 0.4)

        async def blackhole(**_kw: object) -> dict[str, object]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        monkeypatch.setattr(status_router._cluster_rpc, "dispatch_to_machine", blackhole)
        _roster_probe._probe_failures["wsl"] = (1, 0.0)  # window long elapsed

        started = time.monotonic()
        row = await status_router._probe_agent_runner(
            "wsl", ["agent-runner"], _OPS_URL, datetime(2026, 5, 24, tzinfo=UTC), None, None
        )
        elapsed = time.monotonic() - started
        assert row.online is False
        assert elapsed < 0.2  # the fast-fail budget was NOT spent on this read
        # The detached dial lands on its own; wait it out so it cannot leak into
        # the next test's module state.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _roster_probe._recovery_inflight:
            await asyncio.sleep(0.01)
        assert _roster_probe._recovery_inflight == set()

    def test_spawn_failure_never_fails_the_read_and_never_leaks_the_slot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A spawn refusal (e.g. the OS declining a new thread) is best-effort:
        the read still serves the cached offline row, the in-flight slot is
        released, and the next kick can spawn again (QA #3242, PR #3118)."""
        from datetime import UTC, datetime

        def broken_start(name: str, ops_url: str) -> None:
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(_roster_probe, "_start_recovery_thread", broken_start)
        _roster_probe._probe_failures["wsl"] = (1, 0.0)  # window long elapsed

        row = asyncio.run(
            status_router._probe_agent_runner(
                "wsl", ["agent-runner"], _OPS_URL, datetime(2026, 5, 24, tzinfo=UTC), None, None
            )
        )
        assert row.online is False  # the read survived the spawn refusal
        assert _roster_probe._recovery_inflight == set()  # no leaked slot

        spawned: list[str] = []

        def working_start(name: str, ops_url: str) -> None:
            spawned.append(name)

        monkeypatch.setattr(_roster_probe, "_start_recovery_thread", working_start)
        row = asyncio.run(
            status_router._probe_agent_runner(
                "wsl", ["agent-runner"], _OPS_URL, datetime(2026, 5, 24, tzinfo=UTC), None, None
            )
        )
        assert row.online is False
        assert spawned == ["wsl"]  # the next kick spawns again

    def test_real_thread_single_flight_and_convergence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real thread path: a kick starts one daemon dial; while it is in
        flight further kicks are refused; its landed success clears the record
        and releases the in-flight slot."""
        calls: list[str] = []

        async def probe(
            name: str, ops_url: str, *, timeout_s: float | None = None
        ) -> dict[str, object]:
            calls.append(name)
            await asyncio.sleep(0.3)  # hold the slot long enough to re-kick
            return {
                "machine_name": name,
                "serve_gateway": False,
                "serve_agent_runner": True,
                "paused": False,
            }

        monkeypatch.setattr(_roster_probe, "dispatch_status_probe", probe)
        _roster_probe._probe_failures["wsl"] = (1, 0.0)

        _roster_probe._maybe_kick_recovery_dial("wsl", _OPS_URL)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not calls:
            time.sleep(0.01)
        assert calls == ["wsl"]
        _roster_probe._maybe_kick_recovery_dial("wsl", _OPS_URL)  # in flight -> refused
        assert calls == ["wsl"]
        while time.monotonic() < deadline and (
            "wsl" in _roster_probe._probe_failures or _roster_probe._recovery_inflight
        ):
            time.sleep(0.01)
        assert "wsl" not in _roster_probe._probe_failures
        assert _roster_probe._recovery_inflight == set()


class TestPinVerdict:
    def test_no_pin_is_none(self) -> None:
        assert _roster_rows.pin_verdict("abc1234", None) is None

    def test_unknown_head_is_none(self) -> None:
        assert _roster_rows.pin_verdict(None, "abc1234") is None

    def test_on_pin_true(self) -> None:
        assert _roster_rows.pin_verdict("abc1234", "abc1234") is True

    def test_off_pin_false(self) -> None:
        assert _roster_rows.pin_verdict("abc1234", "def5678") is False


class TestClusterPinInPanel:
    def test_panel_carries_pin_and_local_on_pin(
        self,
        db_conn: psycopg.Connection,
        fake_flag: Path,
        stub_machine_identity: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_get_cluster_status reads the pin once → ClusterPanel.cluster_target_sha
        + each machine's on_pin verdict (local pure-gateway row's head from the
        lightweight prod_source_head_sha read)."""
        _ = fake_flag, stub_machine_identity
        _insert_machine(db_conn, "cloud-test", "https://ava.example.com", "gateway")
        monkeypatch.setattr("shared.cluster_pin.get_cluster_target_sha", lambda: "abc1234")
        monkeypatch.setattr(status_router, "prod_source_head_sha", lambda: "abc1234")
        with db_conn.cursor() as cur:
            panel = status_router._get_cluster_status(cur)
        assert panel.cluster_target_sha == "abc1234"
        by_name = {m.name: m for m in panel.machines}
        assert by_name["cloud-test"].head_sha == "abc1234"
        assert by_name["cloud-test"].on_pin is True

    def test_local_off_pin_false(
        self,
        db_conn: psycopg.Connection,
        fake_flag: Path,
        stub_machine_identity: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _ = fake_flag, stub_machine_identity
        _insert_machine(db_conn, "cloud-test", "https://ava.example.com", "gateway")
        monkeypatch.setattr("shared.cluster_pin.get_cluster_target_sha", lambda: "abc1234")
        monkeypatch.setattr(status_router, "prod_source_head_sha", lambda: "def5678")
        with db_conn.cursor() as cur:
            panel = status_router._get_cluster_status(cur)
        assert panel.machines[0].on_pin is False

    def test_no_pin_leaves_on_pin_none(
        self,
        db_conn: psycopg.Connection,
        fake_flag: Path,
        stub_machine_identity: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _ = fake_flag, stub_machine_identity
        _insert_machine(db_conn, "cloud-test", "https://ava.example.com", "gateway")
        monkeypatch.setattr("shared.cluster_pin.get_cluster_target_sha", lambda: None)
        monkeypatch.setattr(status_router, "prod_source_head_sha", lambda: "def5678")
        with db_conn.cursor() as cur:
            panel = status_router._get_cluster_status(cur)
        assert panel.cluster_target_sha is None
        assert panel.machines[0].on_pin is None
