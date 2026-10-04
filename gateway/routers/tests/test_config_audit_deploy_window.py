"""(#4986) The config-audit read quiets its RPC dispatch during a deploy window."""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.config import settings
from gateway.app import app
from gateway.routers import config as config_router
from ops import cluster_rpc
from ops.deploy_window import DeployWindow

_REMOTE = "remote-host"


def _seed_machine(name: str) -> None:
    """Insert a minimal agent-runner row so the target is known."""
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO machines (name, gateway_url) VALUES (%s, %s) "
            "ON CONFLICT (name) DO NOTHING",
            (name, "http://remote.invalid:8000"),
        )
        conn.commit()


def test_get_config_audit_dispatch_quieted_per_request_during_deploy_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner mid-maintenance is expected-unreachable: the audit read still
    503s, but its RPC dispatch is quieted — the window is read per request, so
    the flag follows a flip between two calls (task #4986)."""
    _seed_machine(_REMOTE)
    seen: list[dict[str, object]] = []

    async def _dispatch(*_args: object, **kwargs: object) -> dict[str, object]:
        seen.append(kwargs)
        raise cluster_rpc.ClusterOpUnreachable("no ack")

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _dispatch)
    window = DeployWindow(active=False, detail="no deploy in flight")

    def _window(_db: object) -> DeployWindow:
        return window

    monkeypatch.setattr(config_router, "deploy_in_flight", _window)

    with TestClient(app) as client:
        idle = client.get(f"/api/config/audit?machine={_REMOTE}")
        window = DeployWindow(active=True, detail=f"machine {_REMOTE!r} is mid-deploy")
        during = client.get(f"/api/config/audit?machine={_REMOTE}")

    assert idle.status_code == 503, idle.text
    assert during.status_code == 503, during.text
    assert [kwargs["quiet_unreachable"] for kwargs in seen] == [False, True]
