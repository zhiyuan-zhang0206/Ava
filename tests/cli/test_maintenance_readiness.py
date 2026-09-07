"""Ordinary start measures real gateway health before releasing a stopped hold."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import cli.commands as cli
from gateway.app import app
from shared import host_deploy_state, maintenance, start_serving
from tests.agent.test_maintenance import isolate as isolate
from tests.cli.test_start_readiness_gate import _hermetic_start as _hermetic_start
from tests.cli.test_start_readiness_gate import _roster
from tests.services.test_maintenance_readiness import held as held

pytestmark = pytest.mark.real_service_readiness_gate


@pytest.mark.real_cluster_spawn
@pytest.mark.usefixtures("held")
def test_start_measures_real_health_then_resumes_without_early_business_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _roster(monkeypatch, (("gateway", None),))
    monkeypatch.setattr(cli, "_has_session", MagicMock(return_value=True))
    measurements: list[int] = []
    with TestClient(app) as client:

        def probe(_spec: object) -> cli.ServiceProbe:
            assert maintenance.held()
            assert not start_serving.is_serving()
            assert client.get("/api/agents").status_code == 503
            response = client.get("/api/health")
            measurements.append(response.status_code)
            return cli.ServiceProbe(response.status_code == 200, "http", "measured gateway health")

        monkeypatch.setattr(cli, "_probe_service", probe)
        assert cli.cmd_start(persist_services=False) == 0
    assert measurements and set(measurements) == {200}
    assert start_serving.is_serving()
    assert not maintenance.held()
    posture = host_deploy_state.read()
    assert posture is not None and posture.posture == "idle"
