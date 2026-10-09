"""Database leases, explicit consent, and durable handoff/inbox invariants."""

from __future__ import annotations

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest

from base.agents import impersonation as leases
from base.agents.messages.caller_identity import CallerIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database, insert_inbound_message
from base.events.live.bus import EventBus
from base.events.live.projection import Cancelled
from base.events.live.tests.fakes import patch_announcements
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.tests.impersonation._impersonation_helpers import _active, _agent, _request, _status
from tests.impersonation_support import attested_caller, recorded_tree


def test_impersonation_wake_reconciles_roster_only_for_status_changes(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    timeline: list[int] = []
    roster: list[int] = []

    def no_wake(_db: Database, _bus: EventBus, _agent_id: int, _reason: str) -> None:
        pass

    monkeypatch.setattr(leases, "publish_inbound_wake", no_wake)
    patch_announcements(monkeypatch, leases, changed=timeline, updated=roster)
    leases.wake_agent(database, event_bus, 7)
    leases.wake_agent(database, event_bus, 7, roster_changed=True)
    assert timeline == [7, 7]
    assert roster == [7]


def test_controller_read_that_expires_lease_refreshes_roster(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    owner = _agent(db_conn)
    lease = _active(owner, authority=config_authority)
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    notices: list[tuple[int, bool]] = []

    def record_wake(
        _db: Database, _bus: EventBus, agent_id: int, *, roster_changed: bool = False
    ) -> None:
        notices.append((agent_id, roster_changed))

    monkeypatch.setattr(leases, "wake_agent", record_wake)
    assert (
        leases.get(database, event_bus, lease["id"], attested_caller(lease))["status"] == "expired"
    )
    assert notices == [(owner.agent_id, True)]
    assert (
        leases.get(database, event_bus, lease["id"], attested_caller(lease))["status"] == "expired"
    )
    assert notices == [(owner.agent_id, True)]


class _ShortLockDatabase:
    """The `Database` the lease code dials, with a 100 ms lock timeout on every transaction."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @contextmanager
    def write_transaction(self) -> Generator[psycopg.Connection]:
        with self._database.write_transaction() as conn:
            conn.execute("SET LOCAL lock_timeout='100ms'")
            yield conn

    @contextmanager
    def connect(self) -> Generator[psycopg.Connection]:
        with self._database.connect() as conn:
            conn.execute("SET LOCAL lock_timeout='100ms'")
            yield conn


@pytest.fixture
def short_lock_timeout(database: Database) -> Database:
    return cast(Database, _ShortLockDatabase(database))


def test_native_without_lease_does_not_contend_with_agent_writes(
    db_conn: psycopg.Connection, short_lock_timeout: Database, event_bus: EventBus
) -> None:
    owner = _agent(db_conn)
    db_conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (owner.agent_id,))
    assert leases.native_status(short_lock_timeout, event_bus, owner.agent_id, owner) is None


def test_external_identity_read_does_not_contend_with_lease_writes(
    db_conn: psycopg.Connection, short_lock_timeout: Database, *, config_authority: ConfigAuthority
) -> None:
    owner = _agent(db_conn)
    lease = _active(owner, authority=config_authority)
    db_conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (owner.agent_id,))
    db_conn.execute("SELECT id FROM agent_impersonations WHERE id=%s FOR UPDATE", (lease["id"],))
    result = leases.require_active(short_lock_timeout, lease["id"], attested_caller(lease))
    assert result["status"] == "active"
    assert set(result) == set(lease)


def test_existing_lease_still_serializes_native_reconciliation(
    db_conn: psycopg.Connection,
    short_lock_timeout: Database,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner, authority=config_authority)
    leases.accept(database, event_bus, lease["id"], owner.agent_id, owner, "Handoff brief")
    db_conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (owner.agent_id,))
    with pytest.raises(psycopg.errors.LockNotAvailable):
        leases.native_status(short_lock_timeout, event_bus, owner.agent_id, owner)
    db_conn.rollback()
    assert _status(owner)["status"] == "accepted"


@pytest.mark.parametrize("invalid", ["owner", "ttl", "status"])
def test_native_without_lease_still_requires_current_ownership(
    db_conn: psycopg.Connection, invalid: str, database: Database, event_bus: EventBus
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
        db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    with pytest.raises(leases.ImpersonationError, match="no longer owns"):
        leases.native_status(database, event_bus, owner.agent_id, owner)


def test_accept_requires_a_nonempty_start_message(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner, authority=config_authority)
    for empty in ("", "   "):
        with pytest.raises(leases.ImpersonationError, match="start message is required"):
            leases.accept(database, event_bus, lease["id"], owner.agent_id, owner, empty)
    assert _status(owner)["status"] == "requested"
    leases.accept(
        database,
        event_bus,
        lease["id"],
        owner.agent_id,
        owner,
        "Do the implementation, then summarize.",
    )
    state = _status(owner)
    assert state["status"] == "accepted"
    assert state["start_message"] == "Do the implementation, then summarize."


def test_request_needs_consent_then_native_checkpoint_ack(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner, authority=config_authority)
    assert lease["status"] == "requested"
    assert "token_hash" not in lease
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)
    db_conn.commit()
    assert _status(owner)["reason"] == "Handle the next message"
    with pytest.raises(leases.ImpersonationError, match="not active"):
        leases.require_active(database, lease["id"], attested_caller(lease))
    leases.accept(database, event_bus, lease["id"], owner.agent_id, owner, "Handoff brief")
    assert _status(owner)["status"] == "accepted"
    with pytest.raises(leases.ImpersonationError, match="not active"):
        leases.inbox(database, lease["id"], attested_caller(lease))
    leases.activate(database, event_bus, lease["id"], owner)
    assert (
        leases.require_active(database, lease["id"], attested_caller(lease))["status"] == "active"
    )


def test_consent_and_activation_cannot_use_another_incarnation(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner, authority=config_authority)
    stale = RuntimeIncarnation(owner.agent_id, uuid4(), uuid4())
    with pytest.raises(leases.ImpersonationError, match="no longer owns"):
        leases.accept(database, event_bus, lease["id"], owner.agent_id, stale, "Handoff brief")
    leases.accept(database, event_bus, lease["id"], owner.agent_id, owner, "Handoff brief")
    db_conn.execute(
        "UPDATE agents_meta SET runtime_generation=%s,runtime_owner=%s WHERE id=%s",
        (stale.generation, stale.owner, owner.agent_id),
    )
    db_conn.commit()
    with pytest.raises(leases.ImpersonationError, match="another native incarnation"):
        leases.activate(database, event_bus, lease["id"], stale)


def test_inbox_ack_and_atomic_real_sender_handoff(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    owner = _agent(db_conn)
    lease = _active(owner, authority=config_authority)
    first = insert_inbound_message(
        db_conn, owner.agent_id, "first", "agent:99", bus=event_bus, database=database
    )
    second = insert_inbound_message(
        db_conn, owner.agent_id, "second", "agent:99", bus=event_bus, database=database
    )
    with pytest.raises(leases.ImpersonationError, match="not read"):
        leases.ack(database, event_bus, lease["id"], attested_caller(lease), [first])
    assert [m["id"] for m in leases.inbox(database, lease["id"], attested_caller(lease))] == [
        first,
        second,
    ]
    leases.ack(database, event_bus, lease["id"], attested_caller(lease), [first])
    assert [m["id"] for m in leases.inbox(database, lease["id"], attested_caller(lease))] == [
        second
    ]
    result = leases.release(
        database,
        event_bus,
        lease["id"],
        attested_caller(lease),
        "Finished first; second still needs work.",
    )
    assert result["status"] == "released"
    assert leases.native_status(database, event_bus, owner.agent_id, owner) is None
    rows = db_conn.execute(
        "SELECT id,status,source,payload FROM inbound_messages ORDER BY id"
    ).fetchall()
    assert rows[0][:3] == (first, "done", "agent:99")
    assert rows[1][:3] == (second, "pending", "agent:99")
    assert rows[2][1:3] == ("pending", "external_agent:codex:test")
    assert rows[2][3]["caller_identity"]["subject"] == "codex"
    db_conn.commit()
    assert (
        leases.release(database, event_bus, lease["id"], attested_caller(lease), "Retry") == result
    )
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (3,)


def test_external_inbox_preserves_cancel_until_explicit_processing_ack(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    owner = _agent(db_conn)
    lease = _active(owner, authority=config_authority)
    cancel = insert_inbound_message(
        db_conn,
        owner.agent_id,
        "Stop current work",
        "user",
        kind="cancel",
        bus=event_bus,
        database=database,
    )
    assert [
        (m["id"], m["kind"]) for m in leases.inbox(database, lease["id"], attested_caller(lease))
    ] == [(cancel, "cancel")]
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (cancel,)
    ).fetchone() == ("pending",)
    db_conn.commit()
    leases.ack(database, event_bus, lease["id"], attested_caller(lease), [cancel])
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (cancel,)
    ).fetchone() == ("done",)


def test_cancel_ack_publishes_committed_completion_once(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    owner = _agent(db_conn)
    lease = _active(owner, authority=config_authority)
    cancel = insert_inbound_message(
        db_conn, owner.agent_id, "Stop", "user", kind="cancel", bus=event_bus, database=database
    )
    peer = insert_inbound_message(
        db_conn, owner.agent_id, "Peer work", "agent:99", bus=event_bus, database=database
    )
    leases.inbox(database, lease["id"], attested_caller(lease))
    unread = insert_inbound_message(
        db_conn, owner.agent_id, "Unread", "user", kind="cancel", bus=event_bus, database=database
    )
    published: list[Cancelled] = []
    wakes: list[int] = []

    def publish(_bus: object, payload: str, *, context: str = "") -> int:
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

    monkeypatch.setattr(EventBus, "publish_best_effort_sync", publish)

    def record_wake(
        _db: Database, _bus: EventBus, agent_id: int, *, roster_changed: bool = False
    ) -> None:
        wakes.append(agent_id)

    monkeypatch.setattr(leases, "wake_agent", record_wake)
    with pytest.raises(leases.ImpersonationError, match="not read"):
        leases.ack(database, event_bus, lease["id"], attested_caller(lease), [cancel, unread])
    assert published == []
    assert wakes == []
    leases.ack(database, event_bus, lease["id"], attested_caller(lease), [peer])
    assert published == []
    assert wakes == [owner.agent_id]
    leases.ack(database, event_bus, lease["id"], attested_caller(lease), [cancel])
    assert published == [Cancelled(agent_id=owner.agent_id)]
    assert wakes == [owner.agent_id, owner.agent_id]
    leases.ack(database, event_bus, lease["id"], attested_caller(lease), [cancel, peer])
    assert len(published) == 1
    assert wakes == [owner.agent_id, owner.agent_id]


def test_ttl_revokes_all_borrower_operations_and_preserves_pending(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    owner = _agent(db_conn)
    lease = _active(owner, authority=config_authority)
    pending = insert_inbound_message(
        db_conn, owner.agent_id, "durable", "agent:99", bus=event_bus, database=database
    )
    leases.inbox(database, lease["id"], attested_caller(lease))
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    operations = [
        lambda: leases.require_active(database, lease["id"], attested_caller(lease)),
        lambda: leases.renew(database, event_bus, lease["id"], attested_caller(lease)),
        lambda: leases.inbox(database, lease["id"], attested_caller(lease)),
        lambda: leases.ack(database, event_bus, lease["id"], attested_caller(lease), [pending]),
        lambda: leases.release(
            database, event_bus, lease["id"], attested_caller(lease), "Late summary"
        ),
        lambda: leases.merge_plugin_delta(
            database, lease["id"], attested_caller(lease), {}, expected_version=0
        ),
    ]
    for operation in operations:
        with pytest.raises(leases.ImpersonationError, match="stale-session"):
            operation()
    assert _status(owner)["status"] == "expired"
    assert leases.native_status(database, event_bus, owner.agent_id, owner) is None
    rows = db_conn.execute("SELECT id,status,source FROM inbound_messages ORDER BY id").fetchall()
    assert rows[0] == (pending, "pending", "agent:99")
    assert rows[1][1:] == ("pending", "system:impersonation")


def test_plugin_journal_blocks_new_lease_until_checkpoint_receipt(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    owner = _agent(db_conn)
    lease = _active(owner, authority=config_authority)
    leases.merge_plugin_delta(
        database, lease["id"], attested_caller(lease), {"encoded": "first"}, expected_version=0
    )
    with pytest.raises(leases.ImpersonationError, match="Concurrent"):
        leases.merge_plugin_delta(
            database, lease["id"], attested_caller(lease), {"encoded": "lost"}, expected_version=0
        )
    with pytest.raises(leases.ImpersonationError, match="returned native"):
        leases.mark_plugin_applied(database, lease["id"], 1, owner)
    leases.release(database, event_bus, lease["id"], attested_caller(lease), "State updated")
    with pytest.raises(leases.ImpersonationError, match="unapplied state"):
        _request(owner, authority=config_authority)
    state = _status(owner)
    assert state["plugin_delta"] == [{"encoded": "first"}]
    leases.mark_plugin_applied(database, lease["id"], 1, owner)
    assert _request(owner, authority=config_authority)["status"] == "requested"


def test_competing_requests_have_one_winner(
    db_conn: psycopg.Connection, *, config_authority: ConfigAuthority
) -> None:
    owner = _agent(db_conn)

    def attempt(_index: int) -> str:
        try:
            return _request(owner, authority=config_authority)["status"]
        except leases.ImpersonationError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, range(2)))
    assert sorted(results) == ["conflict", "requested"]


def test_attestation_and_same_machine_checks(
    db_conn: psycopg.Connection, database: Database, *, config_authority: ConfigAuthority
) -> None:
    owner = _agent(db_conn)
    lease = _active(owner, authority=config_authority)
    with pytest.raises(leases.ImpersonationError, match="caller check failed"):
        leases.require_active(database, lease["id"], "incorrect")
    db_conn.execute("UPDATE agents_meta SET machine='another-host' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    with pytest.raises(leases.ImpersonationError, match="placement"):
        leases.require_active(database, lease["id"], attested_caller(lease))
    with pytest.raises(leases.ImpersonationError, match="own machine"):
        _request(owner, authority=config_authority)


def test_termination_atomically_revokes_but_restart_preserves(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    owner = _agent(db_conn)
    lease = _active(owner, authority=config_authority)
    db_conn.execute("UPDATE agents_meta SET status='idling' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    assert (
        leases.get(database, event_bus, lease["id"], attested_caller(lease))["status"] == "active"
    )
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    assert (
        leases.get(database, event_bus, lease["id"], attested_caller(lease))["status"] == "expired"
    )
    db_conn.execute("UPDATE agents_meta SET status='idling' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    with pytest.raises(leases.ImpersonationError, match="not active"):
        leases.require_active(database, lease["id"], attested_caller(lease))


def test_replacement_requires_fresh_consent_before_checkpoint_ack(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner, authority=config_authority)
    leases.accept(database, event_bus, lease["id"], owner.agent_id, owner, "Handoff brief")
    replacement = RuntimeIncarnation(owner.agent_id, uuid4(), uuid4())
    db_conn.execute(
        "UPDATE agents_meta SET runtime_generation=%s,runtime_owner=%s WHERE id=%s",
        (replacement.generation, replacement.owner, owner.agent_id),
    )
    db_conn.commit()
    state = _status(replacement)
    assert state["status"] == "requested"
    assert state["consent_version"] == 2
    leases.accept(database, event_bus, lease["id"], owner.agent_id, replacement, "Handoff brief")
    assert leases.activate(database, event_bus, lease["id"], replacement)["status"] == "active"


def test_expiry_between_driver_read_and_activation_returns_control(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner, authority=config_authority)
    leases.accept(database, event_bus, lease["id"], owner.agent_id, owner, "Handoff brief")
    assert _status(owner)["status"] == "accepted"
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    assert leases.activate(database, event_bus, lease["id"], owner)["status"] == "expired"
    assert leases.native_status(database, event_bus, owner.agent_id, owner) is None


def test_renew_replaces_ttl_and_reject_records_reason(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner, authority=config_authority)
    result = leases.reject(
        database,
        event_bus,
        lease["id"],
        owner.agent_id,
        owner,
        "Finish the critical operation first",
    )
    assert result["rejection_reason"] == "Finish the critical operation first"
    lease = _active(owner, authority=config_authority)
    renewed = leases.renew(
        database, event_bus, lease["id"], attested_caller(lease), ttl_seconds=600
    )
    assert renewed["ttl_seconds"] == 600
    assert renewed["expires_at"] > lease["expires_at"]


def test_reaper_expires_offline_lease_and_keeps_unconsumed_handoff(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    from base.agents.impersonation import maintenance as maintenance
    from base.db import pool

    announced: list[int] = []
    roster_announced: list[int] = []
    patch_announcements(monkeypatch, maintenance, changed=announced, updated=roster_announced)

    owner = _agent(db_conn)
    lease = _active(owner, authority=config_authority)
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    with pool(max_size=2) as reaper_pool:
        assert maintenance.reap_impersonations(reaper_pool, database, event_bus) == 1
        assert announced == [owner.agent_id]
        assert roster_announced == [owner.agent_id]
        db_conn.execute(
            "UPDATE agent_impersonations SET ended_at=clock_timestamp()-interval '8 days' WHERE id=%s",
            (lease["id"],),
        )
        db_conn.commit()
        assert maintenance.reap_impersonations(reaper_pool, database, event_bus) == 0
        assert (
            leases.get(database, event_bus, lease["id"], attested_caller(lease))["status"]
            == "expired"
        )
        db_conn.execute(
            "UPDATE inbound_messages SET status='done' WHERE agent_id=%s", (owner.agent_id,)
        )
        db_conn.commit()
        maintenance.reap_impersonations(reaper_pool, database, event_bus)
    assert db_conn.execute("SELECT count(*) FROM agent_impersonations").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)


def _assert_force_expired_row(
    db_conn: psycopg.Connection,
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    lease_status: str,
) -> None:
    row = db_conn.execute(
        "SELECT status,ended_at,rejection_reason,summary_inbound_id "
        "FROM agent_impersonations WHERE id=%s",
        (lease["id"],),
    ).fetchone()
    assert row is not None
    status, ended_at, reason, inbound_id = row
    assert (status, reason) == ("expired", "force-expired: ended by an operator")
    assert ended_at is not None
    assert (inbound_id is not None) == (lease_status == "active")
    if inbound_id is not None:
        note = db_conn.execute(
            "SELECT content,kind,source FROM inbound_messages WHERE id=%s", (inbound_id,)
        ).fetchone()
        assert note is not None
        content, kind, source = note
        assert (kind, source) == ("chat", "system:impersonation")
        assert "Control has returned" in content
        assert "handoff" not in content.lower()
        assert "read the" not in content.lower()
        assert db_conn.execute(
            "SELECT status FROM inbound_messages WHERE kind='reminder' AND agent_id=%s",
            (owner.agent_id,),
        ).fetchone() == ("done",)


def _assert_expired_lifecycle_entry(db_conn: psycopg.Connection, lease: dict[str, Any]) -> None:
    entry_row = db_conn.execute(
        "SELECT payload FROM agent_impersonation_entries WHERE lease_id=%s "
        "AND kind='lifecycle' ORDER BY seq DESC LIMIT 1",
        (lease["id"],),
    ).fetchone()
    assert entry_row is not None
    entry = entry_row[0]
    assert entry["status"] == "expired"
    assert entry["source"] == "user_session:administrator"


@pytest.mark.parametrize("lease_status", ["requested", "accepted", "active"])
def test_operator_force_expire_closes_only_observed_session(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    lease_status: str,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    from base.agents.impersonation import maintenance as maintenance
    from base.db import pool

    owner = _agent(db_conn)
    lease = _request(owner, authority=config_authority)
    if lease_status in ("accepted", "active"):
        leases.accept(database, event_bus, lease["id"], owner.agent_id, owner, "Brief")
    if lease_status == "active":
        leases.activate(database, event_bus, lease["id"], owner)
        db_conn.execute(
            "INSERT INTO inbound_messages(agent_id,content,kind,source,payload) "
            "VALUES(%s,'Renew soon','reminder','system',jsonb_build_object('lease_id',%s::text))",
            (owner.agent_id, str(lease["id"])),
        )
        db_conn.commit()
    wakes: list[int] = []
    announcements: list[int] = []
    roster_announcements: list[int] = []

    def record_wake(_db: Database, _bus: EventBus, agent_id: int, _reason: str) -> None:
        wakes.append(agent_id)

    monkeypatch.setattr(maintenance, "publish_inbound_wake", record_wake)
    patch_announcements(
        monkeypatch, maintenance, changed=announcements, updated=roster_announcements
    )
    with pool(max_size=2) as gateway_pool:
        assert (
            maintenance.force_expire_impersonation(
                gateway_pool,
                database,
                event_bus,
                owner.agent_id,
                lease["session_id"] + 1,
                "user_session:administrator",
            )
            == "not_open"
        )
        assert (
            maintenance.force_expire_impersonation(
                gateway_pool,
                database,
                event_bus,
                owner.agent_id,
                lease["session_id"],
                "user_session:administrator",
            )
            == "expired"
        )
        assert (
            maintenance.force_expire_impersonation(
                gateway_pool,
                database,
                event_bus,
                owner.agent_id,
                lease["session_id"],
                "user_session:administrator",
            )
            == "not_open"
        )
    assert wakes == [owner.agent_id]
    assert announcements == [owner.agent_id]
    assert roster_announcements == [owner.agent_id]
    _assert_force_expired_row(db_conn, owner, lease, lease_status)
    _assert_expired_lifecycle_entry(db_conn, lease)


def test_operator_force_expire_automatic_session_has_no_manual_end_note(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    from base.agents.impersonation.maintenance import force_expire_impersonation
    from base.db import pool

    owner = _agent(db_conn)
    lease = leases.request(
        database,
        event_bus,
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
        reason="Handle the next message",
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
        automatic=True,
        name="Automatic takeover",
        executor_name="Codex: test",
        authority=config_authority,
    )
    leases.accept(database, event_bus, lease["id"], owner.agent_id, owner, "Brief")
    leases.activate(database, event_bus, lease["id"], owner)
    with pool(max_size=2) as gateway_pool:
        assert (
            force_expire_impersonation(
                gateway_pool,
                database,
                event_bus,
                owner.agent_id,
                lease["session_id"],
                "local_operator",
            )
            == "expired"
        )
    assert db_conn.execute(
        "SELECT summary_inbound_id FROM agent_impersonations WHERE id=%s", (lease["id"],)
    ).fetchone() == (None,)
