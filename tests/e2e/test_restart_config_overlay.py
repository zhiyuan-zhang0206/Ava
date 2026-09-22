"""Restart config overlays validate on the profiled gateway boundary."""

from __future__ import annotations

import httpx
import psycopg
import pytest

from shared.config import settings
from tests.e2e._ports import GATEWAY_URL


@pytest.mark.scenario("tests.e2e.fakes.scenarios.idle_restart_silent:build")
def test_restart_config_overlay_persists_and_rejects_invalid_values(spawned_agent: int) -> None:
    agent_id = spawned_agent
    overlay = {"completion_notice_policy": "hourly"}

    response = httpx.post(
        f"{GATEWAY_URL}/api/agents/{agent_id}/restart",
        json={"source": "agent:405", "config_overlay": overlay},
        timeout=30.0,
    )

    assert response.status_code == 200
    assert response.json() == {"status": "enqueued"}
    with psycopg.connect(settings.data_plane.db_url) as conn:
        persisted_overlay = conn.execute(
            "SELECT config_overlay FROM agents_meta WHERE id = %s", (agent_id,)
        ).fetchone()
    assert persisted_overlay is not None
    assert persisted_overlay[0] == overlay

    invalid = httpx.post(
        f"{GATEWAY_URL}/api/agents/{agent_id}/restart",
        json={"config_overlay": {"completion_notice_policy": "bogus"}},
        timeout=30.0,
    )

    assert invalid.status_code == 422
    assert "completion_notice_policy" in invalid.text
