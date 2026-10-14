"""`/api/cluster/*` endpoint + 503 middleware + handler unit tests.

Middleware short-circuits SDK paths only during the journal's stop/start window;
native drain keeps those dependencies available. Session
backends are recorded, while local drain uses the private test database.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

import base.sessions.backend as sessions_backend
import gateway.app as gateway_app
import gateway.cluster.status as gateway_cluster_status
from base import cluster
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.deploy.git import cluster_drift, running_sha
from base.deploy.lifecycle.start_serving import RootBirth
from base.deploy.maintenance import admission, pause_owner
from base.deploy.maintenance.state import MaintenancePhase
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.native_process import loaded_commit
from base.native_process.loaded_commit import LoadedCommit
from base.native_process.root_control import client as root_client
from gateway.app import app
from gateway.cluster import router
from gateway.events import telemetry_rows
from gateway.http.middleware.cors import cors_allowed_origins
from gateway.tests.agents.gateway_auth import without_credentials
from ops import agent_pause, cluster_status
from ops.cluster_status import pause as cluster_pause


@pytest.fixture
def fake_admin_events(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    """Patch telemetry_rows.query_events for the admin-events route; record the
    kwargs and return canned rows."""

    calls: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    def _query(_conn: object, **kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
        calls.append(kwargs)
        return rows, False

    monkeypatch.setattr(telemetry_rows, "query_events", _query)
    return {"calls": calls, "rows": rows}


@pytest.fixture
def fake_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, machine_identity: MachineIdentity
) -> Path:
    """A tmp file standing in for the paused posture row, controllable by the tests.

    R1 (Task #1021) moved `is_paused` to the host_deploy_state posture row; the
    readers' bound names are shimmed to the file's existence so this suite keeps
    simulating the pause with a file. The fixture is about "the process agrees on
    one paused signal", not which storage (the old-signal sweep, PR5, retired the
    real flag file).
    The gateway middleware reads the local journal through the async
    `gateway.app.cluster_is_paused`, so its bound name uses an async stand-in;
    the status/cluster routers still read the sync `is_paused`."""
    flag = tmp_path / "cluster_paused"

    async def _paused(_request: object) -> bool:
        return flag.exists()

    def _snapshot_paused(
        _db: object, _state: object = None, *, machine_name: Callable[[], str]
    ) -> bool:
        assert machine_name == machine_identity.name
        return flag.exists()

    monkeypatch.setattr(gateway_app, "cluster_is_paused", _paused)

    def paused(_db: Database, *, machine_name: Callable[[], str]) -> bool:
        assert machine_name == machine_identity.name
        return flag.exists()

    monkeypatch.setattr(router, "cluster_is_paused", paused)
    monkeypatch.setattr(gateway_cluster_status, "cluster_is_paused", paused)
    monkeypatch.setattr(cluster_pause, "is_paused", _snapshot_paused)
    return flag


@pytest.fixture(autouse=True)
def pinned_session_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the session-name composer for gateway cluster tests so composed
    session names are deterministic regardless of the dev host config.
    Patches ``session_name`` directly so ``machine_name()``
    stay untouched (other gateway tests call them via ``set_machine_identity``
    + ``status_snapshot()`` and assert the injected value).
    Produces names like ``ava-test-agent-host``.

    ``session_name`` is patched at its source (``base.cluster``) because the
    naming scheme is one fact for the whole process — the ``ops`` cluster modules
    reach it through that module precisely so one setattr pins it for all of them.
    The pause tests patch ``base.sessions.backend.get_backend`` to a recording
    fake, so the composed names are what the assertions read.
    """
    monkeypatch.setattr(cluster, "session_name", lambda svc: f"ava-test-{svc}")  # pyright: ignore[reportUnknownArgumentType]


# ─── middleware 503 mode ──────────────────────────────────────────────────────


