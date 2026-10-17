from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from base.host.permissions_helper.grants import gate
from base.host.permissions_helper.grants.catalog import PING_KEYS, PREFLIGHT_SERVICES, Grant
from base.host.permissions_helper.grants.probe import ProbeResult
from gateway.routers import host_permissions
from services.desktop.permissions_helper.onboarding import GateService

NOW = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)


def _report(**overrides: Grant) -> gate.GateReport:
    grants = dict.fromkeys(PREFLIGHT_SERVICES, Grant.GRANTED)
    booleans = dict.fromkeys(PING_KEYS, True)
    for item_id, grant in overrides.items():
        item = gate.item_for(item_id)
        if item.ping_key:
            booleans[item.ping_key] = grant is Grant.GRANTED
        for service in item.services:
            grants[service] = grant
    return gate.evaluate(
        ProbeResult(grants=grants, booleans=booleans, supports_request=True),
        now=NOW,
    )


class FakeGate(GateService):
    def __init__(self, report: gate.GateReport) -> None:
        self._report = report
        self.requested = 0
        self.deferred = 0
        self.kwargs: dict[str, object] = {}

    def check(self) -> gate.GateReport:
        return self._report

    def onboard(self, **kwargs: object) -> gate.GateReport:
        self.requested += 1
        self.kwargs = kwargs
        return self._report

    def defer(self) -> None:
        self.deferred += 1

    def restart_helper(self) -> str:
        return ""


@pytest.fixture
def app() -> FastAPI:
    built = FastAPI()
    built.include_router(host_permissions.router)
    return built


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def gate_stub() -> FakeGate:
    return FakeGate(_report(**{"screen-recording": Grant.DENIED}))


def _install(app: FastAPI, stub: FakeGate) -> None:
    app.dependency_overrides[host_permissions.gate_service] = lambda: stub


def test_get_returns_the_live_checklist(
    client: TestClient, app: FastAPI, gate_stub: FakeGate
) -> None:
    _install(app, gate_stub)
    body = client.get("/api/host/permissions").json()
    assert body["state"] == "action_required"
    assert body["required_missing"] == 1
    assert body["checked_at"] == NOW.isoformat()
    row = next(item for item in body["items"] if item["id"] == "screen-recording")
    assert row["jump"].startswith("x-apple.systempreferences:")
    assert row["why"] and row["verify"]


def test_request_runs_the_gate_without_an_attestation_flag(
    client: TestClient, app: FastAPI, gate_stub: FakeGate
) -> None:
    _install(app, gate_stub)
    assert client.post("/api/host/permissions/request").json()["state"] == "action_required"
    assert gate_stub.requested == 1
    assert gate_stub.kwargs["automatic"] is False


def test_request_and_defer_go_through_the_same_domain_call(
    client: TestClient, app: FastAPI, gate_stub: FakeGate
) -> None:
    _install(app, gate_stub)
    assert client.post("/api/host/permissions/request").json()["state"] == "action_required"
    assert gate_stub.requested == 1
    assert client.post("/api/host/permissions/defer").json()["state"] == "action_required"
    assert gate_stub.deferred == 1
