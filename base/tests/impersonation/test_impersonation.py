"""Database leases, explicit consent, and durable handoff/inbox invariants."""

from __future__ import annotations

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import cast
from uuid import uuid4

import psycopg
import pytest

from base.agents import impersonation as leases
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database, insert_inbound_message
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.projection import Cancelled
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.tests.impersonation.support import (
    accept_controller,
    create_native_owner,
    native_status,
    request_controller,
)
from tests.fixtures.unit.identity import machine_identity as machine_identity
from tests.impersonation_support import attested_caller


def test_controller_read_that_expires_lease_refreshes_roster(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    owner = create_native_owner(db_conn, local_machine=local_machine)
    lease = accept_controller(
        owner, authority=config_authority, database_gate=database_gate, local_machine=local_machine
    )
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
        leases.get(
            database, event_bus, lease["id"], attested_caller(lease), local_machine=local_machine
        )["status"]
        == "expired"
    )
    assert notices == [(owner.agent_id, True)]
    assert (
        leases.get(
            database, event_bus, lease["id"], attested_caller(lease), local_machine=local_machine
        )["status"]
        == "expired"
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
    db_conn: psycopg.Connection,
    short_lock_timeout: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    owner = create_native_owner(db_conn, local_machine=local_machine)
    db_conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (owner.agent_id,))
    assert leases.native_status(short_lock_timeout, event_bus, owner.agent_id, owner) is None


def test_external_identity_read_does_not_contend_with_lease_writes(
    redis_url: str,
    db_conn: psycopg.Connection,
    short_lock_timeout: Database,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    owner = create_native_owner(db_conn, local_machine=local_machine)
    lease = accept_controller(
        owner, authority=config_authority, database_gate=database_gate, local_machine=local_machine
    )
    db_conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (owner.agent_id,))
    db_conn.execute("SELECT id FROM agent_impersonations WHERE id=%s FOR UPDATE", (lease["id"],))
    result = leases.require_active(
        short_lock_timeout, lease["id"], attested_caller(lease), local_machine=local_machine
    )
    assert result["status"] == "active"
    assert set(result) == set(lease)


def test_existing_lease_still_serializes_native_reconciliation(
    db_conn: psycopg.Connection,
    short_lock_timeout: Database,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    owner = create_native_owner(db_conn, local_machine=local_machine)
    lease = request_controller(
        owner, authority=config_authority, database_gate=database_gate, local_machine=local_machine
    )
    leases.accept(
        database,
        event_bus,
        lease["id"],
        owner.agent_id,
        owner,
        "Handoff brief",
        local_machine=local_machine,
    )
    db_conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (owner.agent_id,))
    with pytest.raises(psycopg.errors.LockNotAvailable):
        leases.native_status(short_lock_timeout, event_bus, owner.agent_id, owner)
    db_conn.rollback()
    assert native_status(owner, database_gate=database_gate)["status"] == "accepted"


