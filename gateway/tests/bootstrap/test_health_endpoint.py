from __future__ import annotations

from typing import Never

import pytest
from psycopg_pool import PoolTimeout

import gateway.app as gateway_app
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.paths import ava_home
from gateway.cluster.process_boot import LOADED_IMAGE
from tests.fixtures.gateway_config import gateway_test_client


def _health() -> dict[str, object]:
    with gateway_test_client() as client:
        resp = client.get("/api/health")
    assert resp.status_code == 200
    return resp.json()


def test_health_payload_names_the_service_that_answered() -> None:
    assert _health()["name"] == "gateway"


def test_health_payload_carries_the_full_identity_set(*, machine_identity: MachineIdentity) -> None:
    payload = _health()
    assert isinstance(payload.pop("started_at"), float)
    assert payload.pop("sha") == LOADED_IMAGE.sha
    assert payload == {
        "status": "ok",
        "name": "gateway",
        "home": str(ava_home()),
        "machine": machine_identity.name(),
        "liveness": "ok",
        "readiness": "ok",
        "components": [
            {"name": "http", "status": "ok", "progress": "serving"},
            {"name": "db", "status": "ok"},
        ],
        "degraded_reasons": [],
    }


def test_health_process_birth_is_stable_between_requests() -> None:
    with gateway_test_client() as client:
        first = client.get("/api/health").json()["started_at"]
        second = client.get("/api/health").json()["started_at"]
    assert isinstance(first, float)
    assert second == first


def test_health_control_pool_timeout_returns_degraded_with_identity(
    *, machine_identity: MachineIdentity
) -> None:
    class _TimedOutPool:
        def connection(self) -> Never:
            raise PoolTimeout("control pool saturated")

        def close(self) -> None:
            pass

    with gateway_test_client() as client:
        client.app.state.control_db_pool = _TimedOutPool()
        resp = client.get("/api/health")

    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "1"
    payload = resp.json()
    assert isinstance(payload.pop("started_at"), float)
    assert payload.pop("sha") == LOADED_IMAGE.sha
    assert payload == {
        "status": "degraded",
        "name": "gateway",
        "home": str(ava_home()),
        "machine": machine_identity.name(),
        "liveness": "ok",
        "readiness": "degraded",
        "components": [
            {"name": "http", "status": "ok", "progress": "serving"},
            {
                "name": "db",
                "status": "degraded",
                "detail": "PoolTimeout: control pool saturated",
            },
        ],
        "degraded_reasons": ["db: PoolTimeout: control pool saturated"],
    }


def test_control_plane_bypasses_an_unreadable_admission_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_read(_request: object) -> bool:
        raise AssertionError("control-plane request read the business admission journal")

    monkeypatch.setattr(gateway_app, "cluster_is_paused", unexpected_read)
    with gateway_test_client() as client:
        assert client.get("/api/health").status_code == 200
