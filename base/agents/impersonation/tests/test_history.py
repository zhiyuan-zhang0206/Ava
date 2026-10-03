"""Permanent session history, numeric handles, and handoff projection contracts."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from base.agents import impersonation as leases
from base.agents.impersonation import history as history
from base.agents.impersonation import sessions as sessions
from base.cluster.machine import machine_name
from base.db import Database, create_agent, insert_inbound_message
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from tests.impersonation_support import attested_caller, recorded_tree, request_legacy_leases


@pytest.fixture
def owner(db_conn: psycopg.Connection[Any]) -> RuntimeIncarnation:
    agent_id = create_agent(db_conn)
    incarnation = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '10 minutes')",
        (agent_id, machine_name(), incarnation.generation, incarnation.owner),
    )
    db_conn.commit()
    return incarnation


def start(owner: RuntimeIncarnation, *, active: bool = True) -> dict[str, Any]:
    result = sessions.request(
        Database.from_settings(),
        EventBus.from_settings(),
        owner.agent_id,
        name="Fix login",
        executor_name="Codex: thoughtful squirrel",
        provider="codex",
        thread_id=str(uuid4()),
        process_metadata=recorded_tree(),
    )
    lease = history.resolve(Database.from_settings(), owner.agent_id, result["session_id"])
    if active:
        leases.accept(
            Database.from_settings(),
            EventBus.from_settings(),
            str(lease["id"]),
            owner.agent_id,
            owner,
            "Continue the login fix",
        )
        leases.activate(Database.from_settings(), EventBus.from_settings(), str(lease["id"]), owner)
        lease = history.resolve(Database.from_settings(), owner.agent_id, result["session_id"])
    return lease


def test_numbers_are_agent_scoped_and_permanent(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    database: Database,
    event_bus: EventBus,
) -> None:
    first = start(owner)
    assert first["session_id"] == 0
    leases.release(database, event_bus, str(first["id"]), attested_caller(first), "First result")
    with pytest.raises(leases.ImpersonationError, match="already has"):
        start(owner)
    # Simulate the native checkpoint receipt, then a second session.
    db_conn.execute(
        "UPDATE agent_impersonations SET handoff_applied_at=now() WHERE id=%s", (first["id"],)
    )
    db_conn.commit()
    second = start(owner)
    assert second["session_id"] == 1
    assert [s["id"] for s in sessions.list_sessions(database, owner.agent_id)] == [1, 0]
    assert sessions.list_sessions(database, owner.agent_id, before=1)[0]["id"] == 0
    other_agent = create_agent(db_conn)
    other_owner = RuntimeIncarnation(other_agent, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '10 minutes')",
        (other_agent, machine_name(), other_owner.generation, other_owner.owner),
    )
    db_conn.commit()
    assert start(other_owner, active=False)["session_id"] == 0
    assert "token_hash" not in sessions.list_sessions(database, owner.agent_id)[0]
    with (
        db_conn.transaction(force_rollback=True),
        pytest.raises(psycopg.errors.RaiseException, match="permanent"),
    ):
        db_conn.execute("DELETE FROM agent_impersonations WHERE id=%s", (first["id"],))


def test_concurrent_requests_cannot_share_control(owner: RuntimeIncarnation) -> None:
    def attempt(_index: int) -> int | None:
        try:
            return start(owner, active=False)["session_id"]
        except leases.ImpersonationError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert results.count(0) == 1
    assert results.count(None) == 1


def test_say_ack_and_file_preserve_all_message_bodies(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    event_bus: EventBus,
) -> None:
    def workspace_for_agent(_agent_id: int) -> Path:
        return tmp_path

    monkeypatch.setattr(history, "workspace_dir", workspace_for_agent)
    lease = start(owner)
    inbound = insert_inbound_message(
        db_conn, owner.agent_id, "Please fix login", source="user", bus=event_bus, database=database
    )
    db_conn.commit()
    read = leases.inbox(database, str(lease["id"]), attested_caller(lease))
    assert [row["id"] for row in read] == [inbound]
    leases.ack(database, event_bus, str(lease["id"]), attested_caller(lease), [inbound])
    first = history.say(
        database,
        event_bus,
        str(lease["id"]),
        attested_caller(lease),
        "I found the cause",
        message_key="progress-1",
    )
    assert (
        history.say(
            database,
            event_bus,
            str(lease["id"]),
            attested_caller(lease),
            "I found the cause",
            message_key="progress-1",
        )
        == first
    )
    with pytest.raises(ValueError, match="different content"):
        history.say(
            database,
            event_bus,
            str(lease["id"]),
            attested_caller(lease),
            "Different reply",
            message_key="progress-1",
        )
    leases.release(
        database, event_bus, str(lease["id"]), attested_caller(lease), "Login fixed; tests passed"
    )
    lease = history.resolve(database, owner.agent_id, 0)
    document, path = history.export_handoff(lease, db_conn)
    assert Path(path) == tmp_path / "impersonation" / "0.json"
    assert json.loads(Path(path).read_text()) == document
    assert [m["payload"]["content"] for m in document["messages"]] == [
        "Please fix login",
        "I found the cause",
    ]
    assert document["messages"][0]["acknowledged"] is True
    assert document["statistics"]["outgoing_messages"] == 1
    assert document["session"]["executor_name"] == "Codex: thoughtful squirrel"
    assert document["session"]["process_metadata"]["name"] == "python3.12"
    # Process facts live once on the session record, not on every message;
    # rows persisted with the former per-message copy still validate.
    said = document["messages"][1]["payload"]["impersonation"]
    assert said["executor_name"] == "Codex: thoughtful squirrel"
    assert "process" not in said
    legacy = history.ImpersonationMetadata.model_validate({**said, "process": {"pid": 1}})
    assert "process" not in legacy.model_dump()
    with (
        db_conn.transaction(force_rollback=True),
        pytest.raises(psycopg.errors.RaiseException, match="permanent"),
    ):
        db_conn.execute("DELETE FROM agent_impersonation_entries WHERE lease_id=%s", (lease["id"],))


def test_legacy_empty_events_never_certify_a_zero_call_claim(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    """A NULL protocol version is never inferred to be an empty manifest."""
    request_legacy_leases(monkeypatch)
    lease = start(owner)
    leases.release(
        database, event_bus, str(lease["id"]), attested_caller(lease), "SDK sampling was enabled"
    )
    document = history.build_document(
        history.resolve(database, owner.agent_id, 0), history.entries(str(lease["id"]), db_conn)
    )
    assert document["statistics"]["event_delivery"] == {
        "state": "pending",
        "pending_reason": "legacy",
        "completion_basis": None,
        "sdk_calls": {
            "coverage": "unknown",
            "sampling_policy": "unknown",
            "consumed_event_count": 0,
        },
        "api_events": {"coverage": "unknown", "consumed_event_count": 0},
    }
    assert document["statistics"]["sdk_sampling_policy"] == "unknown"


def test_export_handoff_rebuilds_a_cached_v1_document(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    """An upgrade retry exports the v2 fields the resumption note requires."""
    request_legacy_leases(monkeypatch)
    from psycopg.types.json import Jsonb

    def workspace_for_agent(_agent_id: int) -> Path:
        return tmp_path

    monkeypatch.setattr(history, "workspace_dir", workspace_for_agent)
    lease = start(owner)
    leases.release(database, event_bus, str(lease["id"]), attested_caller(lease), "Done")
    db_conn.execute(
        "UPDATE agent_impersonations SET handoff_document=%s WHERE id=%s",
        (Jsonb({"version": 1, "session": {"handoff_path": "old"}}), lease["id"]),
    )
    db_conn.commit()

    document, _ = history.export_handoff(history.resolve(database, owner.agent_id, 0), db_conn)

    assert document["version"] == 2
    assert document["statistics"]["event_delivery"]["state"] == "pending"


def test_message_retry_does_not_replace_newer_preview(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    database: Database,
    event_bus: EventBus,
) -> None:
    lease = start(owner)
    history.say(
        database, event_bus, str(lease["id"]), attested_caller(lease), "First", message_key="first"
    )
    history.say(
        database,
        event_bus,
        str(lease["id"]),
        attested_caller(lease),
        "Second",
        message_key="second",
    )
    history.say(
        database, event_bus, str(lease["id"]), attested_caller(lease), "First", message_key="first"
    )
    assert db_conn.execute(
        "SELECT last_message_text FROM agents_meta WHERE id=%s", (owner.agent_id,)
    ).fetchone() == ("Second",)


def test_public_session_exposes_handoff_applied_at(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    database: Database,
    event_bus: EventBus,
) -> None:
    """INC-927 closure read-face: the applied receipt must be visible.

    `ava impersonate list` projects through `public_session`; before this pin
    the whitelist omitted `handoff_applied_at`, so a caller reading the CLI
    output with `.get(...)` saw the missing key as an unapplied handoff while
    the column held a value.
    """
    lease = start(owner)
    leases.release(database, event_bus, str(lease["id"]), attested_caller(lease), "Done")
    db_conn.execute(
        "UPDATE agent_impersonations SET handoff_applied_at=now() WHERE id=%s", (lease["id"],)
    )
    db_conn.commit()
    applied = history.public_session(history.resolve(database, owner.agent_id, lease["session_id"]))
    assert applied["handoff_applied_at"] is not None
