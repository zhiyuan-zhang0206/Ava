"""Database leases, explicit consent, and durable handoff/inbox invariants."""

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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


def _request(
    owner: RuntimeIncarnation, *, provider: str = "codex", thread: str | None = None
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if provider == "codex":
        kwargs["relay_thread_id"] = thread or str(uuid4())
    return leases.request(
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
        ttl_seconds=300,
        reason="Handle the next message",
        relay_provider=provider,
        **kwargs,
    )


def _active(owner: RuntimeIncarnation) -> dict[str, Any]:
    lease = _request(owner)
    leases.accept(lease["id"], owner.agent_id, owner, "Handoff brief")
    leases.activate(lease["id"], owner)
    return lease


@pytest.fixture
def short_lock_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    transaction = leases.write_transaction
    connect = leases.connect

    @contextmanager
    def bounded_transaction() -> Generator[psycopg.Connection]:
        with transaction() as conn:
            conn.execute("SET LOCAL lock_timeout='100ms'")
            yield conn

    @contextmanager
    def bounded_read() -> Generator[psycopg.Connection]:
        with connect() as conn:
            conn.execute("SET LOCAL lock_timeout='100ms'")
            yield conn

    monkeypatch.setattr(leases, "write_transaction", bounded_transaction)
    monkeypatch.setattr(leases, "connect", bounded_read)


def test_native_without_lease_does_not_contend_with_agent_writes(
    db_conn: psycopg.Connection, short_lock_timeout: None
) -> None:
    owner = _agent(db_conn)
    db_conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (owner.agent_id,))
    assert leases.native_status(owner.agent_id, owner) is None


def test_external_identity_read_does_not_contend_with_lease_writes(
    db_conn: psycopg.Connection, short_lock_timeout: None
) -> None:
    owner = _agent(db_conn)
    lease = _active(owner)
    db_conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (owner.agent_id,))
    db_conn.execute("SELECT id FROM agent_impersonations WHERE id=%s FOR UPDATE", (lease["id"],))
    result = leases.require_active(lease["id"], lease["token"])
    assert result["status"] == "active"
    assert set(result) == set(lease) - {"token"}


def test_existing_lease_still_serializes_native_reconciliation(
    db_conn: psycopg.Connection, short_lock_timeout: None
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner)
    leases.accept(lease["id"], owner.agent_id, owner, "Handoff brief")
    db_conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (owner.agent_id,))
    with pytest.raises(psycopg.errors.LockNotAvailable):
        leases.native_status(owner.agent_id, owner)
    db_conn.rollback()
    assert _status(owner)["status"] == "accepted"