@pytest.mark.parametrize("invalid", ["owner", "ttl", "status"])
def test_native_without_lease_still_requires_current_ownership(
    db_conn: psycopg.Connection,
    invalid: str,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    owner = create_native_owner(db_conn, local_machine=local_machine)
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
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    owner = create_native_owner(db_conn, local_machine=local_machine)
    lease = request_controller(
        owner, authority=config_authority, database_gate=database_gate, local_machine=local_machine
    )
    for empty in ("", "   "):
        with pytest.raises(leases.ImpersonationError, match="start message is required"):
            leases.accept(
                database,
                event_bus,
                lease["id"],
                owner.agent_id,
                owner,
                empty,
                local_machine=local_machine,
            )
    assert native_status(owner, database_gate=database_gate)["status"] == "requested"
    leases.accept(
        database,
        event_bus,
        lease["id"],
        owner.agent_id,
        owner,
        "Do the implementation, then summarize.",
        local_machine=local_machine,
    )
    state = native_status(owner, database_gate=database_gate)
    assert state["status"] == "accepted"
    assert state["start_message"] == "Do the implementation, then summarize."


def test_request_needs_consent_then_native_checkpoint_ack(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    owner = create_native_owner(db_conn, local_machine=local_machine)
    lease = request_controller(
        owner, authority=config_authority, database_gate=database_gate, local_machine=local_machine
    )
    assert lease["status"] == "requested"
    assert "token_hash" not in lease
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)
    db_conn.commit()
    assert native_status(owner, database_gate=database_gate)["reason"] == "Handle the next message"
    with pytest.raises(leases.ImpersonationError, match="not active"):
        leases.require_active(
            database, lease["id"], attested_caller(lease), local_machine=local_machine
        )
    leases.accept(
        database,
        event_bus,
        lease["id"],
        owner.agent_id,
        owner,
        "Handoff brief",
        local_machine=local_machine,
    )
    assert native_status(owner, database_gate=database_gate)["status"] == "accepted"
    with pytest.raises(leases.ImpersonationError, match="not active"):
        leases.inbox(database, lease["id"], attested_caller(lease), local_machine=local_machine)
    leases.activate(database, event_bus, lease["id"], owner)
    assert (
        leases.require_active(
            database, lease["id"], attested_caller(lease), local_machine=local_machine
        )["status"]
        == "active"
    )


def test_consent_and_activation_cannot_use_another_incarnation(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    owner = create_native_owner(db_conn, local_machine=local_machine)
    lease = request_controller(
        owner, authority=config_authority, database_gate=database_gate, local_machine=local_machine
    )
    stale = RuntimeIncarnation(owner.agent_id, uuid4(), uuid4())
    with pytest.raises(leases.ImpersonationError, match="no longer owns"):
        leases.accept(
            database,
            event_bus,
            lease["id"],
            owner.agent_id,
            stale,
            "Handoff brief",
            local_machine=local_machine,
        )
    leases.accept(
        database,
        event_bus,
        lease["id"],
        owner.agent_id,
        owner,
        "Handoff brief",
        local_machine=local_machine,
    )
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
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    owner = create_native_owner(db_conn, local_machine=local_machine)
    lease = accept_controller(
        owner, authority=config_authority, database_gate=database_gate, local_machine=local_machine
    )
    first = insert_inbound_message(
        db_conn, owner.agent_id, "first", "agent:99", bus=event_bus, database=database
    )
    second = insert_inbound_message(
        db_conn, owner.agent_id, "second", "agent:99", bus=event_bus, database=database
    )
    with pytest.raises(leases.ImpersonationError, match="not read"):
        leases.ack(
            database,
            event_bus,
            lease["id"],
            attested_caller(lease),
            [first],
            local_machine=local_machine,
        )
    assert [
        m["id"]
        for m in leases.inbox(
            database, lease["id"], attested_caller(lease), local_machine=local_machine
        )
    ] == [
        first,
        second,
    ]
    leases.ack(
        database,
        event_bus,
        lease["id"],
        attested_caller(lease),
        [first],
        local_machine=local_machine,
    )
    assert [
        m["id"]
        for m in leases.inbox(
            database, lease["id"], attested_caller(lease), local_machine=local_machine
        )
    ] == [second]
    result = leases.release(
        database,
        event_bus,
        lease["id"],
        attested_caller(lease),
        "Finished first; second still needs work.",
        local_machine=local_machine,
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
        leases.release(
            database,
            event_bus,
            lease["id"],
            attested_caller(lease),
            "Retry",
            local_machine=local_machine,
        )
        == result
    )
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (3,)


def test_external_inbox_preserves_cancel_until_explicit_processing_ack(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    owner = create_native_owner(db_conn, local_machine=local_machine)
    lease = accept_controller(
        owner, authority=config_authority, database_gate=database_gate, local_machine=local_machine
    )
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
        (m["id"], m["kind"])
        for m in leases.inbox(
            database, lease["id"], attested_caller(lease), local_machine=local_machine
        )
    ] == [(cancel, "cancel")]
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (cancel,)
    ).fetchone() == ("pending",)
    db_conn.commit()
    leases.ack(
        database,
        event_bus,
        lease["id"],
        attested_caller(lease),
        [cancel],
        local_machine=local_machine,
    )
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
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    owner = create_native_owner(db_conn, local_machine=local_machine)
    lease = accept_controller(
        owner, authority=config_authority, database_gate=database_gate, local_machine=local_machine
    )
    cancel = insert_inbound_message(
        db_conn, owner.agent_id, "Stop", "user", kind="cancel", bus=event_bus, database=database
    )
    peer = insert_inbound_message(
        db_conn, owner.agent_id, "Peer work", "agent:99", bus=event_bus, database=database
    )
    leases.inbox(database, lease["id"], attested_caller(lease), local_machine=local_machine)
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
        leases.ack(
            database,
            event_bus,
            lease["id"],
            attested_caller(lease),
            [cancel, unread],
            local_machine=local_machine,
        )
    assert published == []
    assert wakes == []
    leases.ack(
        database,
        event_bus,
        lease["id"],
        attested_caller(lease),
        [peer],
        local_machine=local_machine,
    )
    assert published == []
    assert wakes == [owner.agent_id]
    leases.ack(
        database,
        event_bus,
        lease["id"],
        attested_caller(lease),
        [cancel],
        local_machine=local_machine,
    )
    assert published == [Cancelled(agent_id=owner.agent_id)]
    assert wakes == [owner.agent_id, owner.agent_id]
    leases.ack(
        database,
        event_bus,
        lease["id"],
        attested_caller(lease),
        [cancel, peer],
        local_machine=local_machine,
    )
    assert len(published) == 1
    assert wakes == [owner.agent_id, owner.agent_id]


def test_ttl_revokes_all_borrower_operations_and_preserves_pending(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    owner = create_native_owner(db_conn, local_machine=local_machine)
    lease = accept_controller(
        owner, authority=config_authority, database_gate=database_gate, local_machine=local_machine
    )
    pending = insert_inbound_message(
        db_conn, owner.agent_id, "durable", "agent:99", bus=event_bus, database=database
    )
    leases.inbox(database, lease["id"], attested_caller(lease), local_machine=local_machine)
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    operations = [
        lambda: leases.require_active(
            database, lease["id"], attested_caller(lease), local_machine=local_machine
        ),
        lambda: leases.renew(
            database, event_bus, lease["id"], attested_caller(lease), local_machine=local_machine
        ),
        lambda: leases.inbox(
            database, lease["id"], attested_caller(lease), local_machine=local_machine
        ),
        lambda: leases.ack(
            database,
            event_bus,
            lease["id"],
            attested_caller(lease),
            [pending],
            local_machine=local_machine,
        ),
        lambda: leases.release(
            database,
            event_bus,
            lease["id"],
            attested_caller(lease),
            "Late summary",
            local_machine=local_machine,
        ),
        lambda: leases.merge_plugin_delta(
            database,
            lease["id"],
            attested_caller(lease),
            {},
            expected_version=0,
            local_machine=local_machine,
        ),
    ]
    for operation in operations:
        with pytest.raises(leases.ImpersonationError, match="stale-session"):
            operation()
    assert native_status(owner, database_gate=database_gate)["status"] == "expired"
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
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    owner = create_native_owner(db_conn, local_machine=local_machine)
    lease = accept_controller(
        owner, authority=config_authority, database_gate=database_gate, local_machine=local_machine
    )
    leases.merge_plugin_delta(
        database,
        lease["id"],
        attested_caller(lease),
        {"encoded": "first"},
        expected_version=0,
        local_machine=local_machine,
    )
    with pytest.raises(leases.ImpersonationError, match="Concurrent"):
        leases.merge_plugin_delta(
            database,
            lease["id"],
            attested_caller(lease),
            {"encoded": "lost"},
            expected_version=0,
            local_machine=local_machine,
        )
    with pytest.raises(leases.ImpersonationError, match="returned native"):
        leases.mark_plugin_applied(database, lease["id"], 1, owner)
    leases.release(
        database,
        event_bus,
        lease["id"],
        attested_caller(lease),
        "State updated",
        local_machine=local_machine,
    )
    with pytest.raises(leases.ImpersonationError, match="unapplied state"):
        request_controller(
            owner,
            authority=config_authority,
            database_gate=database_gate,
            local_machine=local_machine,
        )
    state = native_status(owner, database_gate=database_gate)
    assert state["plugin_delta"] == [{"encoded": "first"}]
    leases.mark_plugin_applied(database, lease["id"], 1, owner)
    assert (
        request_controller(
            owner,
            authority=config_authority,
            database_gate=database_gate,
            local_machine=local_machine,
        )["status"]
        == "requested"
    )


def test_competing_requests_have_one_winner(
    redis_url: str,
    db_conn: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    owner = create_native_owner(db_conn, local_machine=local_machine)

    def attempt(_index: int) -> str:
        try:
            return request_controller(
                owner,
                authority=config_authority,
                database_gate=database_gate,
                local_machine=local_machine,
            )["status"]
        except leases.ImpersonationError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, range(2)))
    assert sorted(results) == ["conflict", "requested"]


def test_attestation_and_same_machine_checks(
    redis_url: str,
    db_conn: psycopg.Connection,
    database: Database,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    owner = create_native_owner(db_conn, local_machine=local_machine)
    lease = accept_controller(
        owner, authority=config_authority, database_gate=database_gate, local_machine=local_machine
    )
    with pytest.raises(leases.ImpersonationError, match="caller check failed"):
        leases.require_active(database, lease["id"], "incorrect", local_machine=local_machine)
    db_conn.execute("UPDATE agents_meta SET machine='another-host' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    with pytest.raises(leases.ImpersonationError, match="placement"):
        leases.require_active(
            database, lease["id"], attested_caller(lease), local_machine=local_machine
        )
    with pytest.raises(leases.ImpersonationError, match="own machine"):
        request_controller(
            owner,
            authority=config_authority,
            database_gate=database_gate,
            local_machine=local_machine,
        )
