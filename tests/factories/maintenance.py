"""Real maintenance admission fixtures shared by caller and hosted-runtime tests.

Depends on `base` only; `maintenance_cluster` holds the part that drives the CLI start path.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.db import create_agent
from base.deploy.maintenance import pause_owner

WHEN = datetime(2026, 9, 6, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")


def maintenance_agent(conn: psycopg.Connection[Any], *, machine_name: str) -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling',%s)",
        (agent_id, machine_name),
    )
    conn.commit()
    return agent_id