@pytest.mark.parametrize("invalid", ["owner", "ttl", "status"])
def test_native_without_lease_still_requires_current_ownership(
    db_conn: psycopg.Connection, invalid: str
) -> None:
    owner = _agent(db_conn)
    if invalid == "owner":
        owner = RuntimeIncarnation(owner.agent_id, uuid4(), uuid4())
    elif invalid == "ttl":
        db_conn.execute(
            "UPDATE agents_meta SET lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE id=%s",
            (owner.agent_id,),
        )
    else:
        db_conn.execute("UPDATE agents_meta SET status='restarting' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    with pytest.raises(leases.ImpersonationError, match="no longer owns"):
        leases.native_status(owner.agent_id, owner)


def test_accept_requires_a_nonempty_start_message(db_conn: psycopg.Connection) -> None:
    owner = _agent(db_conn)
    lease = _request(owner)
    for empty in ("", "   "):
        with pytest.raises(leases.ImpersonationError, match="start message is required"):
            leases.accept(lease["id"], owner.agent_id, owner, empty)
    assert _status(owner)["status"] == "requested"
    leases.accept(lease["id"], owner.agent_id, owner, "Do the implementation, then summarize.")
    state = _status(owner)
    assert state["status"] == "accepted"
    assert state["start_message"] == "Do the implementation, then summarize."


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
    leases.accept(lease["id"], owner.agent_id, owner, "Handoff brief")
    assert _status(owner)["status"] == "accepted"
    with pytest.raises(leases.ImpersonationError, match="not active"):
        leases.inbox(lease["id"], lease["token"])
    leases.activate(lease["id"], owner)
    assert leases.require_active(lease["id"], lease["token"])["status"] == "active"


def test_consent_and_activation_cannot_use_another_incarnation(db_conn: psycopg.Connection) -> None:
    owner = _agent(db_conn)
    lease = _request(owner)
    stale = RuntimeIncarnation(owner.agent_id, uuid4(), uuid4())
    with pytest.raises(leases.ImpersonationError, match="no longer owns"):
        leases.accept(lease["id"], owner.agent_id, stale, "Handoff brief")
    leases.accept(lease["id"], owner.agent_id, owner, "Handoff brief")
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
    assert leases.native_status(owner.agent_id, owner) is None
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
    wakes: list[int] = []

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
    monkeypatch.setattr(leases, "_wake", wakes.append)
    with pytest.raises(leases.ImpersonationError, match="not read"):
        leases.ack(lease["id"], lease["token"], [cancel, unread])
    assert published == []
    assert wakes == []
    leases.ack(lease["id"], lease["token"], [peer])
    assert published == []
    assert wakes == [owner.agent_id]
    leases.ack(lease["id"], lease["token"], [cancel])
    assert published == [Cancelled(agent_id=owner.agent_id)]
    assert wakes == [owner.agent_id, owner.agent_id]
    leases.ack(lease["id"], lease["token"], [cancel, peer])
    assert len(published) == 1
    assert wakes == [owner.agent_id, owner.agent_id]


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
    assert leases.native_status(owner.agent_id, owner) is None
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
    leases.accept(lease["id"], owner.agent_id, owner, "Handoff brief")
    replacement = RuntimeIncarnation(owner.agent_id, uuid4(), uuid4())
    db_conn.execute(
        "UPDATE agents_meta SET runtime_generation=%s,runtime_owner=%s WHERE id=%s",
        (replacement.generation, replacement.owner, owner.agent_id),
    )
    db_conn.commit()
    state = _status(replacement)
    assert state["status"] == "requested"
    assert state["consent_version"] == 2
    leases.accept(lease["id"], owner.agent_id, replacement, "Handoff brief")
    assert leases.activate(lease["id"], replacement)["status"] == "active"


def test_expiry_between_driver_read_and_activation_returns_control(
    db_conn: psycopg.Connection,
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner)
    leases.accept(lease["id"], owner.agent_id, owner, "Handoff brief")
    assert _status(owner)["status"] == "accepted"
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    assert leases.activate(lease["id"], owner)["status"] == "expired"
    assert leases.native_status(owner.agent_id, owner) is None


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


def test_request_requires_a_relay_binding(db_conn: psycopg.Connection) -> None:
    owner = _agent(db_conn)
    with pytest.raises(ValueError, match="provider"):
        leases.request(
            owner.agent_id,
            caller=CallerIdentity(kind="external_agent", subject="codex"),
            relay_provider="nope",
        )
    with pytest.raises(ValueError, match="thread id"):
        leases.request(
            owner.agent_id,
            caller=CallerIdentity(kind="external_agent", subject="codex"),
            relay_provider="codex",
        )
    with pytest.raises(ValueError, match="thread id and remote are rejected"):
        leases.request(
            owner.agent_id,
            caller=CallerIdentity(kind="external_agent", subject="codex"),
            relay_provider="claude",
            relay_thread_id=str(uuid4()),
        )


def test_claude_request_mints_a_scoped_relay_credential(db_conn: psycopg.Connection) -> None:
    owner = _agent(db_conn)
    lease = _request(owner, provider="claude", thread=None)
    assert lease["relay_provider"] == "claude"
    assert lease.get("relay_token")
    assert "relay_token_hash" not in lease
    assert leases.relay_get(lease["id"], lease["relay_token"])["status"] == "requested"
    with pytest.raises(leases.ImpersonationError, match="Invalid relay token"):
        leases.relay_get(lease["id"], lease["token"])
    with pytest.raises(leases.ImpersonationError, match="Invalid relay token"):
        leases.relay_get(lease["id"], "wrong")


def test_request_validates_and_records_the_batch_window(db_conn: psycopg.Connection) -> None:
    owner = _agent(db_conn)
    lease = _request(owner, provider="codex")
    assert lease["relay_batch_window_seconds"] == 30  # default: merge enabled
    for bad in (-1, 301, 1.5, True, "30"):
        with pytest.raises(ValueError, match="relay_batch_window_seconds"):
            leases.request(
                owner.agent_id,
                caller=CallerIdentity(kind="external_agent", subject="codex"),
                relay_provider="codex",
                relay_thread_id=str(uuid4()),
                relay_batch_window_seconds=bad,  # type: ignore[arg-type] — the runtime check rejects non-ints
            )
    with pytest.raises(leases.ImpersonationError, match="already has"):
        _request(owner, provider="codex")  # first lease still open
    leases.reject(lease["id"], owner.agent_id, owner, "window probe done")
    window_off = leases.request(
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
        relay_batch_window_seconds=0,
    )
    assert window_off["relay_batch_window_seconds"] == 0


def test_codex_request_defers_relay_credential_to_activation(db_conn: psycopg.Connection) -> None:
    owner = _agent(db_conn)
    lease = _request(owner)
    assert lease["relay_provider"] == "codex"
    assert lease["relay_thread_id"]
    assert "relay_token" not in lease
    with pytest.raises(leases.ImpersonationError, match="Invalid relay token"):
        leases.relay_get(lease["id"], "any")


def test_accept_without_relay_binding_fails_loudly(db_conn: psycopg.Connection) -> None:
    # A legacy-shape lease (created before the relay binding existed) has all
    # relay fields NULL; accepting it must fail loudly, never guess an endpoint.
    owner = _agent(db_conn)
    legacy_id = uuid4()
    db_conn.execute(
        "INSERT INTO agent_impersonations(id,agent_id,source,machine,token_hash,reason,"
        "status,ttl_seconds,expires_at) VALUES(%s,%s,'external_agent:codex:old',%s,%s,'',"
        "'requested',300,clock_timestamp()+interval '5 minutes')",
        (legacy_id, owner.agent_id, machine_name(), "legacy-hash"),
    )
    db_conn.commit()
    with pytest.raises(leases.ImpersonationError, match="no relay binding"):
        leases.accept(str(legacy_id), owner.agent_id, owner, "Handoff brief")


def test_provision_relay_only_for_the_accepting_incarnation(db_conn: psycopg.Connection) -> None:
    owner = _agent(db_conn)
    lease = _request(owner)
    leases.accept(lease["id"], owner.agent_id, owner, "Handoff brief")
    foreign = RuntimeIncarnation(owner.agent_id, uuid4(), uuid4())
    with pytest.raises(leases.ImpersonationError):
        leases.provision_relay(lease["id"], foreign, "relay-credential")
    provisioned = leases.provision_relay(lease["id"], owner, "relay-credential")
    assert "relay_token_hash" not in provisioned
    assert leases.relay_get(lease["id"], "relay-credential")["status"] == "accepted"


def test_provision_relay_revokes_the_previous_credential(db_conn: psycopg.Connection) -> None:
    owner = _agent(db_conn)
    lease = _request(owner)
    leases.accept(lease["id"], owner.agent_id, owner, "Handoff brief")
    leases.activate(lease["id"], owner)
    leases.provision_relay(lease["id"], owner, "first")
    leases.provision_relay(lease["id"], owner, "second")
    with pytest.raises(leases.ImpersonationError, match="Invalid relay token"):
        leases.relay_get(lease["id"], "first")
    assert leases.relay_get(lease["id"], "second")["status"] == "active"


def test_active_lease_binding_inherits_the_replacement_incarnation(
    db_conn: psycopg.Connection,
) -> None:
    """Every restart/host turnover mints a fresh incarnation; the active lease's
    accepting binding must follow it so relay supervision can re-provision."""
    owner = _agent(db_conn)
    lease = _active(owner)
    replacement = RuntimeIncarnation(owner.agent_id, uuid4(), uuid4())
    db_conn.execute(
        "UPDATE agents_meta SET runtime_generation=%s,runtime_owner=%s WHERE id=%s",
        (replacement.generation, replacement.owner, owner.agent_id),
    )
    db_conn.commit()
    state = _status(replacement)
    assert state["status"] == "active"
    assert (state["accepted_generation"], state["accepted_owner"]) == (
        str(replacement.generation),
        str(replacement.owner),
    )
    provisioned = leases.provision_relay(lease["id"], replacement, "relay-credential")
    assert "relay_token_hash" not in provisioned
    assert leases.relay_get(lease["id"], "relay-credential")["status"] == "active"
    # The old incarnation cannot mint the relay credential after the transfer.
    with pytest.raises(leases.ImpersonationError):
        leases.provision_relay(lease["id"], owner, "old-incarnation-credential")


def test_relay_inbox_uses_the_scoped_credential_only(db_conn: psycopg.Connection) -> None:
    owner = _agent(db_conn)
    lease = _request(owner, provider="claude", thread=None)
    leases.accept(lease["id"], owner.agent_id, owner, "Handoff brief")
    leases.activate(lease["id"], owner)
    insert_inbound_message(db_conn, owner.agent_id, "hello", "user", kind="chat")
    db_conn.commit()
    with pytest.raises(leases.ImpersonationError, match="Invalid relay token"):
        leases.relay_inbox(lease["id"], lease["token"])
    rows = leases.relay_inbox(lease["id"], lease["relay_token"])
    assert [row["content"] for row in rows] == ["hello"]
    with pytest.raises(leases.ImpersonationError):
        leases.inbox(lease["id"], lease["relay_token"])


def test_relay_heartbeat_beats_while_open_and_stops_at_terminal(
    db_conn: psycopg.Connection,
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner, provider="claude", thread=None)
    leases.relay_heartbeat(lease["id"], lease["relay_token"])
    row = leases.relay_get(lease["id"], lease["relay_token"])
    assert row["relay_heartbeat_at"] is not None
    leases.accept(lease["id"], owner.agent_id, owner, "Handoff brief")
    leases.activate(lease["id"], owner)
    leases.relay_heartbeat(lease["id"], lease["relay_token"])
    leases.release(lease["id"], lease["token"], "Done")
    with pytest.raises(leases.ImpersonationError, match="ended"):
        leases.relay_heartbeat(lease["id"], lease["relay_token"])


def test_fail_acceptance_rolls_back_with_reason_and_native_note(
    db_conn: psycopg.Connection,
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner)
    leases.accept(lease["id"], owner.agent_id, owner, "Handoff brief")
    result = leases.fail_acceptance(lease["id"], owner, "relay process exited at startup")
    assert result["status"] == "rejected"
    assert result["rejection_reason"] == "relay process exited at startup"
    assert result["relay_last_failure_at"] is not None
    notes = db_conn.execute(
        "SELECT content FROM inbound_messages WHERE agent_id=%s AND kind='system_note'",
        (owner.agent_id,),
    ).fetchall()
    assert any("rolled back" in row[0] for row in notes)


def test_fail_acceptance_requires_an_accepted_lease(db_conn: psycopg.Connection) -> None:
    owner = _agent(db_conn)
    lease = _request(owner)
    with pytest.raises(leases.ImpersonationError):
        leases.fail_acceptance(lease["id"], owner, "too early")
    leases.accept(lease["id"], owner.agent_id, owner, "Handoff brief")
    leases.activate(lease["id"], owner)
    with pytest.raises(leases.ImpersonationError):
        leases.fail_acceptance(lease["id"], owner, "too late")


def test_record_relay_failure_is_rate_limited(db_conn: psycopg.Connection) -> None:
    owner = _agent(db_conn)
    lease = _active(owner)
    assert leases.record_relay_failure(lease["id"], owner) is True
    assert leases.record_relay_failure(lease["id"], owner) is False


def test_relay_liveness_alert_logs_only_for_stale_active_leases(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _agent(db_conn)
    _active(owner)
    errors: list[tuple[str, object]] = []

    class FakeLogger:
        def error(self, message: str, **fields: object) -> None:
            errors.append((message, fields))

        def exception(self, message: str, **fields: object) -> None:
            errors.append((message, fields))

    monkeypatch.setattr(leases, "logger", FakeLogger())
    leases.relay_liveness_alert(owner.agent_id)
    assert len(errors) == 1
    assert "relay heartbeat is stale" in errors[0][0]
    # A fresh heartbeat suppresses the alert.
    db_conn.execute(
        "UPDATE agent_impersonations SET relay_heartbeat_at=clock_timestamp() WHERE agent_id=%s",
        (owner.agent_id,),
    )
    db_conn.commit()
    errors.clear()
    leases.relay_liveness_alert(owner.agent_id)
    assert errors == []
    leases.relay_liveness_alert(owner.agent_id + 1)
    assert errors == []


def test_reminder_is_inserted_once_per_lease_and_wakes(
    db_conn: psycopg.Connection,
) -> None:
    from shared.db import pool
    from shared.impersonation_maintenance import remind_expiring_impersonations

    owner = _agent(db_conn)
    lease = _active(owner)
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()+interval '4 minutes' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    with pool(max_size=2) as reaper_pool:
        assert remind_expiring_impersonations(reaper_pool) == 1
        # Idempotent: a second scan in the same window inserts nothing new.
        assert remind_expiring_impersonations(reaper_pool) == 0
    reminder = db_conn.execute(
        "SELECT content,kind,source,status,payload FROM inbound_messages "
        "WHERE agent_id=%s AND kind='reminder'",
        (owner.agent_id,),
    ).fetchone()
    assert reminder is not None
    assert reminder[1:4] == ("reminder", "system", "pending")
    assert reminder[4]["lease_id"] == str(lease["id"])
    assert str(lease["id"]) in reminder[0]
    assert "renew" in reminder[0] and "release" in reminder[0]


def test_reminder_skips_leases_outside_the_window(db_conn: psycopg.Connection) -> None:
    from shared.db import pool
    from shared.impersonation_maintenance import remind_expiring_impersonations

    owner = _agent(db_conn)
    _active(owner)
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()+interval '30 minutes' "
        "WHERE agent_id=%s",
        (owner.agent_id,),
    )
    db_conn.commit()
    with pool(max_size=2) as reaper_pool:
        assert remind_expiring_impersonations(reaper_pool) == 0
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='reminder'",
        (owner.agent_id,),
    ).fetchone() == (0,)