class TestPauseMiddleware:
    def test_sdk_path_returns_503_when_paused(self, fake_flag: Path) -> None:
        fake_flag.write_text("")
        with TestClient(app) as client:
            r = client.get("/api/agents")
        assert r.status_code == 503
        assert "updating" in r.json()["detail"]
        assert r.headers.get("Retry-After") == "30"

    def test_paused_503_carries_cors_headers(self, fake_flag: Path) -> None:
        """A 503 short-circuited by the pause middleware still carries the CORS
        headers — CORSMiddleware is the OUTERMOST middleware, so a browser
        caller sees the real 503 instead of "Failed to fetch" (#187)."""
        fake_flag.write_text("")
        allowed_origin = cors_allowed_origins()[0]
        with TestClient(app) as client:
            r = client.get("/api/agents", headers={"Origin": allowed_origin})
        assert r.status_code == 503
        assert r.headers["access-control-allow-origin"] == allowed_origin
        assert r.headers["access-control-allow-credentials"] == "true"

    def test_cluster_status_bypasses_503(self, fake_flag: Path, set_machine_identity) -> None:
        """`/api/cluster/status` still returns 200 while paused — it is the observability + control path."""
        fake_flag.write_text("")
        set_machine_identity(role="gateway", name="test-mc")
        with TestClient(app) as client:
            r = client.get("/api/cluster/status")
        assert r.status_code == 200
        body = r.json()
        assert body["paused"] is True
        # The pure-gateway local snapshot resolves paused from the posture row
        # alone; the breakdown names that single cause (task #3404).
        assert body["paused_reason"] == "business_pause"
        assert body["machine_name"] == "test-mc"
        assert body["serve_gateway"] is True
        assert body["serve_agent_runner"] is False
        # Retired updater projections are absent from the live status wire.
        assert "current_orchestration" not in body
        assert "last_updater_outcome" not in body

    def test_no_503_when_unpaused(self, fake_flag: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Flag absent → middleware does not block; /api/agents follows normal business path."""
        # flag absent by default
        with TestClient(app) as client:
            r = client.get("/api/agents")
        # just not 503 (business logic may return 200 / other, but must not be our paused middleware)
        assert r.status_code != 503

    def test_alerts_webhook_bypasses_503(self, fake_flag: Path) -> None:
        """The alert webhook must land during a rollout window — a
        503 there exhausts Grafana's retries and the alert is lost. The 401
        (no webhook token configured in this test env) proves the request
        reached the router instead of the pause short-circuit."""
        fake_flag.write_text("")
        with TestClient(app) as client:
            r = without_credentials(client).post(
                "/api/alerts",
                json={"status": "firing", "alerts": []},
            )
        assert r.status_code == 401

    def test_alerts_panel_still_503_when_paused(self, fake_flag: Path) -> None:
        """Only the webhook bypasses the pause — the read/stream paths stay
        503 like every other path (read-only, not needed mid-rollout)."""
        fake_flag.write_text("")
        with TestClient(app) as client:
            r = client.get("/api/alerts")
        assert r.status_code == 503

    def test_openapi_disabled(self, fake_flag: Path) -> None:
        """FastAPI metadata (`/openapi.json` / `/docs` / `/redoc`) is turned off
        at app construction so it can't leak the full route schema. Unpaused
        the route is FastAPI 404; paused, /api/cluster/* is the only bypass
        and metadata returns 503 along with everything else (correct — there's
        nothing to serve and no caller depends on this path)."""
        with TestClient(app) as client:
            r = client.get("/openapi.json")
        assert r.status_code == 404

    def test_other_agent_paths_still_503_when_paused(self, fake_flag: Path) -> None:
        """Only the agent SELF-REPORT paths bypass the pause — an externally
        initiated terminate stays 503 (business logic, not a drain signal)."""

    def test_alwk_does_not_claim_while_paused(
        self, fake_flag: Path, db_conn: psycopg.Connection
    ) -> None:
        """Middleware order (audit P2-1): a paused cluster 503s BEFORE dedup
        engages — a pause-window ALWK request must not leave a placeholder
        row. The old registration order ran dedup first, so every
        pause-window request claimed then 503'd (an INSERT+DELETE per
        request, and a bricked key if the process died inside the window)."""
        with db_conn.cursor() as cur:
            cur.execute("INSERT INTO agents (label) VALUES ('pause-order') RETURNING id")
            row = cur.fetchone()
            assert row is not None
            aid = row[0]
            cur.execute("INSERT INTO agents_meta (id, status) VALUES (%s, 'running')", (aid,))
        db_conn.commit()
        fake_flag.write_text("")
        with TestClient(app) as client:
            r = client.post(
                f"/api/agents/{aid}/messages",
                json={"content": "during pause", "source": "user"},
                headers={"Idempotency-Key": "key-pause-order"},
            )
        assert r.status_code == 503
        with db_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM api_idempotency WHERE key = %s", ("key-pause-order",))
            row = cur.fetchone()
            assert row is not None and row[0] == 0, "dedup must not claim while paused"
        fake_flag.write_text("")
        with TestClient(app) as client:
            r = client.post("/api/agents/123/terminate", json={"source": "user"})
        assert r.status_code == 503


# ─── handler business logic ───────────────────────────────────────────────────


class _FakeSessionBackend:
    """Record every service and orchestration session operation without spawning."""

    def __init__(self) -> None:
        self.killed: list[str] = []
        self.spawned: list[str] = []
        self.spawn_calls: list[tuple[str, str, object]] = []
        self.alive_answer: bool | list[bool] = False
        self.alive_by_name: dict[str, bool] = {}
        self.spawn_ok = True
        self.spawn_ok_by_name: dict[str, bool] = {}

    def _alive(self, name: str) -> bool:
        if name in self.alive_by_name:
            return self.alive_by_name[name]
        if isinstance(self.alive_answer, list):
            return self.alive_answer.pop(0)
        return self.alive_answer

    def has_session(self, name: str) -> bool:
        return self._alive(name)

    def kill_session(
        self, name: str, *, graceful: bool = False, expected: bool = False, **_: object
    ) -> tuple[bool, str]:
        self.killed.append(name)
        return True, "stub"

    def new_session(self, name: str, cmd: str, cwd: object, *, env: object, **_: object) -> bool:
        self.spawned.append(name)
        self.spawn_calls.append((name, cmd, cwd))
        return self.spawn_ok_by_name.get(name, self.spawn_ok)

    def list_sessions(self, prefix: str = "") -> list[str]:
        return []


@pytest.fixture
def pause_backend(monkeypatch: pytest.MonkeyPatch) -> _FakeSessionBackend:
    backend = _FakeSessionBackend()
    monkeypatch.setattr(sessions_backend, "get_backend", lambda: backend)
    monkeypatch.setattr(agent_pause, "host_running", lambda: False)
    return backend


class TestPauseAgents:
    @pytest.fixture(autouse=True)
    def _private_pause_owner(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
        monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")

    def test_completed_drain_keeps_sdk_requests_and_services_available(
        self,
        pause_backend: _FakeSessionBackend,
        database: Database,
        event_bus: EventBus,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
        """Phase A may finish while a peer still needs this gateway's SDK API."""
        pause_backend.alive_answer = True

        agent_pause.pause_agents(database, event_bus, local_machine=machine_identity)

        current = admission.snapshot()
        assert current is not None and current.maintenance is not None
        assert current.maintenance.phase == MaintenancePhase.DRAINED
        with TestClient(app) as client:
            assert client.get("/api/agents").status_code == 200
        assert not cluster_pause.is_paused(database, machine_name=machine_identity.name)
        assert pause_backend.killed == []
        assert pause_backend.has_session("ava-test-agent-host")

    def test_idempotent_when_session_missing(
        self,
        pause_backend: _FakeSessionBackend,
        database: Database,
        event_bus: EventBus,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
        """Repeated Phase A reuses the same drain without starting services."""
        agent_pause.pause_agents(database, event_bus, local_machine=machine_identity)
        first = pause_owner.read()
        agent_pause.pause_agents(database, event_bus, local_machine=machine_identity)
        assert pause_owner.read() == first
        assert first.maintenance is not None and first.maintenance.phase == MaintenancePhase.DRAINED
        assert pause_backend.killed == pause_backend.spawned == []


class TestUnpauseLocalCluster:
    @pytest.fixture(autouse=True)
    def _private_pause_owner(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
        monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")

    def test_unpause_restores_posture_and_releases_admission(
        self,
        pause_backend: _FakeSessionBackend,
        database: Database,
        event_bus: EventBus,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
        from base.deploy.state.host_deploy_state import set_posture

        agent_pause.pause_agents(database, event_bus, local_machine=machine_identity)
        set_posture(database, "paused", machine=machine_identity.name())
        assert cluster_pause.is_paused(database, machine_name=machine_identity.name)

        cluster_pause.unpause_local_cluster(database, event_bus, local_machine=machine_identity)

        assert not cluster_pause.is_paused(database, machine_name=machine_identity.name)
        assert not admission.held()
        assert pause_backend.spawned == pause_backend.killed == []

    def test_missing_pause_and_repeated_resume_do_not_start_services(
        self,
        pause_backend: _FakeSessionBackend,
        database: Database,
        event_bus: EventBus,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
        cluster_pause.unpause_local_cluster(database, event_bus, local_machine=machine_identity)
        cluster_pause.unpause_local_cluster(database, event_bus, local_machine=machine_identity)
        assert not cluster_pause.is_paused(database, machine_name=machine_identity.name)
        assert pause_backend.spawned == pause_backend.killed == []


class TestRetiredDeploymentEndpoints:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/api/cluster/update"),
            ("POST", "/api/cluster/rollout"),
            ("POST", "/api/cluster/restart"),
            ("GET", "/api/cluster/update-check"),
            ("POST", "/api/cluster/recover"),
        ],
    )
    def test_removed_ingress_cannot_dispatch_or_pause(
        self, monkeypatch: pytest.MonkeyPatch, method: str, path: str
    ) -> None:
        """The retired update and recovery routes do not exist over HTTP."""

        def forbidden(_db: object, _bus: object, *_args: object, **_kwargs: object) -> None:
            pytest.fail("retired HTTP ingress reached the old updater")

        monkeypatch.setattr(agent_pause, "pause_agents", forbidden)
        with TestClient(app) as client:
            assert client.request(method, path).status_code == 404
        assert path not in app.openapi()["paths"]


class TestStatusSnapshot:
    def test_snapshot_reflects_flag_and_role(
        self,
        serving_root: RootBirth,
        fake_flag: Path,
        set_machine_identity,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
        from base.deploy.lifecycle import start_serving
        from base.deploy.state import host_deploy_state

        monkeypatch.setattr(start_serving, "run_dir", lambda: fake_flag.parent)
        set_machine_identity(role="agent-runner", name="runner-a")
        host_deploy_state.set_posture(database, "idle", machine=machine_identity.name())
        start_serving.mark_serving(start_serving.begin_start(), runtime=serving_root.runtime)
        # unpaused
        snap = cluster_status.status_snapshot(
            database,
            image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
            local_machine=machine_identity,
        )
        assert snap.machine_name == "runner-a"
        assert snap.serve_gateway is False
        assert snap.serve_agent_runner is True
        assert snap.paused is False
        # paused
        fake_flag.write_text("")
        snap2 = cluster_status.status_snapshot(
            database,
            image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
            local_machine=machine_identity,
        )
        assert snap2.paused is True

    def test_snapshot_includes_head_sha(
        self,
        set_machine_identity,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
        """status_snapshot threads the prod-source HEAD so the roster can compare
        it against the cluster pin."""
        set_machine_identity(role="agent-runner", name="runner-a")
        monkeypatch.setattr(cluster_drift, "prod_source_head_sha", lambda: "abc1234")
        assert (
            cluster_status.status_snapshot(
                database,
                image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
                local_machine=machine_identity,
            ).head_sha
            == "abc1234"
        )

    @pytest.mark.parametrize("sha", ["def5678", None])
    def test_snapshot_includes_running_sha(
        self,
        set_machine_identity,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        sha: str | None,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
        """status_snapshot threads the commit the answering process froze at its
        own boot — distinct from head_sha (the checkout the pin verdict compares)
        so the roster can expose a node running stale code even when its checkout
        reads on-pin."""
        set_machine_identity(role="agent-runner", name="runner-a")

        def unexpected_process_read(_root: Path) -> str | None:
            raise AssertionError("snapshot must use its supplied image")

        monkeypatch.setattr(loaded_commit, "capture_commit", unexpected_process_read)
        assert (
            cluster_status.status_snapshot(
                database,
                image=LoadedCommit(source_root=Path(__file__).parent, sha=sha),
                local_machine=machine_identity,
            ).running_sha
            == sha
        )

    def test_snapshot_ignores_the_start_bookmark(
        self,
        set_machine_identity,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
        """A start that restarted nothing must not be able to make this node look
        current.

        `ava start` writes `$AVA_HOME/running_sha` from the fresh HEAD one line
        before a launcher that skips already-running sessions, so after a
        no-op start the bookmark and the checkout agree while the live processes
        still hold the old code. Sourcing the snapshot from the bookmark made
        that state indistinguishable from a real restart — every roster consumer
        saw running_sha == head_sha for a node on days-old code. The snapshot
        answers from the process, so the stale commit survives the bookmark's
        advance and the divergence every drift renderer keys on is there."""
        set_machine_identity(role="agent-runner", name="runner-a")
        monkeypatch.setattr(running_sha, "get", lambda: "n3wn3w0bbbb")
        monkeypatch.setattr(cluster_drift, "prod_source_head_sha", lambda: "n3wn3w0bbbb")

        snap = cluster_status.status_snapshot(
            database,
            image=LoadedCommit(source_root=Path(__file__).parent, sha="0ld0ld0aaaa"),
            local_machine=machine_identity,
        )

        assert snap.running_sha == "0ld0ld0aaaa"
        assert snap.head_sha == "n3wn3w0bbbb"
        assert snap.running_sha != snap.head_sha

    @pytest.mark.parametrize("online", [True, False, None])
    def test_supervisor_status_preserves_unavailable_inspection(
        self,
        fake_flag: Path,
        set_machine_identity,
        monkeypatch: pytest.MonkeyPatch,
        online: bool | None,
        database: Database,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
        del fake_flag
        set_machine_identity(role="agent-runner", name="test-host")

        def inspect_root() -> object | None:
            if online is None:
                raise RuntimeError("native root inspection unavailable")
            return object() if online else None

        monkeypatch.setattr(root_client, "root_process", inspect_root)
        assert (
            cluster_status.status_snapshot(
                database,
                image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
                local_machine=machine_identity,
            ).supervisor_online
            is online
        )


# ─── cluster endpoints via TestClient ─────────────────────────────────────────


class TestClusterEndpoints:
    @pytest.mark.parametrize("route", ["/api/cluster/stop", "/api/cluster/resume"])
    def test_the_legacy_deploy_lease_transition_is_gone(self, route: str) -> None:
        """The legacy lease-bound stop/resume routes and their ops kinds no longer exist."""
        with TestClient(app) as client:
            r = client.post(
                route,
                json={"deploy_holder": "g:pid1", "deploy_acquired_at": "2026-08-25T00:00:00Z"},
            )
        assert r.status_code in {404, 405}

    def test_post_stopping_marks_machine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """POST /api/cluster/stopping?machine=<name>&home=<home> retracts that unit."""
        marked: list[tuple[str, str]] = []
        from ops.cluster import operations as ops_mod

        monkeypatch.setattr(
            ops_mod,
            "mark_stopping",
            lambda _db, name, home: marked.append((name, home)),  # pyright: ignore[reportUnknownArgumentType]
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/cluster/stopping", params={"machine": "runner-a", "home": "~/.ava"}
            )
        assert r.status_code == 200
        assert r.json() == {"machine": "runner-a"}
        assert marked == [("runner-a", "~/.ava")]


# ─── admin events query ──────────────────────────────────────────────────────


# ─── admin: DELETE /api/cluster/machines/{name} ──────────────────────────────


# ─── agent roster: GET /api/cluster/machines ─────────────────────────────────


# ─── machines pause / resume (Task #1283) ────────────────────────────────────


def seed_away_machine(
    db_conn: psycopg.Connection, *, name: str = "away", local_row: bool = True
) -> None:
    """Machines rows for the pause tests, isolated from other tests' rows: the
    paused target (`name`, agent-runner) plus, when `local_row`, this gateway
    host's own row (gateway-only in the DB — the roster's local lightweight
    path, so no probe dial; the test identity itself carries agent-runner so
    spawn_agent works)."""
    with db_conn.cursor() as cur:
        cur.execute("TRUNCATE machines")
        if local_row:
            cur.execute(
                "INSERT INTO machines (name, role, gateway_url) "
                "VALUES ('test-host', ARRAY['gateway'], NULL)"
            )
        cur.execute(
            "INSERT INTO machines (name, role, gateway_url) "
            "VALUES (%s, ARRAY['agent-runner'], NULL)",
            (name,),
        )
    db_conn.commit()


def seed_agent_on_machine(
    db_conn: psycopg.Connection,
    machine: str,
    *,
    status: str = "idling",
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> int:
    """One live agent row homed on `machine` (test spawn helper + machine
    stamp; the row-creation path moved gateway-side, Task #1236 follow-up)."""
    from base.agents.tests.units import spawn_agent

    aid = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET machine = %s, status = %s WHERE id = %s",
            (machine, status, aid),
        )
    db_conn.commit()
    return aid


def seed_drain_owner(db_conn: psycopg.Connection, agent_id: int = 405) -> None:
    """The drain-owner agent row the pause endpoint reassigns tasks to (the
    FK target `agent_tasks.owner -> agents(id)`)."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
            (agent_id,),
        )
    db_conn.commit()


def seed_in_progress_task(db_conn: psycopg.Connection, owner: int, title: str) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, status, owner, created_by) "
            "VALUES (%s, 'desc', 'in_progress', %s, 'user') RETURNING id",
            (title, owner),
        )
        row = cur.fetchone()
        assert row is not None
        (task_id,) = row
    db_conn.commit()
    return task_id
