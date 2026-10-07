"""Cluster endpoints cases: admin machine delete."""

from __future__ import annotations

from typing import cast

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.db import Database
from base.events.live.bus import EventBus
from gateway.app import app
from tests.gateway.test_cluster_endpoints import _pin_session_names as _pin_session_names
from tests.gateway.test_cluster_endpoints import (
    _seed_agent_on_machine,
    _seed_away_machine,
    _seed_drain_owner,
    _seed_in_progress_task,
)
from tests.gateway.test_cluster_endpoints import (
    fake_admin_events as fake_admin_events,
)
from tests.gateway.test_cluster_endpoints import (
    fake_flag as fake_flag,
)
from tests.gateway.test_cluster_endpoints import (
    pause_backend as pause_backend,
)


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
        from ops.cluster import rpc as cluster_rpc

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

        monkeypatch.setattr(cluster_rpc, "dispatch_to_url", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
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

        from base.api_contracts.status import MachineStatus
        from gateway.cluster import router as cluster_router

        set_machine_identity(role="gateway", name="cloud-test")
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("TRUNCATE machines")  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "INSERT INTO machines (name, role, gateway_url) "
                "VALUES ('wsl-test', ARRAY['agent-runner'], 'http://wsl-test:18121')"
            )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]
        now = datetime.now(UTC)

        async def _fake_gather(_db: object, rows, local_name, **_kw):  # type: ignore[no-untyped-def]
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

        from base.api_contracts.status import MachineStatus
        from gateway.cluster import router as cluster_router

        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("TRUNCATE machines")  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "INSERT INTO machines (name, role, gateway_url) "
                "VALUES ('control-test', ARRAY['gateway'], 'https://example.com'), "
                "('wsl-test', ARRAY['agent-runner'], NULL)"
            )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]

        now = datetime.now(UTC)

        async def _fake_gather(_db: object, rows, local_name, **_kw):  # type: ignore[no-untyped-def]
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
        database: Database,
        event_bus: EventBus,
    ) -> None:  # type: ignore[no-untyped-def]
        """A machine whose ops server cannot take the graceful terminate (already
        unreachable) gets its agent rows force-marked terminated in the shared
        DB — pause must not leave agents 'running' on a machine that is leaving."""
        from gateway.agents import forward as _fwd

        set_machine_identity(role="agent-runner", name="test-host")
        _seed_away_machine(db_conn)
        aid = _seed_agent_on_machine(db_conn, "away")
        from base.db import insert_inbound_message

        old_chat_id = insert_inbound_message(
            db_conn, aid, "queued before pause", source="user", bus=event_bus, database=database
        )

        async def _unreachable(_db: object, target: str, path: str, json_body: dict) -> dict:
            raise RuntimeError("ops server unreachable")

        monkeypatch.setattr(_fwd, "enqueue_lifecycle", _unreachable)  # pyright: ignore[reportUnknownArgumentType]
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

        from base.config import settings
        from services.wake.delivery_watchdog.daemon import select_terminated_owners_with_pending

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