def test_release_dismisses_its_pending_reminder(db_conn: psycopg.Connection) -> None:
    from shared.db import pool
    from shared.impersonation_maintenance import remind_expiring_impersonations

    owner = _agent(db_conn)
    lease = _active(owner)
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()+interval '2 minutes' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    with pool(max_size=2) as reaper_pool:
        assert remind_expiring_impersonations(reaper_pool) == 1
    leases.release(lease["id"], lease["token"], "Done before expiry")
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE agent_id=%s AND kind='reminder'",
        (owner.agent_id,),
    ).fetchone() == ("done",)


def test_expiry_dismisses_its_pending_reminder(db_conn: psycopg.Connection) -> None:
    from shared.db import pool
    from shared.impersonation_maintenance import reap_impersonations, remind_expiring_impersonations

    owner = _agent(db_conn)
    lease = _active(owner)
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()+interval '1 minute' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    with pool(max_size=2) as reaper_pool:
        assert remind_expiring_impersonations(reaper_pool) == 1
        db_conn.execute(
            "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
            "WHERE id=%s",
            (lease["id"],),
        )
        db_conn.commit()
        assert reap_impersonations(reaper_pool) == 1
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE agent_id=%s AND kind='reminder'",
        (owner.agent_id,),
    ).fetchone() == ("done",)
