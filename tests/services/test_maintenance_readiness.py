"""A stopped generation can prove readiness without reopening native work."""

import os
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg_pool import PoolTimeout

from base import config
from base.cluster.auth import bearer_header
from base.config import settings
from base.db import Database
from base.deploy.lifecycle import start_serving
from base.deploy.maintenance import admission, pause_owner
from base.deploy.maintenance.state import MaintenanceHold
from base.deploy.state import host_deploy_state
from base.events.live.bus import EventBus
from base.host.env import runtime_config as rt
from gateway.app import app
from tests.agent.test_maintenance import WHEN
from tests.agent.test_maintenance import isolate as isolate


@pytest.fixture
def held(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database: Database) -> None:
    monkeypatch.setattr(start_serving, "state_path", lambda: tmp_path / "serving.json")
    pause_owner.begin_maintenance("update", WHEN)
    pause_owner.change_maintenance("update", WHEN, MaintenanceHold(), MaintenanceHold("stopped"))
    host_deploy_state.set_posture(database, "paused")
    start_serving.begin_start()


@pytest.mark.usefixtures("held")
def test_gateway_health_probes_database_during_hold_and_business_stays_closed() -> None:
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["name"] == "gateway"
        assert health.json()["status"] == "ok"
        assert client.get("/api/agents").status_code == 503
    assert admission.held()
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
    assert admission.held()
    assert not start_serving.is_serving()


def test_control_plane_bypasses_an_unreadable_admission_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_read(_request: object) -> bool:
        raise AssertionError("control-plane request read the business admission journal")

    monkeypatch.setattr("gateway.app._cluster_is_paused", unexpected_read)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200


def test_fleet_drain_keeps_sdk_open_during_preparation_identity_probe(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    from ops import agent_pause

    class ProbeBoundaryError(Exception):
        pass

    monkeypatch.setattr(agent_pause, "machine_role", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(agent_pause, "host_running", lambda: True)
    with TestClient(app) as client:

        def inspect_before_drain() -> None:
            response = client.get("/api/agents")
            assert response.status_code == 200, response.text
            current = admission.snapshot()
            assert current is not None and current.maintenance is not None
            assert current.maintenance.phase == "preparing"
            raise ProbeBoundaryError

        monkeypatch.setattr(agent_pause, "host_identity", inspect_before_drain)
        # A continued release drain: its hold is published, still preparing.
        pause_owner.begin_maintenance("fleet", WHEN)
        with pytest.raises(ProbeBoundaryError):
            agent_pause.prepare(database, event_bus, "fleet", WHEN)
    # Only an explicit abort releases the hold, never the drain.
    assert admission.held()


@pytest.mark.usefixtures("held")
def test_held_health_exemption_preserves_authentication(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.data_plane, "cluster_secret", uuid4().hex)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    with TestClient(app) as client:
        # Health is already public; control-plane classification must not make
        # the authenticated status surface public too.
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/cluster/status").status_code == 401
    assert admission.held()


def _authenticated(monkeypatch: pytest.MonkeyPatch) -> str:
    """The served home's ledger authenticates machine tokens; returns the human secret."""
    secret = uuid4().hex
    monkeypatch.setattr(settings.data_plane, "cluster_secret", secret)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr("base.paths.ava_home", rt._ava_home)
    return secret


@pytest.mark.usefixtures("held")
def test_held_gateway_serves_bootstrap_only_to_an_authenticated_caller(
    monkeypatch: pytest.MonkeyPatch, served_gateway_home: Any
) -> None:
    """Bootstrap is control-plane: a runner started under a hold and its processes'
    config resolution read it before any hold is released. The exemption keeps
    the authentication and serves no database login; business stays closed."""
    _authenticated(monkeypatch)
    runner = bearer_header(served_gateway_home.api.runner)
    with TestClient(app) as client:
        assert client.get("/api/bootstrap").status_code == 401
        assert client.get("/api/bootstrap", headers=bearer_header("wrong")).status_code == 401
        served = client.get("/api/bootstrap", headers=runner)
        assert client.get("/api/agents", headers=runner).status_code == 503
    assert served.status_code == 200, served.text
    body: dict[str, str] = served.json()
    assert urlsplit(body["AVA_DB_URL"]).password is None
    assert not {"AVA_RUNNER_DB_PASSWORD", "AVA_REDIS_ADMIN_PASSWORD"} & set(body)
    payload = "".join(body.values())
    for role in (served_gateway_home.roles.runner, served_gateway_home.roles.gateway):
        assert role.name not in payload and role.password not in payload
    assert admission.business_paused()


@pytest.mark.usefixtures("held")
def test_a_runners_first_join_reaches_a_held_gateway(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, served_gateway_home: Any
) -> None:
    """A runner's init joins (`join_gateway`) with the bundle the gateway issued
    while the gateway's own hold still stands, over the gateway's real
    middleware stack."""
    from base.cluster.authority import unit
    from base.host.env import bootstrap
    from cli import unit_join

    secret = _authenticated(monkeypatch)
    runner = (tmp_path / "runner").resolve()
    runner.mkdir(mode=0o700)
    issued = unit.issue_bundle(
        rt._ava_home(),
        unit=unit.UnitIdentity(machine="mini", home=str(runner)),
        endpoint=config.bootstrap_config_values()["AVA_DB_URL"],
        cluster_secret=secret,
        ttl_s=600,
    )
    bundle = tmp_path / "mini.bundle"
    bundle.write_bytes(issued.envelope)
    # The seeded ledger's logins are no PostgreSQL roles: skip the install's login probe.
    monkeypatch.setattr(
        unit, "install_bundle", partial(unit.install_bundle, probe=lambda _dsn: None)
    )
    gateway = "http://127.0.0.1:1"
    with TestClient(app) as client, patch.dict(os.environ):

        def dial(url: str, **kwargs: Any) -> Any:
            return client.get(url.removeprefix(gateway), **kwargs)

        monkeypatch.setattr(bootstrap, "dial_get", dial)
        os.environ[unit.CAPABILITY_KEY_ENV] = issued.transport_key
        unit_join.join_gateway(
            {"AVA_GATEWAY_URL": gateway, "AVA_MACHINE_NAME": "mini"}, runner, str(bundle)
        )
    installed = unit.require_unit_capability(runner)
    assert installed.api is not None and not bundle.exists()
    assert admission.business_paused()
