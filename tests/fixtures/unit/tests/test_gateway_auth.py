from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from gateway.tests.agents.gateway_auth import gateway_credentials, without_credentials
from tests.fixtures.gateway_config import gateway_test_app, gateway_test_client


def test_a_gateway_client_carries_the_cluster_secret_by_default(
    gateway_data_plane: None, machine_identity: MachineIdentity
) -> None:
    with gateway_test_client(machine=machine_identity) as client:
        assert client.headers["Authorization"] == f"Bearer {settings.data_plane.cluster_secret}"
        assert client.get("/api/status").status_code == 200
        assert without_credentials(client).get("/api/status").status_code == 401


def test_an_explicit_authorization_header_wins(
    gateway_data_plane: None, machine_identity: MachineIdentity
) -> None:
    with gateway_test_client(
        machine=machine_identity, headers={"Authorization": "Bearer someone-else"}
    ) as client:
        assert client.headers["Authorization"] == "Bearer someone-else"
        assert client.get("/api/status").status_code == 401


def test_the_default_follows_the_secret_current_when_the_client_is_built(
    gateway_data_plane: None, monkeypatch: pytest.MonkeyPatch, machine_identity: MachineIdentity
) -> None:
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "rotated-before-build")
    with gateway_test_client(machine=machine_identity) as client:
        assert client.headers["Authorization"] == "Bearer rotated-before-build"
        assert client.get("/api/status").status_code == 200


def test_clients_of_other_apps_are_untouched() -> None:
    standin = FastAPI()
    with TestClient(standin) as client:
        assert "Authorization" not in client.headers


def test_gateway_credentials_is_the_header_of_the_current_secret(
    machine_identity: MachineIdentity,
) -> None:
    app = gateway_test_app(machine=machine_identity)
    assert gateway_credentials(app.state.gateway_config) == {
        "Authorization": f"Bearer {settings.data_plane.cluster_secret}"
    }
