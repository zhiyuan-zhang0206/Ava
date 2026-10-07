"""Real maintenance admission fixtures shared by caller and hosted-runtime tests."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest

from base.cluster.machine import machine_name
from base.db import create_agent
from base.deploy.maintenance import admission, pause_owner

WHEN = datetime(2026, 9, 6, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")


def maintenance_agent(conn: psycopg.Connection[Any]) -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling',%s)",
        (agent_id, machine_name()),
    )
    conn.commit()
    return agent_id


def start_cluster_through_ready_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    from base.deploy.lifecycle import start_serving
    from cli.commands.lifecycle._pause_resume import resume_after_start

    monkeypatch.setattr("base.deploy.state.host_deploy_state.set_posture", MagicMock())
    monkeypatch.setattr("ops.agent_pause.publish_inbound_wake", MagicMock())
    monkeypatch.setattr(start_serving, "is_serving", lambda: True)

    @resume_after_start
    def ready_start() -> int:
        admission.require_start_allowed()
        return 0

    assert ready_start() == 0
    assert not admission.held()
