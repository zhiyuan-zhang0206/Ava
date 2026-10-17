import os
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from psycopg_pool import PoolTimeout

from base import config
from base.cluster.machine import MachineIdentity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.deploy.lifecycle import start_serving
from base.deploy.maintenance import admission, pause_owner
from base.deploy.maintenance.state import MaintenanceHold, MaintenancePhase
from base.deploy.state import host_deploy_state
from base.deploy.tests.maintenance_factory import WHEN
from base.deploy.tests.maintenance_factory import isolate as isolate
from base.events.live.bus import EventBus
from base.host.env import dotenv_file
from base.host.net.auth import bearer_header
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.config_registration import plugin_bootstrap_config
from gateway.tests.agents.gateway_auth import without_credentials
from tests.fixtures.gateway_config import gateway_test_client


@pytest.fixture
def held(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unit_home: Path,
    database: Database,
    *,
    machine_identity: MachineIdentity,
) -> None:
    del unit_home
    monkeypatch.setattr(start_serving, "state_path", lambda: tmp_path / "serving.json")
    pause_owner.begin_maintenance("update", WHEN)
    pause_owner.change_maintenance(
        "update", WHEN, MaintenanceHold(), MaintenanceHold(MaintenancePhase.STOPPED)
    )
    host_deploy_state.set_posture(database, "paused", machine=machine_identity.name())
    start_serving.begin_start()


@pytest.mark.usefixtures("held")
def test_gateway_health_probes_database_during_hold_and_business_stays_closed(
    gateway_data_plane: None,
) -> None:
    with gateway_test_client() as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["name"] == "gateway"
        assert health.json()["status"] == "ok"
        assert client.get("/api/agents").status_code == 503
    assert admission.held()
    assert not start_serving.is_serving()


@pytest.mark.usefixtures("held")
def test_held_gateway_health_still_reports_database_failure(
    gateway_data_plane: None,
) -> None:
    with gateway_test_client() as client:
        client.app.state.control_db_pool.connection = MagicMock(
            side_effect=PoolTimeout("isolated readiness failure")
        )
        response = client.get("/api/health")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"
        assert "PoolTimeout" in response.text
    assert admission.held()
    assert not start_serving.is_serving()


def test_fleet_drain_keeps_sdk_open_during_preparation_identity_probe(
    gateway_data_plane: None,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    from ops import agent_pause

    class ProbeBoundaryError(Exception):
        pass

    machine_identity.set(role="agent-runner")
    monkeypatch.setattr(agent_pause, "host_running", lambda: True)
    with gateway_test_client(machine=machine_identity) as client:

        def inspect_before_drain() -> None:
            response = client.get("/api/agents")
            assert response.status_code == 200, response.text
            current = admission.snapshot()
            assert current is not None and current.maintenance is not None
            assert current.maintenance.phase == MaintenancePhase.PREPARING
            raise ProbeBoundaryError

        monkeypatch.setattr(agent_pause, "host_identity", inspect_before_drain)
        pause_owner.begin_maintenance("fleet", WHEN)
        with pytest.raises(ProbeBoundaryError):
            agent_pause.prepare(database, event_bus, "fleet", WHEN, local_machine=machine_identity)
    assert admission.held()


@pytest.mark.usefixtures("held")
def test_held_health_exemption_preserves_authentication(
    monkeypatch: pytest.MonkeyPatch, gateway_data_plane: None
) -> None:
    monkeypatch.setattr(settings.data_plane, "cluster_secret", uuid4().hex)
    with gateway_test_client() as client:
        assert client.get("/api/health").status_code == 200
        assert without_credentials(client).get("/api/cluster/status").status_code == 401
    assert admission.held()


def _authenticated(monkeypatch: pytest.MonkeyPatch) -> str:
    secret = uuid4().hex
    monkeypatch.setattr(settings.data_plane, "cluster_secret", secret)
    monkeypatch.setattr("base.paths.meiva_home", dotenv_file.unit_home)
    return secret


@pytest.mark.usefixtures("held")
def test_held_gateway_serves_bootstrap_only_to_an_authenticated_caller(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    served_gateway_home: Any,
    gateway_data_plane: None,
) -> None:
    _authenticated(monkeypatch)
    runner = bearer_header(served_gateway_home.api.runner)
    with gateway_test_client() as client:
        assert without_credentials(client).get("/api/bootstrap").status_code == 401
        assert client.get("/api/bootstrap", headers=bearer_header("wrong")).status_code == 401
        served = client.get("/api/bootstrap", headers=runner)
        assert client.get("/api/agents", headers=runner).status_code == 503
    assert served.status_code == 200, served.text
    body: dict[str, str] = served.json()
    assert urlsplit(body["MEIVA_DB_URL"]).password is None
    assert not {"MEIVA_RUNNER_DB_PASSWORD", "MEIVA_REDIS_ADMIN_PASSWORD"} & set(body)
    payload = "".join(body.values())
    for role in (served_gateway_home.roles.runner, served_gateway_home.roles.gateway):
        assert role.name not in payload and role.password not in payload
    assert admission.business_paused(tmp_path)


@pytest.mark.usefixtures("held")
def test_a_runners_first_join_reaches_a_held_gateway(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    served_gateway_home: Any,
    gateway_data_plane: None,
) -> None:
    from base.host.db_authority import unit
    from base.host.env import bootstrap
    from cli.commands.cluster import unit_join

    secret = _authenticated(monkeypatch)
    complete = settings if settings.profile is None else config.Settings(profile=None)
    bootstrap_authority = ConfigAuthority(settings, complete, tmp_path / ".env")
    runner = (tmp_path / "runner").resolve()
    runner.mkdir(mode=0o700)
    issued = unit.issue_bundle(
        dotenv_file.unit_home(),
        unit=unit.UnitIdentity(machine="mini", home=str(runner)),
        endpoint=config.bootstrap_config_values(
            bootstrap_authority,
            provider_key_envs=(
                binding.key_env for binding in build_model_catalog().bindings.values()
            ),
            plugin_cluster_config=plugin_bootstrap_config(),
        )["MEIVA_DB_URL"],
        cluster_secret=secret,
        ttl_s=600,
    )
    bundle = tmp_path / "mini.bundle"
    bundle.write_bytes(issued.envelope)
    monkeypatch.setattr(
        unit, "install_bundle", partial(unit.install_bundle, probe=lambda _dsn: None)
    )
    gateway = "http://127.0.0.1:1"
    with gateway_test_client() as client, patch.dict(os.environ):

        def dial(url: str, **kwargs: Any) -> Any:
            return client.get(url.removeprefix(gateway), **kwargs)

        monkeypatch.setattr(bootstrap, "dial_get", dial)
        os.environ[unit.CAPABILITY_KEY_ENV] = issued.transport_key
        unit_join.join_gateway(
            {"MEIVA_GATEWAY_URL": gateway, "MEIVA_MACHINE_NAME": "mini"}, runner, str(bundle)
        )
    installed = unit.require_unit_capability(runner)
    assert installed.api is not None and not bundle.exists()
    assert admission.business_paused(tmp_path)
