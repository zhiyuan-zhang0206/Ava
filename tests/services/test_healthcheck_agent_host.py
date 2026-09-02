"""Hosted agent-host healthcheck launch environment."""

from __future__ import annotations

import pytest

from services.healthchecks import agent_host as healthcheck


def test_agent_host_env_carries_projected_runner_url(monkeypatch: pytest.MonkeyPatch) -> None:
    projected_url = "postgresql://ava_runner:runner-password@127.0.0.1:5433/ava"

    def _project(_url: str) -> str:
        return projected_url

    monkeypatch.setattr(
        healthcheck,
        "runner_db_url_projection",
        _project,
    )

    assert healthcheck._agent_host_env() == {
        "AVA_PROCESS_PROFILE": "agent",
        "AVA_DB_URL": projected_url,
    }
