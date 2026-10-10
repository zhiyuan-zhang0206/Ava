"""A retried `ava impersonate send` records exactly one event in the lease log."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest

from base.agents import impersonation as leases
from base.agents.impersonation import history as history
from base.agents.impersonation import sessions
from base.agents.messages import delivery_outbox as outbox
from base.cluster.machine import machine_name
from base.config import Settings
from base.config.service_read import ConfigAuthority
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from cli.commands.agents.impersonation import _send
from tests.impersonation_support import attested_caller, recorded_tree


class _SingleConnectionPool:
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def connection(self, *, timeout: float | None = None) -> Any:
        del timeout
        return nullcontext(self._conn)


def test_a_failed_send_replayed_from_the_outbox_logs_one_event(
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    database: Database,
    event_bus: EventBus,
    publish_wake: Callable[[int, str], bool],
) -> None:
    agent_id = create_agent(db_conn)
    owner = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '10 minutes')",
        (agent_id, machine_name(), owner.generation, owner.owner),
    )
    db_conn.commit()
    runtime = Settings(profile=None)
    authority = ConfigAuthority(runtime=runtime, all_domains=runtime, env_path=tmp_path / ".env")
    result = sessions.request(
        database,
        event_bus,
        owner.agent_id,
        authority=authority,
        name="Fix login",
        executor_name="Codex: thoughtful squirrel",
        provider="codex",
        thread_id=str(uuid4()),
        process_metadata=recorded_tree(),
    )
    lease = history.resolve(database, owner.agent_id, result["session_id"])
    leases.accept(
        database, event_bus, str(lease["id"]), owner.agent_id, owner, "Continue the login fix"
    )
    leases.activate(database, event_bus, str(lease["id"]), owner)
    lease = history.resolve(database, owner.agent_id, result["session_id"])

    target_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,lease_expires_at) "
        "VALUES(%s,'idling',%s,clock_timestamp()+interval '10 minutes')",
        (target_id, machine_name()),
    )
    db_conn.commit()
    snapshot = outbox.DeliveryOutboxLimits(
        enabled=True,
        retry_backoff_steps=(0.0,),
        budget_seconds=3600.0,
        abandoned_retention_days=30,
        dedup_window_seconds=900.0,
        flush_interval_seconds=1.0,
        max_entries=8,
    )
    monkeypatch.setenv("AVA_HOME", str(tmp_path))

    def read_limits(_authority: ConfigAuthority) -> outbox.DeliveryOutboxLimits:
        return snapshot

    monkeypatch.setattr(outbox, "limits", read_limits)
    outbox._reset_caches_for_tests()
    monkeypatch.setattr(
        "base.native_process.ownership.process_metadata", lambda: attested_caller(lease)
    )

    def gateway_down(*_args: Any, **_kwargs: Any) -> Any:
        raise httpx.ConnectError("gateway unavailable")

    monkeypatch.setattr("base.host.net.http_dial.post", gateway_down)
    args = argparse.Namespace(
        agent_id=owner.agent_id,
        session_id=0,
        target_agent_id=target_id,
        content="CLI event-log delivery",
    )
    with pytest.raises(httpx.TransportError):
        _send(args)
    entries = [
        entry
        for entry in (outbox._read(path) for path in outbox.journal_dir().glob("*.json"))
        if entry is not None
    ]
    assert len(entries) == 1
    entry = entries[0]
    assert entry.origin_agent_id == owner.agent_id

    assert entry.origin_agent_id == owner.agent_id
    pool = _SingleConnectionPool(db_conn)
    assert (
        outbox.flush(
            pool, publish_wake, authority=authority, now=datetime.now(UTC) + timedelta(seconds=1)
        ).delivered
        == 1
    )
    # A stale retry after the first response was lost carries the same key and must
    # only recover the committed receipt, not insert a second message or log row.
    outbox.record_failed_send(
        authority=authority,
        origin_agent_id=owner.agent_id,
        agent_id=target_id,
        source=f"agent:{owner.agent_id}",
        content="CLI event-log delivery",
        client_message_id=entry.client_message_id,
    )
    assert (
        outbox.flush(
            pool, publish_wake, authority=authority, now=datetime.now(UTC) + timedelta(seconds=1)
        ).delivered
        == 1
    )
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE client_message_id=%s",
        (entry.client_message_id,),
    ).fetchone() == (1,)

    leases.release(
        database, event_bus, str(lease["id"]), attested_caller(lease), "CLI delivery replayed"
    )
    api_events = [
        row["payload"]
        for row in history.entries(str(lease["id"]), db_conn)
        if row["kind"] == "api_event"
    ]
    assert [(event["event_name"], event["agent_id"]) for event in api_events] == [
        ("send_message", target_id)
    ]
    assert history.resolve(database, owner.agent_id, 0)["events_completed_at"] is not None
