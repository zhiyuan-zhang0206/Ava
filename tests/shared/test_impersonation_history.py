"""Permanent session history, numeric handles, and handoff projection contracts."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString, cast
from uuid import uuid4

import psycopg
import pytest

from shared import impersonation as leases
from shared import impersonation_history as history
from shared import impersonation_sessions as sessions
from shared.db import create_agent, insert_inbound_message
from shared.impersonation_events import consume_events
from shared.machine import machine_name
from shared.runtime_incarnation import RuntimeIncarnation
from tests.impersonation_support import attested_caller, recorded_tree


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
        owner.agent_id,
        name="Fix login",
        executor_name="Codex: thoughtful squirrel",
        provider="codex",
        thread_id=str(uuid4()),
        process_metadata=recorded_tree(),
    )
    lease = history.resolve(owner.agent_id, result["session_id"])
    if active:
        leases.accept(str(lease["id"]), owner.agent_id, owner, "Continue the login fix")
        leases.activate(str(lease["id"]), owner)
        lease = history.resolve(owner.agent_id, result["session_id"])
    return lease


def test_numbers_are_agent_scoped_and_permanent(
    db_conn: psycopg.Connection[Any], owner: RuntimeIncarnation
) -> None:
    first = start(owner)
    assert first["session_id"] == 0
    leases.release(str(first["id"]), attested_caller(first), "First result")
    with pytest.raises(leases.ImpersonationError, match="already has"):
        start(owner)
    # Simulate the native checkpoint receipt, then a second session.
    db_conn.execute(
        "UPDATE agent_impersonations SET handoff_applied_at=now() WHERE id=%s", (first["id"],)
    )
    db_conn.commit()
    second = start(owner)
    assert second["session_id"] == 1
    assert [s["id"] for s in sessions.list_sessions(owner.agent_id)] == [1, 0]
    assert sessions.list_sessions(owner.agent_id, before=1)[0]["id"] == 0
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
    assert "token_hash" not in sessions.list_sessions(owner.agent_id)[0]
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
) -> None:
    def workspace_for_agent(_agent_id: int) -> Path:
        return tmp_path

    monkeypatch.setattr(history, "workspace_dir", workspace_for_agent)
    lease = start(owner)
    inbound = insert_inbound_message(db_conn, owner.agent_id, "Please fix login", source="user")
    db_conn.commit()
    read = leases.inbox(str(lease["id"]), attested_caller(lease))
    assert [row["id"] for row in read] == [inbound]
    leases.ack(str(lease["id"]), attested_caller(lease), [inbound])
    first = history.say(
        str(lease["id"]), attested_caller(lease), "I found the cause", message_key="progress-1"
    )
    assert (
        history.say(
            str(lease["id"]), attested_caller(lease), "I found the cause", message_key="progress-1"
        )
        == first
    )
    with pytest.raises(ValueError, match="different content"):
        history.say(
            str(lease["id"]), attested_caller(lease), "Different reply", message_key="progress-1"
        )
    leases.release(str(lease["id"]), attested_caller(lease), "Login fixed; tests passed")
    lease = history.resolve(owner.agent_id, 0)
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
    with (
        db_conn.transaction(force_rollback=True),
        pytest.raises(psycopg.errors.RaiseException, match="permanent"),
    ):
        db_conn.execute("DELETE FROM agent_impersonation_entries WHERE lease_id=%s", (lease["id"],))


def test_consumer_retains_sdk_facts_without_sampling_or_reinstrumentation(
    db_conn: psycopg.Connection[Any], owner: RuntimeIncarnation
) -> None:
    lease = start(owner)
    base = {
        "ts": datetime.now(UTC).isoformat(),
        "agent_id": owner.agent_id,
        "source": f"agent:{owner.agent_id}",
        "category": "telemetry",
    }
    events = [
        {
            **base,
            "id": 10,
            "event_name": "sdk_call",
            "attributes": {"fn": "ava.tasks.create", "duration": 0.25},
        },
        {
            **base,
            "id": 11,
            "event_name": "task_create",
            "category": "audit",
            "attributes": {"task_id": 99},
        },
    ]
    assert consume_events(owner.agent_id, 0, events) == 2
    assert consume_events(owner.agent_id, 0, events) == 0
    with pytest.raises(ValueError, match="another agent"):
        consume_events(owner.agent_id, 0, [{**events[0], "agent_id": owner.agent_id + 1}])
    leases.release(str(lease["id"]), attested_caller(lease), "Created task 99")
    result = history.build_document(
        history.resolve(owner.agent_id, 0), history.entries(str(lease["id"]), db_conn)
    )
    assert result["statistics"]["sdk_calls"] == {"ava.tasks.create": 1}
    assert result["statistics"]["sdk_duration_seconds"] == 0.25
    assert result["statistics"]["api_operations"] == {"task_create": 1}
    assert result["sdk_events"][0]["payload"] == events[0]


def test_timeline_pages_inside_a_session_using_existing_numeric_cursors(
    db_conn: psycopg.Connection[Any], owner: RuntimeIncarnation
) -> None:
    from agent.impersonation_handoff import start_marker
    from gateway.routers.timeline import _window_before
    from shared.impersonation_timeline import hydrate
    from shared.timeline import build_timeline_items

    lease = start(owner)
    marker = start_marker(lease)
    for number in range(15):
        history.say(
            str(lease["id"]), attested_caller(lease), f"Message {number}", message_key=str(number)
        )
    items, count = build_timeline_items([marker], [])
    page = hydrate(items, owner.agent_id, limit=5)
    assert count == 1
    assert len(page) == 7  # marker + limit+1 lookahead
    assert page[-1].payload == "Message 14"
    cursor = page[-5].item_id
    older = hydrate(items, owner.agent_id, limit=5, before=cursor)
    window, more = _window_before(older, cursor, 5)
    assert [item.payload for item in window] == [f"Message {i}" for i in range(5, 10)]
    assert more
    assert page[-1].impersonation is not None
    assert page[-1].impersonation.executor_name == "Codex: thoughtful squirrel"
    archived, _ = build_timeline_items([marker], [], segment_prefix="s2.checkpoint")
    archive_page = hydrate(archived, owner.agent_id, limit=5)
    assert archive_page[-1].item_id.startswith("s2.checkpoint.0.")


def test_late_events_refresh_handoff_after_native_receipt_and_manifest_closes_replay(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import httpx
    from psycopg.types.json import Jsonb

    from ava import _impersonation_events as reader
    from services.agent_host.impersonation_events import reconcile_one
    from shared.impersonation_events import complete_delivery

    def workspace_for_agent(_agent_id: int) -> Path:
        return tmp_path

    monkeypatch.setattr(history, "workspace_dir", workspace_for_agent)
    lease = start(owner)
    event: dict[str, Any] = {
        "id": "late-1",
        "ts": datetime.now(UTC).isoformat(),
        "agent_id": owner.agent_id,
        "event_name": "sdk_call",
        "category": "telemetry",
        "attributes": {"fn": "ava.files.read", "duration": 0.1},
    }
    leases.release(str(lease["id"]), attested_caller(lease), "Done")
    lease = history.resolve(owner.agent_id, 0)
    visible: list[dict[str, Any]] = []
    reads: list[dict[str, Any]] = []

    def get(path: str, *, params: dict[str, Any]) -> httpx.Response:
        reads.append(params)
        items = visible if params.get("event_name") == "sdk_call" else []
        return httpx.Response(
            200,
            request=httpx.Request("GET", "http://test" + path),
            json={"items": items, "meta": {"has_more": False}},
        )

    monkeypatch.setattr(reader, "_get", get)
    reader.consume_recorded_events(lease)
    document, path = history.export_handoff(lease, db_conn)
    assert document["statistics"]["event_delivery"] == "pending"
    db_conn.execute(
        "UPDATE agent_impersonations SET handoff_document=%s,handoff_path=%s,"
        "handoff_applied_at=now(),events_next_read_at=now() WHERE id=%s",
        (Jsonb(document), path, lease["id"]),
    )
    db_conn.commit()
    visible.append(event)  # Indexing completes after native resumption.
    reconcile_one()
    updated = json.loads(Path(path).read_text())
    assert updated["statistics"]["sdk_event_count"] == 1
    assert updated["statistics"]["event_delivery"] == "pending"
    with pytest.raises(ValueError, match="manifest differs"):
        complete_delivery(owner.agent_id, 0, [])
    complete_delivery(owner.agent_id, 0, [event["id"]])
    assert json.loads(Path(path).read_text())["statistics"]["event_delivery"] == "complete"
    count = len(reads)
    reader.consume_recorded_events(lease)  # stale caller snapshot rechecks DB receipt
    assert len(reads) == count
    with pytest.raises(ValueError, match="certified delivery"):
        consume_events(owner.agent_id, 0, [{**event, "id": "unexpected"}])


def test_message_retry_does_not_replace_newer_preview(
    db_conn: psycopg.Connection[Any], owner: RuntimeIncarnation
) -> None:
    lease = start(owner)
    history.say(str(lease["id"]), attested_caller(lease), "First", message_key="first")
    history.say(str(lease["id"]), attested_caller(lease), "Second", message_key="second")
    history.say(str(lease["id"]), attested_caller(lease), "First", message_key="first")
    assert db_conn.execute(
        "SELECT last_message_text FROM agents_meta WHERE id=%s", (owner.agent_id,)
    ).fetchone() == ("Second",)


def test_upgrade_preserves_legacy_credential_and_message_backfill(
    db_conn: psycopg.Connection[Any],
) -> None:
    from psycopg import sql

    root = Path(__file__).parents[2]
    up = root / "migrations/20260913T180056_named-impersonation-history.sql"
    down = root / "migrations/20260913T180056_named-impersonation-history.down.sql"
    agent_id = create_agent(db_conn)
    legacy_id = uuid4()
    db_conn.commit()
    with db_conn.transaction(force_rollback=True):
        db_conn.execute(sql.SQL(cast(LiteralString, down.read_text())))
        inserted = db_conn.execute(
            "INSERT INTO inbound_messages(agent_id,content,kind,source) "
            "VALUES(%s,'Legacy message','chat','user') RETURNING id",
            (agent_id,),
        ).fetchone()
        assert inserted is not None
        inbound = inserted[0]
        db_conn.execute(
            "INSERT INTO agent_impersonations(id,agent_id,source,machine,token_hash,status,"
            "ttl_seconds,expires_at,ended_at) VALUES(%s,%s,'external_agent:codex',%s,"
            "'existing-credential-hash','released',3600,now(),now())",
            (legacy_id, agent_id, machine_name()),
        )
        db_conn.execute(
            "INSERT INTO agent_impersonation_messages(lease_id,inbound_id,acknowledged_at) "
            "VALUES(%s,%s,now())",
            (legacy_id, inbound),
        )
        handoff = db_conn.execute(
            "INSERT INTO inbound_messages(agent_id,content,kind,source) "
            "VALUES(%s,'Legacy completion summary','chat','external_agent:codex') RETURNING id",
            (agent_id,),
        ).fetchone()
        assert handoff is not None
        db_conn.execute(
            "UPDATE agent_impersonations SET summary_inbound_id=%s WHERE id=%s",
            (handoff[0], legacy_id),
        )
        db_conn.execute(sql.SQL(cast(LiteralString, up.read_text())))
        row = db_conn.execute(
            "SELECT session_id,token_hash,id FROM agent_impersonations WHERE agent_id=%s",
            (agent_id,),
        ).fetchone()
        assert row == (0, "existing-credential-hash", legacy_id)
        records = history.entries(str(legacy_id), db_conn)
        assert records[1]["payload"]["content"] == "Legacy message"
        assert records[1]["payload"]["acknowledged_at"] is not None
        assert records[2]["payload"]["content"] == "Legacy completion summary"
        assert db_conn.execute(
            "SELECT summary FROM agent_impersonations WHERE id=%s", (legacy_id,)
        ).fetchone() == ("Legacy completion summary",)
        assert db_conn.execute(
            "SELECT impersonation_index FROM agents WHERE id=%s", (agent_id,)
        ).fetchone() == (1,)
        with pytest.raises(psycopg.errors.RaiseException, match="permanent"), db_conn.transaction():
            db_conn.execute(sql.SQL(cast(LiteralString, down.read_text())))


@pytest.mark.parametrize("automatic", [True, False])
def test_inbound_attachments_survive_timeline_and_handoff(
    db_conn: psycopg.Connection[Any], owner: RuntimeIncarnation, automatic: bool
) -> None:
    from psycopg.types.json import Jsonb

    from agent.impersonation_handoff import start_marker
    from shared.impersonation_timeline import hydrate
    from shared.timeline import build_timeline_items
    from shared.uploads import upload_url

    lease = start(owner)
    db_conn.execute(
        "UPDATE agent_impersonations SET automatic=%s WHERE id=%s", (automatic, lease["id"])
    )
    valid_url = upload_url(owner.agent_id, "screenshot.png")
    payload = {
        "content_blocks": [
            {"type": "image_url", "image_url": {"url": valid_url}},
            {
                "type": "image_url",
                "image_url": {"url": upload_url(owner.agent_id + 1, "private.png")},
            },
        ]
    }
    inserted = db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,kind,source,content,payload) "
        "VALUES(%s,'chat','user','[image]',%s) RETURNING id",
        (owner.agent_id, Jsonb(payload)),
    ).fetchone()
    assert inserted is not None
    db_conn.commit()
    leases.inbox(str(lease["id"]), attested_caller(lease))
    leases.ack(str(lease["id"]), attested_caller(lease), [inserted[0]])
    items, _ = build_timeline_items([start_marker(lease)], [])
    projected = hydrate(items, owner.agent_id, limit=5)
    image_item = next(item for item in projected if item.inbound_id == inserted[0])
    assert image_item.images == [valid_url]
    document = history.build_document(lease, history.entries(str(lease["id"]), db_conn))
    assert document["messages"][0]["payload"]["payload"] == payload
    assert document["messages"][0]["acknowledged"] is True
