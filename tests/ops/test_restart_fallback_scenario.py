"""Diagnostic contract: a fresh fake model repeats restart after markerless fallback.

This proves the existing defect, not a corrected production restart protocol.
No actual child or atexit handler is installed; the real DB CAS and scenario
factory run against the isolated CI database.
"""

from __future__ import annotations

import atexit
import json
import subprocess
from collections.abc import Callable

import psycopg
import pytest

import ava
from agent import db as agent_db
from ops.agent_spawn import create_agent_row
from shared.machine import machine_name
from tests.e2e.fakes.scenarios import lifecycle_restart


def test_fallback_fresh_model_reissues_restart_without_completion_marker(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id, _birth = create_agent_row(spawner="test", machine=machine_name())
    monkeypatch.setattr(ava.self, "AGENT_ID", agent_id)
    initial = lifecycle_restart.build("diagnostic")
    assert initial.script == lifecycle_restart.RESTART_SCRIPT
    row = db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source,status) "
        "VALUES(%s,'','restart','self','claimed') RETURNING id",
        (agent_id,),
    ).fetchone()
    assert row is not None
    original_request = row[0]
    db_conn.execute("UPDATE agents_meta SET status='restarting' WHERE id=%s", (agent_id,))
    db_conn.commit()
    callbacks: list[Callable[[], None]] = []
    launches: list[object] = []

    def capture(callback: Callable[[], None]) -> None:
        callbacks.append(callback)

    def no_process(argv: object, **_kwargs: object) -> None:
        launches.append(argv)

    monkeypatch.setattr(atexit, "register", capture)
    monkeypatch.setattr(subprocess, "Popen", no_process)
    monkeypatch.setattr(agent_db, "self_respawn_restarter_grace_s", lambda: 0.0)
    agent_db.schedule_self_respawn(agent_id)
    assert len(callbacks) == 1
    callbacks[0]()
    assert len(launches) == 1
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("idling",)
    rows = db_conn.execute(
        "SELECT id,kind,source,status FROM inbound_messages WHERE agent_id=%s ORDER BY id",
        (agent_id,),
    ).fetchall()
    assert rows == [(original_request, "restart", "self", "claimed")]
    # Same persisted request, no additional chat or command. A newly built fake
    # still chooses restart because its sole discriminator is the missing marker.
    successor = lifecycle_restart.build("diagnostic")
    assert successor.cursor == 0
    assert successor.script == lifecycle_restart.RESTART_SCRIPT
    assert successor.script[0].tool_calls[0]["id"] == "call_restart"
    print(  # noqa: T201 — explicit CI-only diagnostic receipt, no production content.
        json.dumps({"agent": agent_id, "inbounds": rows, "freshModelReissuesRestart": True})
    )
