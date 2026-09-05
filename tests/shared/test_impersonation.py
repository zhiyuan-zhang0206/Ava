"""Database leases, explicit consent, and durable handoff/inbox invariants."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, LiteralString, cast
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from shared import impersonation as leases
from shared.caller_identity import CallerIdentity
from shared.config import settings
from shared.db import create_agent, insert_inbound_message
from shared.live_events import Cancelled
from shared.machine import machine_name
from shared.runtime_incarnation import RuntimeIncarnation


def _agent(conn: psycopg.Connection) -> RuntimeIncarnation:
    agent_id = create_agent(conn)
    owner = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '10 minutes')",
        (agent_id, machine_name(), owner.generation, owner.owner),
    )
    conn.commit()
    return owner


def _status(owner: RuntimeIncarnation) -> dict[str, Any]:
    result = leases.native_status(owner.agent_id, owner)
    assert result is not None
    return result


def _request(owner: RuntimeIncarnation) -> dict[str, Any]:
    return leases.request(
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
        ttl_seconds=300,
        reason="Handle the next message",
    )


def _active(owner: RuntimeIncarnation) -> dict[str, Any]:
    lease = _request(owner)
    leases.accept(lease["id"], owner.agent_id, owner)
    leases.activate(lease["id"], owner)
    return lease


def test_request_needs_consent_then_native_checkpoint_ack(db_conn: psycopg.Connection) -> None:
    owner = _agent(db_conn)
    lease = _request(owner)
    assert lease["status"] == "requested"
    assert "token_hash" not in lease
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)
    db_conn.commit()
    assert _status(owner)["reason"] == "Handle the next message"
    with pytest.raises(leases.ImpersonationError, match="not active"):
        leases.require_active(lease["id"], lease["token"])
    leases.accept(lease["id"], owner.agent_id, owner)
    assert leases.is_paused(owner.agent_id)
    with pytest.raises(leases.ImpersonationError, match="not active"):
        leases.inbox(lease["id"], lease["token"])
    leases.activate(lease["id"], owner)
    assert leases.require_active(lease["id"], lease["token"])["status"] == "active"


def test_consent_and_activation_cannot_use_another_incarnation(db_conn: psycopg.Connection) -> None:
    owner = _agent(db_conn)
    lease = _request(owner)
    stale = RuntimeIncarnation(owner.agent_id, uuid4(), uuid4())
    with pytest.raises(leases.ImpersonationError, match="no longer owns"):
        leases.accept(lease["id"], owner.agent_id, stale)
    leases.accept(lease["id"], owner.agent_id, owner)
    db_conn.execute(
        "UPDATE agents_meta SET runtime_generation=%s,runtime_owner=%s WHERE id=%s",
        (stale.generation, stale.owner, owner.agent_id),
    )
    db_conn.commit()
    with pytest.raises(leases.ImpersonationError, match="another native incarnation"):
        leases.activate(lease["id"], stale)


def test_inbox_ack_and_atomic_real_sender_handoff(db_conn: psycopg.Connection) -> None:
    owner = _agent(db_conn)
    lease = _active(owner)
    first = insert_inbound_message(db_conn, owner.agent_id, "first", "agent:99")
    second = insert_inbound_message(db_conn, owner.agent_id, "second", "agent:99")
    with pytest.raises(leases.ImpersonationError, match="not read"):
        leases.ack(lease["id"], lease["token"], [first])
    assert [m["id"] for m in leases.inbox(lease["id"], lease["token"])] == [first, second]
    leases.ack(lease["id"], lease["token"], [first])
    assert [m["id"] for m in leases.inbox(lease["id"], lease["token"])] == [second]
    result = leases.release(lease["id"], lease["token"], "Finished first; second still needs work.")
    assert result["status"] == "released"
    assert not leases.is_paused(owner.agent_id)
    rows = db_conn.execute(
        "SELECT id,status,source,payload FROM inbound_messages ORDER BY id"
    ).fetchall()
    assert rows[0][:3] == (first, "done", "agent:99")
    assert rows[1][:3] == (second, "pending", "agent:99")
    assert rows[2][1:3] == ("pending", "external_agent:codex:test")
    assert rows[2][3]["caller_identity"]["subject"] == "codex"
    db_conn.commit()
    assert leases.release(lease["id"], lease["token"], "Retry") == result
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (3,)


def test_external_inbox_preserves_cancel_until_explicit_processing_ack(
    db_conn: psycopg.Connection,
) -> None:
    owner = _agent(db_conn)
    lease = _active(owner)
    cancel = insert_inbound_message(
        db_conn, owner.agent_id, "Stop current work", "user", kind="cancel"
    )
    assert [(m["id"], m["kind"]) for m in leases.inbox(lease["id"], lease["token"])] == [
        (cancel, "cancel")
    ]
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (cancel,)
    ).fetchone() == ("pending",)
    db_conn.commit()
    leases.ack(lease["id"], lease["token"], [cancel])
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (cancel,)
    ).fetchone() == ("done",)


def test_cancel_ack_publishes_committed_completion_once(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _agent(db_conn)
    lease = _active(owner)
    cancel = insert_inbound_message(db_conn, owner.agent_id, "Stop", "user", kind="cancel")
    peer = insert_inbound_message(db_conn, owner.agent_id, "Peer work", "agent:99")
    leases.inbox(lease["id"], lease["token"])
    unread = insert_inbound_message(db_conn, owner.agent_id, "Unread", "user", kind="cancel")
    published: list[Cancelled] = []

    def publish(channel: str, payload: str, *, context: str = "") -> int:
        assert channel == settings.data_plane.events_channel
        # A separate connection must see both writes before the UI is told
        # the external actor completed cancellation.
        assert db_conn.execute(
            "SELECT i.status,m.acknowledged_at IS NOT NULL FROM inbound_messages i "
            "JOIN agent_impersonation_messages m ON m.inbound_id=i.id "
            "WHERE i.id=%s AND m.lease_id=%s",
            (cancel, lease["id"]),
        ).fetchone() == ("done", True)
        db_conn.commit()
        published.append(Cancelled.model_validate_json(payload))
        return 1

    monkeypatch.setattr("shared.redis_client.publish_best_effort_sync", publish)
    with pytest.raises(leases.ImpersonationError, match="not read"):
        leases.ack(lease["id"], lease["token"], [cancel, unread])
    assert published == []
    leases.ack(lease["id"], lease["token"], [peer])
    assert published == []
    leases.ack(lease["id"], lease["token"], [cancel])
    assert published == [Cancelled(agent_id=owner.agent_id)]
    leases.ack(lease["id"], lease["token"], [cancel, peer])
    assert len(published) == 1


def test_ttl_revokes_all_borrower_operations_and_preserves_pending(
    db_conn: psycopg.Connection,
) -> None:
    owner = _agent(db_conn)
    lease = _active(owner)
    pending = insert_inbound_message(db_conn, owner.agent_id, "durable", "agent:99")
    leases.inbox(lease["id"], lease["token"])
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    operations = [
        lambda: leases.require_active(lease["id"], lease["token"]),
        lambda: leases.renew(lease["id"], lease["token"]),
        lambda: leases.inbox(lease["id"], lease["token"]),
        lambda: leases.ack(lease["id"], lease["token"], [pending]),
        lambda: leases.release(lease["id"], lease["token"], "Late summary"),
        lambda: leases.merge_plugin_delta(lease["id"], lease["token"], {}, expected_version=0),
    ]
    for operation in operations:
        with pytest.raises(leases.ImpersonationError, match="TTL has expired"):
            operation()
    assert _status(owner)["status"] == "expired"
    assert not leases.is_paused(owner.agent_id)
    rows = db_conn.execute("SELECT id,status,source FROM inbound_messages ORDER BY id").fetchall()
    assert rows[0] == (pending, "pending", "agent:99")
    assert rows[1][1:] == ("pending", "system:impersonation")


def test_plugin_journal_blocks_new_lease_until_checkpoint_receipt(
    db_conn: psycopg.Connection,
) -> None:
    owner = _agent(db_conn)
    lease = _active(owner)
    leases.merge_plugin_delta(lease["id"], lease["token"], {"encoded": "first"}, expected_version=0)
    with pytest.raises(leases.ImpersonationError, match="Concurrent"):
        leases.merge_plugin_delta(
            lease["id"], lease["token"], {"encoded": "lost"}, expected_version=0
        )
    with pytest.raises(leases.ImpersonationError, match="returned native"):
        leases.mark_plugin_applied(lease["id"], 1, owner)
    leases.release(lease["id"], lease["token"], "State updated")
    with pytest.raises(leases.ImpersonationError, match="unapplied state"):
        _request(owner)
    state = _status(owner)
    assert state["plugin_delta"] == [{"encoded": "first"}]
    leases.mark_plugin_applied(lease["id"], 1, owner)
    assert _request(owner)["status"] == "requested"


def test_competing_requests_have_one_winner(db_conn: psycopg.Connection) -> None:
    owner = _agent(db_conn)

    def attempt(_index: int) -> str:
        try:
            return _request(owner)["status"]
        except leases.ImpersonationError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, range(2)))
    assert sorted(results) == ["conflict", "requested"]


def test_token_and_same_machine_checks(db_conn: psycopg.Connection) -> None:
    owner = _agent(db_conn)
    lease = _active(owner)
    with pytest.raises(leases.ImpersonationError, match="token"):
        leases.require_active(lease["id"], "incorrect")
    db_conn.execute("UPDATE agents_meta SET machine='another-host' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    with pytest.raises(leases.ImpersonationError, match="placement"):
        leases.require_active(lease["id"], lease["token"])
    with pytest.raises(leases.ImpersonationError, match="own machine"):
        _request(owner)


def test_termination_atomically_revokes_but_restart_preserves(db_conn: psycopg.Connection) -> None:
    owner = _agent(db_conn)
    lease = _active(owner)
    db_conn.execute("UPDATE agents_meta SET status='restarting' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    assert leases.get(lease["id"], lease["token"])["status"] == "active"
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    assert leases.get(lease["id"], lease["token"])["status"] == "expired"
    db_conn.execute("UPDATE agents_meta SET status='idling' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    with pytest.raises(leases.ImpersonationError, match="not active"):
        leases.require_active(lease["id"], lease["token"])


def test_replacement_requires_fresh_consent_before_checkpoint_ack(
    db_conn: psycopg.Connection,
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner)
    leases.accept(lease["id"], owner.agent_id, owner)
    replacement = RuntimeIncarnation(owner.agent_id, uuid4(), uuid4())
    db_conn.execute(
        "UPDATE agents_meta SET runtime_generation=%s,runtime_owner=%s WHERE id=%s",
        (replacement.generation, replacement.owner, owner.agent_id),
    )
    db_conn.commit()
    state = _status(replacement)
    assert state["status"] == "requested"
    assert state["consent_version"] == 2
    leases.accept(lease["id"], owner.agent_id, replacement)
    assert leases.activate(lease["id"], replacement)["status"] == "active"


def test_expiry_between_driver_read_and_activation_returns_control(
    db_conn: psycopg.Connection,
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner)
    leases.accept(lease["id"], owner.agent_id, owner)
    assert _status(owner)["status"] == "accepted"
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    assert leases.activate(lease["id"], owner)["status"] == "expired"
    assert not leases.is_paused(owner.agent_id)


def test_renew_replaces_ttl_and_reject_records_reason(db_conn: psycopg.Connection) -> None:
    owner = _agent(db_conn)
    lease = _request(owner)
    result = leases.reject(
        lease["id"], owner.agent_id, owner, "Finish the critical operation first"
    )
    assert result["rejection_reason"] == "Finish the critical operation first"
    lease = _active(owner)
    renewed = leases.renew(lease["id"], lease["token"], ttl_seconds=600)
    assert renewed["ttl_seconds"] == 600
    assert renewed["expires_at"] > lease["expires_at"]


def test_rollback_refuses_active_lease_or_pending_handoff(db_conn: psycopg.Connection) -> None:
    owner = _agent(db_conn)
    lease = _active(owner)
    migration = (
        Path(__file__).resolve().parents[2]
        / "migrations/20260905T073254_agent-impersonation.down.sql"
    )
    migration_sql = sql.SQL(cast(LiteralString, migration.read_text()))
    with (
        pytest.raises(psycopg.errors.RaiseException, match="Finish impersonations"),
        db_conn.transaction(force_rollback=True),
    ):
        db_conn.execute(migration_sql)
    leases.release(lease["id"], lease["token"], "Complete")
    with (
        pytest.raises(psycopg.errors.RaiseException, match="Finish impersonations"),
        db_conn.transaction(force_rollback=True),
    ):
        db_conn.execute(migration_sql)
    db_conn.execute(
        "UPDATE inbound_messages SET status='done' WHERE agent_id=%s", (owner.agent_id,)
    )
    db_conn.commit()
    with db_conn.transaction(force_rollback=True):
        db_conn.execute(migration_sql)
        assert db_conn.execute("SELECT to_regclass('agent_impersonations')").fetchone() == (None,)


def test_reaper_expires_offline_lease_and_keeps_unconsumed_handoff(
    db_conn: psycopg.Connection,
) -> None:
    from shared.db import pool
    from shared.impersonation_maintenance import reap_impersonations

    owner = _agent(db_conn)
    lease = _active(owner)
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    with pool(max_size=2) as reaper_pool:
        assert reap_impersonations(reaper_pool) == 1
        db_conn.execute(
            "UPDATE agent_impersonations SET ended_at=clock_timestamp()-interval '8 days' WHERE id=%s",
            (lease["id"],),
        )
        db_conn.commit()
        assert reap_impersonations(reaper_pool) == 0
        assert leases.get(lease["id"], lease["token"])["status"] == "expired"
        db_conn.execute(
            "UPDATE inbound_messages SET status='done' WHERE agent_id=%s", (owner.agent_id,)
        )
        db_conn.commit()
        reap_impersonations(reaper_pool)
    assert db_conn.execute("SELECT count(*) FROM agent_impersonations").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)
