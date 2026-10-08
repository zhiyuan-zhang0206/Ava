"""Ordinary start measures real gateway health before releasing a stopped hold."""

import pytest
from fastapi.testclient import TestClient

import cli.commands._probe as _probe_commands
import cli.commands.lifecycle.start as _start_commands
from base.db import Database
from base.deploy.lifecycle import start_serving
from base.deploy.maintenance import admission
from base.deploy.state import host_deploy_state
from cli.commands.lifecycle.tests.startup.test_start_readiness_gate import (
    _hermetic_start as _hermetic_start,
)
from cli.commands.lifecycle.tests.startup.test_start_readiness_gate import _roster
from gateway.app import app
from tests.components.agent.test_maintenance import isolate as isolate
from tests.components.services.test_maintenance_readiness import held as held

pytestmark = pytest.mark.real_service_readiness_gate


@pytest.mark.usefixtures("held")
def test_start_measures_real_health_then_resumes_without_early_business_admission(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:

    _roster(monkeypatch, (("gateway", None),))
    measurements: list[int] = []
    with TestClient(app) as client:

        def probe(_spec: object) -> _probe_commands.ServiceProbe:
            assert admission.held()
            assert not start_serving.is_serving()
            assert client.get("/api/agents").status_code == 503
            response = client.get("/api/health")
            measurements.append(response.status_code)
            return _probe_commands.ServiceProbe(
                response.status_code == 200, "http", "measured gateway health"
            )

        monkeypatch.setattr(_probe_commands, "_probe_service", probe)
        assert _start_commands.cmd_start(persist_services=False, retained_children=[]) == 0
        # Completion must release the real business gate in the same turn,
        # without a sleep that lets an independent posture cache expire.
        resumed = client.get("/api/agents")
        assert resumed.status_code == 200, resumed.text
    assert measurements and set(measurements) == {200}
    assert start_serving.is_serving()
    assert not admission.held()
    posture = host_deploy_state.read(database)
    assert posture is not None and posture.posture == "idle"
