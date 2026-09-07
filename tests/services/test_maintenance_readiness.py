"""A stopped generation can prove readiness without reopening native work."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool, PoolTimeout

from gateway.app import app
from services.agent_ops import daemon
from shared import host_deploy_state, maintenance, pause_owner, start_serving
from shared.config import settings
from shared.maintenance_state import MaintenanceHold
from tests.agent.test_maintenance import WHEN
from tests.agent.test_maintenance import isolate as isolate


@pytest.fixture
def held(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(start_serving, "state_path", lambda: tmp_path / "serving.json")
    pause_owner.begin_maintenance("update", WHEN)
    pause_owner.change_maintenance("update", WHEN, MaintenanceHold(), MaintenanceHold("stopped"))
    host_deploy_state.set_posture("paused")
    start_serving.begin_start()


@pytest.mark.usefixtures("held")
def test_gateway_health_probes_database_during_hold_and_business_stays_closed() -> None:
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["name"] == "gateway"
        assert health.json()["status"] == "ok"
        assert client.get("/api/agents").status_code == 503
    assert maintenance.held()
    assert not start_serving.is_serving()


@pytest.mark.usefixtures("held")
def test_held_gateway_health_still_reports_database_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        monkeypatch.setattr(
            app.state.control_db_pool,
            "connection",
            MagicMock(side_effect=PoolTimeout("isolated readiness failure")),
        )
        response = client.get("/api/health")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"
        assert "PoolTimeout" in response.text
    assert maintenance.held()
    assert not start_serving.is_serving()


@pytest.mark.usefixtures("held")
def test_held_health_exemption_preserves_authentication(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.data_plane, "cluster_secret", uuid4().hex)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    with TestClient(app) as client:
        # Health is already public; control-plane classification must not make
        # the authenticated status surface public too.
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/cluster/status").status_code == 401
    assert maintenance.held()


@pytest.mark.real_cluster_spawn
@pytest.mark.usefixtures("held")
async def test_real_ops_status_and_exact_resume_keep_readiness_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real dispatch, executor, PostgreSQL posture and journal; no service is launched.
    with (
        ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2) as pool,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        monkeypatch.setattr(daemon, "_db_pool", pool)
        monkeypatch.setattr(daemon, "_op_executor", executor)
        monkeypatch.setattr(daemon, "_dispatch_sem", asyncio.Semaphore(2))

        async def request(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
            status, raw, _ = await daemon._ops_route(
                json.dumps({"kind": kind, "payload": payload}).encode()
            )
            assert status == 200
            return json.loads(raw)

        status = await request("status_probe", {})
        assert status["status"] == "completed"
        assert status["result"]["paused"] is True
        transition = {"deploy_holder": "update", "deploy_acquired_at": WHEN.isoformat()}
        early = await request("cluster_resume", transition)
        assert early["status"] == "failed"
        assert "readiness" in early["result"]["error"]
        assert maintenance.held()
        generation = start_serving.begin_start()
        assert start_serving.mark_serving(generation)
        wrong = await request("cluster_resume", {**transition, "deploy_holder": "other"})
        assert wrong["status"] == "failed"
        assert maintenance.held()
        resumed = await request("cluster_resume", transition)
        assert resumed["status"] == "completed"
        assert not maintenance.held()
        posture = host_deploy_state.read()
        assert posture is not None and posture.posture == "idle"


@pytest.mark.usefixtures("held")
def test_maintenance_start_waiver_does_not_publish_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands import _maintenance

    monkeypatch.setattr("cli.commands.start.cmd_start", MagicMock(return_value=0))
    assert _maintenance._start("update", WHEN) != 0
    current = maintenance.snapshot()
    assert current is not None and current.maintenance is not None
    assert current.maintenance.phase == "starting"
