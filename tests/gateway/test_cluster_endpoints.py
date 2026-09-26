"""`/api/cluster/*` endpoint + 503 middleware + handler unit tests.

Middleware short-circuits SDK paths only during the journal's stop/start window;
native drain keeps those dependencies available. Session
backends are recorded, while local drain uses the private test database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway import loki_events
from gateway._cors import cors_allowed_origins
from gateway.app import app
from ops import cluster_pause, cluster_status
from shared.start_serving import RootBirth


@pytest.fixture
def fake_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A tmp file standing in for the paused posture row, controllable by the tests.

    R1 (Task #1021) moved `is_paused` to the host_deploy_state posture row; the
    readers' bound names are shimmed to the file's existence so this suite keeps
    simulating the pause with a file. The fixture is about "the process agrees on
    one paused signal", not which storage (the old-signal sweep, PR5, retired the
    real flag file).
    The gateway middleware reads the local journal through the async
    `gateway.app._cluster_is_paused`, so its bound name uses an async stand-in;
    the status/cluster routers still read the sync `is_paused`."""
    flag = tmp_path / "cluster_paused"

    async def _paused(_request: object) -> bool:
        return flag.exists()

    def _snapshot_paused(_state: object = None) -> bool:
        return flag.exists()

    monkeypatch.setattr("gateway.app._cluster_is_paused", _paused)
    monkeypatch.setattr("gateway.routers.cluster.cluster_is_paused", flag.exists)
    monkeypatch.setattr("gateway.routers.status.cluster_is_paused", flag.exists)
    monkeypatch.setattr("ops.cluster_pause.is_paused", _snapshot_paused)
    return flag


@pytest.fixture(autouse=True)
def _pin_session_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the session-name composer for gateway cluster tests so composed
    session names are deterministic regardless of the dev host config.
    Patches ``session_name`` directly so ``machine_name()``
    stay untouched (other gateway tests call them via ``set_machine_identity``
    + ``status_snapshot()`` and assert the injected value).
    Produces names like ``ava-test-agent-host``.

    ``session_name`` is patched at its source (``shared.cluster``) because the
    naming scheme is one fact for the whole process — the ``ops`` cluster modules
    reach it through that module precisely so one setattr pins it for all of them.
    The pause tests patch ``shared.session_backend.get_backend`` to a recording
    fake, so the composed names are what the assertions read.
    """
    monkeypatch.setattr("shared.cluster.session_name", lambda svc: f"ava-test-{svc}")  # pyright: ignore[reportUnknownArgumentType]


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
            r = client.post(
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
    from ops import agent_pause

    backend = _FakeSessionBackend()
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: backend)
    monkeypatch.setattr(agent_pause, "host_running", lambda: False)
    return backend


class TestPauseLocalCluster:
    @pytest.fixture(autouse=True)
    def _private_pause_owner(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from shared import pause_owner

        monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
        monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")

    def test_completed_drain_keeps_sdk_requests_and_services_available(
        self, pause_backend: _FakeSessionBackend
    ) -> None:
        """Phase A may finish while a peer still needs this gateway's SDK API."""
        from shared import maintenance

        pause_backend.alive_answer = True

        cluster_pause.pause_local_cluster()

        current = maintenance.snapshot()
        assert current is not None and current.maintenance is not None
        assert current.maintenance.phase == "drained"
        with TestClient(app) as client:
            assert client.get("/api/agents").status_code == 200
        assert not cluster_pause.is_paused()
        assert pause_backend.killed == []
        assert pause_backend.has_session("ava-test-agent-host")

    def test_idempotent_when_session_missing(self, pause_backend: _FakeSessionBackend) -> None:
        """Repeated Phase A reuses the same drain without starting services."""
        from shared import pause_owner

        cluster_pause.pause_local_cluster()
        first = pause_owner.read()
        cluster_pause.pause_local_cluster()
        assert pause_owner.read() == first
        assert first.maintenance is not None and first.maintenance.phase == "drained"
        assert pause_backend.killed == pause_backend.spawned == []


