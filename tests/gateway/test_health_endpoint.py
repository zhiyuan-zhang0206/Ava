"""`/api/health` payload contract — the identity a probe is allowed to trust.

A 200 on the gateway port only proves *something* listens there. What makes the
answer believable is the identity the body carries, so every field in it is a
contract with `shared.daemon_health` and is pinned here rather than left to
whatever the handler happens to return.

`name` is the newest of those fields and the reason this module exists: it is
step 1 of an expand-contract pair (#1038). Nothing reads it yet — `_probe_home`
gains the `name` arm only after every deployed gateway emits it — so without a
test the field has no consumer to keep it alive and would be trimmed as dead
weight by the next reader. That is exactly the failure the ordering guards
against, one PR later.
"""

from __future__ import annotations

from typing import Never

import pytest
from fastapi.testclient import TestClient
from psycopg_pool import PoolTimeout

from gateway.app import app
from shared import process_sha
from shared.machine import machine_name
from shared.paths import ava_home


def _health() -> dict[str, object]:
    with TestClient(app) as client:
        resp = client.get("/api/health")
    assert resp.status_code == 200
    return resp.json()


def test_health_payload_names_the_service_that_answered() -> None:
    """`name` is what lets a probe ask "is this a *gateway*" rather than only
    "is this something of ours".

    Same-home, wrong-service is the hole it closes: any other Ava daemon of this
    cluster answering on the gateway port satisfies the `home` check, because
    `home` is a property of the cluster, not of the service.
    """
    assert _health()["name"] == "gateway"


def test_health_payload_carries_the_full_identity_set() -> None:
    """Which service, which cluster, which host — the three axes an impostor can
    differ on, all resolved against this process's own view."""
    payload = _health()
    assert isinstance(payload.pop("started_at"), float)
    assert payload.pop("sha") == process_sha.get()
    assert payload == {
        "status": "ok",
        "name": "gateway",
        "home": str(ava_home()),
        "machine": machine_name(),
        "liveness": "ok",
        "readiness": "ok",
        "components": [
            {"name": "http", "status": "ok", "progress": "serving"},
            {"name": "db", "status": "ok"},
        ],
        "degraded_reasons": [],
    }


def test_health_process_birth_is_stable_between_requests() -> None:
    first = _health()["started_at"]
    second = _health()["started_at"]
    assert isinstance(first, float)
    assert second == first


def test_health_control_pool_timeout_returns_degraded_with_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saturated control pool is a bounded DB degradation, not a hung probe."""

    class _TimedOutPool:
        def connection(self) -> Never:
            raise PoolTimeout("control pool saturated")

        def close(self) -> None:
            pass

    with TestClient(app) as client:
        monkeypatch.setattr(app.state, "control_db_pool", _TimedOutPool())
        resp = client.get("/api/health")

    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "1"
    payload = resp.json()
    assert isinstance(payload.pop("started_at"), float)
    assert payload.pop("sha") == process_sha.get()
    assert payload == {
        "status": "degraded",
        "name": "gateway",
        "home": str(ava_home()),
        "machine": machine_name(),
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
