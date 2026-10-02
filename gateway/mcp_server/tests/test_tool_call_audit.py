"""An MCP tool call is recorded in `audit_events` after it ran.

The tool has already executed when the audit fact is written, so a failed write must not turn
a successful call into a tool error (the client would retry and repeat the side effect): it is
reported instead.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

from base import telemetry
from base.agents.messages.caller_identity import CallerIdentity
from gateway.mcp_server.endpoint import _record_tool_call


def _caller() -> CallerIdentity:
    return CallerIdentity(kind="external_agent", subject="mcp", instance=uuid.uuid4().hex[:12])


async def test_a_tool_call_is_recorded_with_its_outcome(db_conn: psycopg.Connection) -> None:
    caller = _caller()

    await _record_tool_call(caller, {"tool": "spawn_agent", "outcome": "ok"})

    rows = db_conn.execute(
        "SELECT agent_id, attributes FROM audit_events "
        "WHERE event_name='mcp_tool_call' AND source=%s",
        (caller.source(),),
    ).fetchall()
    [(agent_id, attributes)] = rows
    assert agent_id is None
    assert (attributes["tool"], attributes["outcome"]) == ("spawn_agent", "ok")


async def test_a_failed_audit_write_does_not_fail_the_tool_call(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    caller = _caller()
    reported: list[str] = []

    def refuse(_event: object) -> None:
        raise RuntimeError("database down")

    monkeypatch.setattr("base.telemetry.audit_events.record_audit_standalone", refuse)

    def emit(_category: str, name: str, **kwargs: Any) -> None:
        if name == "audit_write_failed":
            reported.append(kwargs["attributes"]["event_name"])

    monkeypatch.setattr(telemetry, "emit", emit)

    await _record_tool_call(caller, {"tool": "spawn_agent", "outcome": "ok"})

    assert reported == ["mcp_tool_call"]
    count = db_conn.execute(
        "SELECT count(*) FROM audit_events WHERE source=%s", (caller.source(),)
    ).fetchone()
    assert count == (0,)