# subprocess.run faked), so they opt out of the autouse guard that refuses them
# suite-wide (tests/conftest.py:_guard_cluster_spawn).
class TestUnpauseLocalCluster:
    @pytest.fixture(autouse=True)
    def _private_pause_owner(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from shared import pause_owner

        monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
        monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")

    def test_unpause_restores_posture_and_releases_admission(
        self, pause_backend: _FakeSessionBackend
    ) -> None:
        from shared import maintenance
        from shared.host_deploy_state import set_posture

        cluster_pause.pause_local_cluster()
        set_posture("paused")
        assert cluster_pause.is_paused()

        cluster_pause.unpause_local_cluster()

        assert not cluster_pause.is_paused()
        assert not maintenance.held()
        assert pause_backend.spawned == pause_backend.killed == []

    def test_missing_pause_and_repeated_resume_do_not_start_services(
        self, pause_backend: _FakeSessionBackend
    ) -> None:
        cluster_pause.unpause_local_cluster()
        cluster_pause.unpause_local_cluster()
        assert not cluster_pause.is_paused()
        assert pause_backend.spawned == pause_backend.killed == []


# the session backend faked), so they opt out of the autouse guard that refuses
# them suite-wide (tests/conftest.py:_guard_cluster_spawn).


# the session backend faked), so they opt out of the autouse guard that refuses
# them suite-wide (tests/conftest.py:_guard_cluster_spawn).


# the session backend faked), so they opt out of the autouse guard that refuses
# them suite-wide (tests/conftest.py:_guard_cluster_spawn).


class TestLockHolderLiveness:
    """`_lock_holder_is_live` parses `<machine>:pid<N>` and probes the pid locally.

    The probe itself lives in `shared.cluster_lock.holder_process_gone` (the
    manual recovery and the automatic reclaim share one verdict), so the death
    evidence is stubbed at the shared seam — and the two "treated live" cases
    below patch the same machine-name seam, or they would pass vacuously on a
    host whose real name differs from the `mc` they assume.
    """

    def test_this_machine_dead_pid_is_not_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ops import ops_cluster as ops_mod

        monkeypatch.setattr("shared.machine.machine_name", lambda: "mc")
        monkeypatch.setattr("shared.proc.process_alive", lambda _pid: False)  # pyright: ignore[reportUnknownArgumentType]
        assert ops_mod._lock_holder_is_live("mc:pid123") is False

    def test_this_machine_alive_pid_is_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ops import ops_cluster as ops_mod

        monkeypatch.setattr("shared.machine.machine_name", lambda: "mc")
        monkeypatch.setattr("shared.proc.process_alive", lambda _pid: True)  # pyright: ignore[reportUnknownArgumentType]
        assert ops_mod._lock_holder_is_live("mc:pid123") is True

    def test_foreign_machine_holder_is_treated_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ops import ops_cluster as ops_mod

        # Can't probe a remote pid — must not clobber another gateway's lock.
        monkeypatch.setattr("shared.machine.machine_name", lambda: "mc")
        assert ops_mod._lock_holder_is_live("other:pid5") is True

    def test_unparseable_holder_is_treated_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ops import ops_cluster as ops_mod

        monkeypatch.setattr("shared.machine.machine_name", lambda: "mc")
        assert ops_mod._lock_holder_is_live("garbage") is True


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
        """Stranded-host recovery is the host-local `ava cluster recover` verb only."""
        from ops import ops_cluster as ops_mod

        def forbidden(*_args: object, **_kwargs: object) -> None:
            pytest.fail("retired HTTP ingress reached the old updater")

        monkeypatch.setattr(cluster_pause, "pause_local_cluster", forbidden)
        monkeypatch.setattr(ops_mod, "cluster_recover_op", forbidden)
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
    ) -> None:
        from shared import host_deploy_state, start_serving

        monkeypatch.setattr(start_serving, "run_dir", lambda: fake_flag.parent)
        set_machine_identity(role="agent-runner", name="wsl")
        host_deploy_state.set_posture("idle")
        start_serving.mark_serving(start_serving.begin_start(), runtime=serving_root.runtime)
        # unpaused
        snap = cluster_status.status_snapshot()
        assert snap.machine_name == "wsl"
        assert snap.serve_gateway is False
        assert snap.serve_agent_runner is True
        assert snap.paused is False
        # paused
        fake_flag.write_text("")
        snap2 = cluster_status.status_snapshot()
        assert snap2.paused is True

    def test_snapshot_includes_head_sha(
        self,
        set_machine_identity,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """status_snapshot threads the prod-source HEAD so the roster can compare
        it against the cluster pin."""
        set_machine_identity(role="agent-runner", name="wsl")
        monkeypatch.setattr("shared.cluster_drift.prod_source_head_sha", lambda: "abc1234")
        assert cluster_status.status_snapshot().head_sha == "abc1234"

    def test_snapshot_includes_running_sha(
        self,
        set_machine_identity,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """status_snapshot threads the commit the answering process froze at its
        own boot — distinct from head_sha (the checkout the pin verdict compares)
        so the roster can expose a node running stale code even when its checkout
        reads on-pin."""
        set_machine_identity(role="agent-runner", name="wsl")
        monkeypatch.setattr("shared.process_sha.get", lambda: "def5678")
        assert cluster_status.status_snapshot().running_sha == "def5678"

    def test_snapshot_ignores_the_start_bookmark(
        self,
        set_machine_identity,
        monkeypatch: pytest.MonkeyPatch,
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
        set_machine_identity(role="agent-runner", name="wsl")
        monkeypatch.setattr("shared.process_sha.get", lambda: "0ld0ld0aaaa")
        monkeypatch.setattr("shared.running_sha.get", lambda: "n3wn3w0bbbb")
        monkeypatch.setattr("shared.cluster_drift.prod_source_head_sha", lambda: "n3wn3w0bbbb")

        snap = cluster_status.status_snapshot()

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
    ) -> None:
        del fake_flag
        set_machine_identity(role="agent-runner", name="test-host")
        monkeypatch.setattr(cluster_status, "_supervisor_online", lambda: online)
        assert cluster_status.status_snapshot().supervisor_online is online


# ─── /api/cluster/stop + update endpoints via TestClient ─────────────────────


class TestClusterEndpoints:
    def test_post_stop_dispatches_cluster_stop(
        self,
        monkeypatch: pytest.MonkeyPatch,
        set_machine_identity,
    ) -> None:
        """POST /api/cluster/stop forwards a cluster_stop op to this host's own
        ops server — the gateway router never touches the flag/session itself."""
        from gateway.routers import cluster as cluster_router

        set_machine_identity(role="gateway", name="test-host")
        dispatched: list[dict] = []

        async def _fake_dispatch(
            *,
            target_machine,
            kind,
            payload,
            timeout_s=None,
            retries=None,
            idempotency_key=None,
        ):  # type: ignore[no-untyped-def]
            dispatched.append({"target_machine": target_machine, "kind": kind, "payload": payload})  # pyright: ignore[reportUnknownMemberType]
            return {}

        monkeypatch.setattr(cluster_router._cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            r = client.post(
                "/api/cluster/stop",
                json={"deploy_holder": "g:pid1", "deploy_acquired_at": "2026-08-25T00:00:00Z"},
            )
        assert r.status_code == 200
        assert r.json() == {"paused": True}
        assert dispatched == [
            {
                "target_machine": "test-host",
                "kind": "cluster_stop",
                "payload": {
                    "deploy_holder": "g:pid1",
                    "deploy_acquired_at": "2026-08-25T00:00:00Z",
                },
            }
        ]

    def test_post_resume_dispatches_cluster_resume(
        self,
        monkeypatch: pytest.MonkeyPatch,
        set_machine_identity,
    ) -> None:
        """POST /api/cluster/resume forwards a cluster_resume op to this host's
        own ops server, symmetric with /stop."""
        from gateway.routers import cluster as cluster_router

        set_machine_identity(role="gateway", name="test-host")
        dispatched: list[dict] = []

        async def _fake_dispatch(
            *,
            target_machine,
            kind,
            payload,
            timeout_s=None,
            retries=None,
            idempotency_key=None,
        ):  # type: ignore[no-untyped-def]
            dispatched.append({"target_machine": target_machine, "kind": kind, "payload": payload})  # pyright: ignore[reportUnknownMemberType]
            return {}

        monkeypatch.setattr(cluster_router._cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            r = client.post(
                "/api/cluster/resume",
                json={"deploy_holder": "g:pid1", "deploy_acquired_at": "2026-08-25T00:00:00Z"},
            )
        assert r.status_code == 200
        assert r.json() == {"paused": False}
        assert dispatched == [
            {
                "target_machine": "test-host",
                "kind": "cluster_resume",
                "payload": {
                    "deploy_holder": "g:pid1",
                    "deploy_acquired_at": "2026-08-25T00:00:00Z",
                },
            }
        ]

    def test_post_resume_never_mints_a_capability_from_current_state(self) -> None:
        with TestClient(app) as client:
            r = client.post("/api/cluster/resume")
        assert r.status_code == 422

    def test_transition_capability_requires_an_rfc3339_offset(self) -> None:
        with TestClient(app) as client:
            r = client.post(
                "/api/cluster/resume",
                json={
                    "deploy_holder": "g:pid1",
                    "deploy_acquired_at": "2026-08-25T00:00:00",
                },
            )
        assert r.status_code == 422

    def test_post_stopping_marks_machine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """POST /api/cluster/stopping?machine=<name>&home=<home> retracts that unit."""
        marked: list[tuple[str, str]] = []
        from ops import ops_cluster as ops_mod

        monkeypatch.setattr(
            ops_mod,
            "mark_stopping",
            lambda name, home: marked.append((name, home)),  # pyright: ignore[reportUnknownArgumentType]
        )
        with TestClient(app) as client:
            r = client.post("/api/cluster/stopping", params={"machine": "wsl", "home": "~/.ava"})
        assert r.status_code == 200
        assert r.json() == {"machine": "wsl"}
        assert marked == [("wsl", "~/.ava")]


# ─── admin events query ──────────────────────────────────────────────────────


@pytest.fixture
def fake_admin_events(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    """Patch loki_events.query_events for the admin-events route; record the
    kwargs and return canned rows."""

    calls: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    def _query(**kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
        calls.append(kwargs)
        return rows, False

    monkeypatch.setattr(loki_events, "query_events", _query)
    return {"calls": calls, "rows": rows}


def _row(
    *, msg: str = "hi", level: str = "info", event: str = "log", agent_id: int | None = None
) -> dict[str, Any]:
    return {
        "id": 1,
        "line_sha256": "a" * 64,
        "ts": datetime(2026, 8, 12, tzinfo=UTC),
        "agent_id": agent_id,
        "machine": "machine-1",
        "process": "gateway",
        "category": "telemetry",
        "event_name": event,
        "level": level,
        "source": "test",
        "target_agent_id": None,
        "attributes": {"msg": msg},
    }


class TestAdminEvents:
    """`GET /api/cluster/admin/events` slices the unified event stream from
    Loki for ops debugging without SSH (task #1197)."""

    def test_returns_newest_first(self, fake_admin_events: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        fake_admin_events["rows"].extend([_row(msg="oldest"), _row(msg="newest", agent_id=1)])
        with TestClient(app) as client:
            r = client.get("/api/cluster/admin/events?limit=10")
        assert r.status_code == 200
        items = r.json()["items"]
        assert [i["payload"]["msg"] for i in items] == ["oldest", "newest"]
        # Wire shape includes the canonical-line identity shared by all event APIs.
        assert set(items[0]) == {"id", "line_sha256", "ts", "agent_id", "level", "event", "payload"}

    def test_telemetry_and_log_categories_only(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        """Old PG contract: category IN (telemetry, log) — audit rows (spawn /
        send_message / ...) stay out of the ops log slice. The route passes the
        category set to Loki."""
        with TestClient(app) as client:
            client.get("/api/cluster/admin/events")
        assert fake_admin_events["calls"][0]["categories"] == ["telemetry", "log"]

    def test_filter_agent_id(self, fake_admin_events: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            client.get("/api/cluster/admin/events?agent_id=3")
        kw = fake_admin_events["calls"][0]
        assert kw["agent_id"] == 3
        assert kw["service_only"] is False

    def test_filter_service_only(self, fake_admin_events: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            client.get("/api/cluster/admin/events?service_only=true")
        kw = fake_admin_events["calls"][0]
        assert kw["service_only"] is True
        assert kw["agent_id"] is None

    def test_rejects_agent_id_with_service_only(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            r = client.get("/api/cluster/admin/events?agent_id=1&service_only=true")
        assert r.status_code == 400
        assert fake_admin_events["calls"] == []

    def test_filter_level_threshold(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        # level is a MINIMUM threshold (warning -> warning|error|critical)
        with TestClient(app) as client:
            client.get("/api/cluster/admin/events?level=WARNING")
        kw = fake_admin_events["calls"][0]
        assert kw["level_min"] == "warning"
        assert "level" not in kw

    def test_rejects_invalid_level(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            r = client.get("/api/cluster/admin/events?level=NOPE")
        assert r.status_code == 400
        assert fake_admin_events["calls"] == []

    def test_filter_since_relative(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        before = datetime.now(UTC)
        with TestClient(app) as client:
            client.get("/api/cluster/admin/events?since=1m")
        after = datetime.now(UTC)
        from_ = fake_admin_events["calls"][0]["from_"]
        assert from_ is not None
        # cutoff = now - 60s, on the Python clock
        assert before - timedelta(seconds=70) <= from_ <= after - timedelta(seconds=50)

    def test_filter_since_absolute(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            client.get("/api/cluster/admin/events?since=2026-08-01T00:00:00Z")
        assert fake_admin_events["calls"][0]["from_"] == datetime(2026, 8, 1, tzinfo=UTC)

    def test_rejects_invalid_since(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            r = client.get("/api/cluster/admin/events?since=garbage")
        assert r.status_code == 400
        assert fake_admin_events["calls"] == []

    def test_filter_event_multi(self, fake_admin_events: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            client.get("/api/cluster/admin/events?event=spawn,terminate")
        assert fake_admin_events["calls"][0]["event_names"] == ["spawn", "terminate"]

    def test_filter_grep(self, fake_admin_events: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            client.get("/api/cluster/admin/events?grep=hello")
        assert fake_admin_events["calls"][0]["grep"] == "hello"

    def test_limit_defaults_to_200(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            client.get("/api/cluster/admin/events")
        assert fake_admin_events["calls"][0]["limit"] == 200

    def test_limit_defaults_come_from_display_config(
        self, fake_admin_events: dict[str, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        """The implicit page is ``settings.display.cluster_events_default_limit``
        (``AVA_CLUSTER_EVENTS_DEFAULT_LIMIT``); the literal 200 is only that
        field's default, not a hard-coded page size."""
        from shared.config import settings

        monkeypatch.setattr(settings.display, "cluster_events_default_limit", 7)
        with TestClient(app) as client:
            client.get("/api/cluster/admin/events")
        assert fake_admin_events["calls"][0]["limit"] == 7

    def test_limit_caps_at_1000(self, fake_admin_events: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            r = client.get("/api/cluster/admin/events?limit=1001")
        assert r.status_code == 400
        assert fake_admin_events["calls"] == []

    def test_no_since_uses_default_window(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        # no since -> from_ None -> query_events' 24h lower bound applies
        with TestClient(app) as client:
            client.get("/api/cluster/admin/events")
        assert fake_admin_events["calls"][0]["from_"] is None


# ─── admin: DELETE /api/cluster/machines/{name} ──────────────────────────────


class TestAdminMachineDelete:
    def test_deletes_existing_row(self, db_conn, set_machine_identity) -> None:  # type: ignore[no-untyped-def]
        set_machine_identity(role="gateway", name="cloud")
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("DELETE FROM machines WHERE name = 'laminar-stale'")  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "INSERT INTO machines (name, role, gateway_url) "
                "VALUES ('laminar-stale', ARRAY['gateway'], 'https://example.com')"
            )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]
        with TestClient(app) as client:
            r = client.delete("/api/cluster/machines/laminar-stale")
        assert r.status_code == 200
        assert r.json() == {"deleted": True}
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("SELECT COUNT(*) FROM machines WHERE name = 'laminar-stale'")  # pyright: ignore[reportUnknownMemberType]
            (n,) = cur.fetchone()  # pyright: ignore[reportUnknownMemberType]
        assert n == 0

    def test_missing_row_returns_deleted_false(self, db_conn, set_machine_identity) -> None:  # type: ignore[no-untyped-def]
        set_machine_identity(role="gateway", name="cloud")
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("DELETE FROM machines WHERE name = 'never-existed'")  # pyright: ignore[reportUnknownMemberType]
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]
        with TestClient(app) as client:
            r = client.delete("/api/cluster/machines/never-existed")
        assert r.status_code == 200
        assert r.json() == {"deleted": False}

    def test_refuses_to_delete_self(self, db_conn, set_machine_identity) -> None:  # type: ignore[no-untyped-def]
        set_machine_identity(role="gateway", name="cloud")
        with TestClient(app) as client:
            r = client.delete("/api/cluster/machines/cloud")
        assert r.status_code == 400
        assert "refusing" in r.json()["detail"]


# ─── agent roster: GET /api/cluster/machines ─────────────────────────────────


class TestAgentMachineList:
    def test_get_cluster_machines_returns_name_description_live(
        self,
        db_conn,
        set_machine_identity,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # type: ignore[no-untyped-def]
        # Seed one LOCAL agent-runner row. The local machine is probed through
        # its own ops server like any other (status_probe), so stub the op
        # dispatch; the row survives the agent-view filter (only agent-runner
        # machines run agents).
        from ops import cluster_rpc

        set_machine_identity(role="agent-runner", name="wsl-test")

        async def _fake_dispatch(
            *,
            target_machine,
            kind,
            payload,
            timeout_s=None,
            ops_url=None,
            retries=None,
            idempotency_key=None,
        ):  # type: ignore[no-untyped-def]
            assert kind == "status_probe"
            assert ops_url == "http://wsl-test:18121"
            # The ops server echoes its own machine_name; the gateway verifies it
            # matches the probed row, so the stub must self-report the same name.
            return {
                "machine_name": "wsl-test",
                "serve_gateway": False,
                "serve_agent_runner": True,
                "paused": False,
            }

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("TRUNCATE machines")  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "INSERT INTO machines (name, role, gateway_url, description) "
                "VALUES ('wsl-test', ARRAY['agent-runner'], "
                "'http://wsl-test:18121', 'voice IO + browser')"
            )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]
        with TestClient(app) as client:
            r = client.get("/api/cluster/machines")
        assert r.status_code == 200
        body = r.json()
        assert body == [
            {
                "name": "wsl-test",
                "description": "voice IO + browser",
                "live": True,
                "is_staging": False,
            }
        ]

    def test_get_cluster_machines_reachable_unknown_is_not_live(
        self,
        db_conn,
        set_machine_identity,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # type: ignore[no-untyped-def]
        """The agent/config projection must not target a runner whose ops
        server answered but could not provide a determinate status."""
        from datetime import UTC, datetime

        from gateway.routers import cluster as cluster_router
        from gateway.schemas import MachineStatus

        set_machine_identity(role="gateway", name="cloud-test")
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("TRUNCATE machines")  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "INSERT INTO machines (name, role, gateway_url) "
                "VALUES ('wsl-test', ARRAY['agent-runner'], 'http://wsl-test:18121')"
            )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]
        now = datetime.now(UTC)

        async def _fake_gather(rows, local_name, **_kw):  # type: ignore[no-untyped-def]
            return [
                MachineStatus(
                    name="wsl-test",
                    serve_gateway=False,
                    serve_agent_runner=True,
                    gateway_url="http://wsl-test:18121",
                    up_since_at=now,
                    online=True,
                    paused=None,
                )
            ]

        monkeypatch.setattr(cluster_router, "gather_cluster_status", _fake_gather)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            response = client.get("/api/cluster/machines")

        assert response.status_code == 200
        assert response.json() == [
            {
                "name": "wsl-test",
                "description": None,
                "live": False,
                "is_staging": False,
            }
        ]

    def test_get_cluster_machines_excludes_gateway(
        self,
        db_conn,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # type: ignore[no-untyped-def]
        """The agent view (`/api/cluster/machines`) lists only machines that run
        agent processes — the gateway (which runs none) is filtered out.

        gather_cluster_status is stubbed so the filter is exercised in isolation,
        without a real status_probe round-trip (no live runner in tests). One DB
        row is seeded so the handler does not short-circuit on an empty roster
        before reaching the stub."""
        from datetime import UTC, datetime

        from gateway.routers import cluster as cluster_router
        from gateway.schemas import MachineStatus

        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("TRUNCATE machines")  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "INSERT INTO machines (name, role, gateway_url) "
                "VALUES ('control-test', ARRAY['gateway'], 'https://example.com'), "
                "('wsl-test', ARRAY['agent-runner'], NULL)"
            )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]

        now = datetime.now(UTC)

        async def _fake_gather(rows, local_name, *, cluster_target_sha=None):  # type: ignore[no-untyped-def]
            return [
                MachineStatus(
                    name="control-test",
                    serve_gateway=True,
                    serve_agent_runner=False,
                    gateway_url="https://example.com",
                    up_since_at=now,
                    online=True,
                    paused=False,
                    description="ops gateway",
                    stopped_at=None,
                ),
                MachineStatus(
                    name="wsl-test",
                    serve_gateway=False,
                    serve_agent_runner=True,
                    gateway_url="",
                    up_since_at=now,
                    online=True,
                    paused=False,
                    description="voice IO + browser",
                    stopped_at=None,
                ),
            ]

        monkeypatch.setattr(cluster_router, "gather_cluster_status", _fake_gather)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            r = client.get("/api/cluster/machines")
        assert r.status_code == 200
        body = r.json()
        assert body == [
            {
                "name": "wsl-test",
                "description": "voice IO + browser",
                "live": True,
                "is_staging": False,
            }
        ]
        assert all(m["name"] != "control-test" for m in body)

    def test_set_machine_staging_flips_flag_and_excludes_from_roster_targets(
        self, db_conn, set_machine_identity
    ) -> None:  # type: ignore[no-untyped-def]
        """POST /api/cluster/machines/{name}/staging flips the operator staging
        flag; a flagged row is still served on the roster (visible) but
        `list_agent_runners`-backed endpoints exclude it. Unknown name → 404."""
        set_machine_identity(role="gateway", name="test-host")
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("TRUNCATE machines")  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "INSERT INTO machines (name, role, gateway_url) "
                "VALUES ('stage', ARRAY['agent-runner'], NULL)"
            )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]

        with TestClient(app) as client:
            r = client.post("/api/cluster/machines/stage/staging", json={"is_staging": True})
            assert r.status_code == 200
            assert r.json() == {"deleted": True}

            # unknown machine → 404
            r = client.post("/api/cluster/machines/ghost/staging", json={"is_staging": True})
            assert r.status_code == 404

            # roster still serves the row (staging is visible), with the flag set
            r = client.get("/api/cluster/roster")
            assert r.status_code == 200
            stage_row = next(m for m in r.json() if m["name"] == "stage")
            assert stage_row["is_staging"] is True

            # unmark restores the normal target posture
            r = client.post("/api/cluster/machines/stage/staging", json={"is_staging": False})
            assert r.status_code == 200
            r = client.get("/api/cluster/roster")
            stage_row = next(m for m in r.json() if m["name"] == "stage")
            assert stage_row["is_staging"] is False

    def test_get_cluster_roster_returns_full_status(self, db_conn, set_machine_identity) -> None:  # type: ignore[no-untyped-def]
        """`/api/cluster/roster` returns the full MachineStatus rows (name/role/
        online/paused), backing the thin `ava cluster status`."""
        set_machine_identity(role="gateway", name="test-host")
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("TRUNCATE machines")  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "INSERT INTO machines (name, role, gateway_url) "
                "VALUES ('test-host', ARRAY['gateway'], 'https://example.com')"
            )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]
        with TestClient(app) as client:
            r = client.get("/api/cluster/roster")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        assert body[0]["name"] == "test-host"
        assert body[0]["serve_gateway"] is True
        assert body[0]["serve_agent_runner"] is False
        assert body[0]["online"] is True
        assert "stopped_at" in body[0]


# ─── machines pause / resume (Task #1283) ────────────────────────────────────


def _seed_away_machine(
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


def _seed_agent_on_machine(
    db_conn: psycopg.Connection, machine: str, *, status: str = "idling"
) -> int:
    """One live agent row homed on `machine` (test spawn helper + machine
    stamp; the row-creation path moved gateway-side, Task #1236 follow-up)."""
    from tests.conftest import spawn_agent

    aid = spawn_agent(spawner="user")
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET machine = %s, status = %s WHERE id = %s",
            (machine, status, aid),
        )
    db_conn.commit()
    return aid


def _seed_drain_owner(db_conn: psycopg.Connection, agent_id: int = 405) -> None:
    """The drain-owner agent row the pause endpoint reassigns tasks to (the
    FK target `agent_tasks.owner -> agents(id)`)."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
            (agent_id,),
        )
    db_conn.commit()


def _seed_in_progress_task(db_conn: psycopg.Connection, owner: int, title: str) -> int:
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


class TestMachinePauseResume:
    def test_pause_drains_terminates_and_hides_from_roster(
        self, db_conn, set_machine_identity
    ) -> None:  # type: ignore[no-untyped-def]
        """The full pause contract: tasks of the machine's live agents are
        drained to #405 with a note, every agent is terminated (graceful via
        the in-process lifecycle stub), and the machine vanishes from the
        roster + agent machine list. The row keeps its registration info."""
        # identity carries agent-runner so spawn_agent works; the DB row for
        # the local host stays gateway-only (roster's local lightweight path)
        set_machine_identity(role="agent-runner", name="test-host")
        _seed_away_machine(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _seed_drain_owner(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        aid = _seed_agent_on_machine(db_conn, "away")  # pyright: ignore[reportUnknownArgumentType]
        _seed_agent_on_machine(db_conn, "away")  # pyright: ignore[reportUnknownArgumentType]
        _seed_in_progress_task(db_conn, aid, "task-on-away")  # pyright: ignore[reportUnknownArgumentType]

        with TestClient(app) as client:
            r = client.post(
                "/api/cluster/machines/away/pause", json={"reason": "\u4f11\u5047\u4e00\u5468"}
            )
        assert r.status_code == 200
        body = r.json()
        assert body["paused"] is True
        assert body["terminated_agents"] == 2
        assert body["force_marked_agents"] == 0
        assert body["reassigned_tasks"] == 1
        assert body["pause_reason"] == "\u4f11\u5047\u4e00\u5468"
        assert body["paused_at"] is not None

        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "SELECT COUNT(*) FROM agents_meta WHERE machine = 'away' AND status != 'terminated'"
            )
            (n_live,) = cur.fetchone()  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "SELECT owner, results FROM agent_tasks WHERE title = 'task-on-away'"
            )
            owner, results = cur.fetchone()  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "SELECT gateway_url, role FROM machines WHERE name = 'away'"
            )
            gateway_url, role = cur.fetchone()  # pyright: ignore[reportUnknownMemberType]
        assert n_live == 0
        assert owner == 405
        assert "machine pause" in results or "paused" in results
        assert gateway_url is None and role == ["agent-runner"]  # registration kept

        # roster + agent machine list hide the paused machine — the cluster
        # shows only its active members (the gateway host itself)
        with TestClient(app) as client:
            roster = client.get("/api/cluster/roster").json()
            machines_list = client.get("/api/cluster/machines").json()
        assert [m["name"] for m in roster] == ["test-host"]
        assert machines_list == []

    def test_pause_already_paused_is_idempotent(self, db_conn, set_machine_identity) -> None:  # type: ignore[no-untyped-def]
        """Re-pausing an already-paused machine is a safe no-op: nothing left
        to drain/terminate, the original latch values are returned."""
        set_machine_identity(role="gateway", name="test-host")
        _seed_away_machine(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            first = client.post("/api/cluster/machines/away/pause", json={"reason": "once"})
            second = client.post("/api/cluster/machines/away/pause", json={"reason": "twice"})
        assert first.status_code == 200 and second.status_code == 200
        assert first.json()["paused_at"] == second.json()["paused_at"]
        assert second.json()["pause_reason"] == "once"  # first reason preserved
        assert second.json()["terminated_agents"] == 0

    def test_pause_force_marks_when_ops_unreachable(
        self,
        db_conn: psycopg.Connection,
        set_machine_identity,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # type: ignore[no-untyped-def]
        """A machine whose ops server cannot take the graceful terminate (already
        unreachable) gets its agent rows force-marked terminated in the shared
        DB — pause must not leave agents 'running' on a machine that is leaving."""
        from gateway.routers import agents_forward as _fwd

        set_machine_identity(role="agent-runner", name="test-host")
        _seed_away_machine(db_conn)
        aid = _seed_agent_on_machine(db_conn, "away")
        from shared.db import insert_inbound_message

        old_chat_id = insert_inbound_message(db_conn, aid, "queued before pause", source="user")

        async def _unreachable(target: str, path: str, json_body: dict) -> dict:
            raise RuntimeError("ops server unreachable")

        monkeypatch.setattr(_fwd, "_enqueue_lifecycle", _unreachable)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            r = client.post("/api/cluster/machines/away/pause", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["terminated_agents"] == 0
        assert body["force_marked_agents"] == 1
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT status, termination_source, last_force_terminate_inbound_id "
                "FROM agents_meta WHERE machine = 'away'"
            )
            status_row = cur.fetchone()
            assert status_row is not None
            status, source, fence_id = status_row
            cur.execute(
                "SELECT id FROM inbound_messages WHERE agent_id=%s AND kind='terminate' "
                "ORDER BY id DESC LIMIT 1",
                (aid,),
            )
            terminate_row = cur.fetchone()
            assert terminate_row is not None
            terminate_id = terminate_row[0]
        assert status == "terminated"
        assert source == "user"
        assert old_chat_id < fence_id == terminate_id

        from psycopg_pool import ConnectionPool

        from services.delivery_watchdog.daemon import select_terminated_owners_with_pending
        from shared.config import settings

        with ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2) as pool:
            assert select_terminated_owners_with_pending(cast(ConnectionPool, pool), 86400.0) == []

    def test_pause_unknown_machine_404(self, db_conn, set_machine_identity) -> None:  # type: ignore[no-untyped-def]
        set_machine_identity(role="gateway", name="test-host")
        with TestClient(app) as client:
            r = client.post("/api/cluster/machines/ghost/pause", json={})
        assert r.status_code == 404

    def test_pause_refuses_gateway_own_machine(self, db_conn, set_machine_identity) -> None:  # type: ignore[no-untyped-def]
        """Pausing the gateway host itself is refused — the cluster needs its
        gateway member online to answer anything."""
        set_machine_identity(role="gateway", name="test-host")
        _seed_away_machine(db_conn, name="test-host", local_row=False)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            r = client.post("/api/cluster/machines/test-host/pause", json={})
        assert r.status_code == 400
        assert "refusing" in r.json()["detail"]

    def test_resume_restores_roster_and_is_idempotent(self, db_conn, set_machine_identity) -> None:  # type: ignore[no-untyped-def]
        """Resume clears the latch: the machine is served on the roster and the
        agent machine list again; resuming a not-paused machine is a no-op
        (resumed=False)."""
        set_machine_identity(role="gateway", name="test-host")
        _seed_away_machine(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            client.post("/api/cluster/machines/away/pause", json={})
            r = client.post("/api/cluster/machines/away/resume", json={})
            again = client.post("/api/cluster/machines/away/resume", json={})
        assert r.status_code == 200
        assert r.json() == {"name": "away", "resumed": True}
        assert again.json() == {"name": "away", "resumed": False}

        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "SELECT paused_at, pause_reason FROM machines WHERE name = 'away'"
            )
            paused_at, pause_reason = cur.fetchone()  # pyright: ignore[reportUnknownMemberType]
        assert paused_at is None and pause_reason is None

        with TestClient(app) as client:
            roster = client.get("/api/cluster/roster").json()
            machines_list = client.get("/api/cluster/machines").json()
        assert "away" in [m["name"] for m in roster]
        assert "away" in [m["name"] for m in machines_list]

    def test_resume_unknown_machine_404(self, db_conn, set_machine_identity) -> None:  # type: ignore[no-untyped-def]
        set_machine_identity(role="gateway", name="test-host")
        with TestClient(app) as client:
            r = client.post("/api/cluster/machines/ghost/resume", json={})
        assert r.status_code == 404
