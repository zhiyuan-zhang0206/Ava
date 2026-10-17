"""Database-enforced delivery budget, races, restart and native handoff."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from base.agents import impersonation as leases
from base.agents.impersonation import delivery as delivery
from base.agents.impersonation.tests.impersonation_support import recorded_tree
from base.agents.inbox.insert import insert_inbound_message
from base.api_contracts.caller_identity import CallerIdentity
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.tests.fakes import recording
from base.native_process.runtime_incarnation import RuntimeIncarnation

type ActiveSession = tuple[dict[str, Any], RuntimeIncarnation, int]


@pytest.fixture
def active(
    db_conn: psycopg.Connection,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> ActiveSession:
    from base.host.env import dotenv_file

    window, attempts = getattr(request, "param", (180, 2))
    monkeypatch.setattr(dotenv_file, "unit_home", lambda: tmp_path)
    (tmp_path / ".env").write_text(
        f"MEIVA_IMPERSONATION_ACK_WINDOW_SECONDS={window}\n"
        f"MEIVA_IMPERSONATION_MAX_DELIVERY_ATTEMPTS={attempts}\n"
    )
    agent_id = create_agent(db_conn)
    owner = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '1 hour')",
        (agent_id, machine_identity.name(), owner.generation, owner.owner),
    )
    db_conn.commit()
    lease = leases.request(
        database,
        event_bus,
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
        ttl_seconds=3600,
        reason="Test ACK exhaustion",
        relay_provider="claude",
        process_metadata=recorded_tree(),
        authority=config_authority,
        local_machine=machine_identity.name(),
    )
    assert (lease["ack_window_seconds"], lease["max_delivery_attempts"]) == (window, attempts)
    leases.accept(
        database,
        event_bus,
        lease["id"],
        agent_id,
        owner,
        "Process and ACK the test message",
        local_machine=machine_identity.name(),
    )
    leases.activate(database, event_bus, lease["id"], owner)
    lease = leases.relay_get(
        database,
        event_bus,
        lease["id"],
        lease["relay_token"],
        local_machine=machine_identity.name(),
    ) | {"relay_token": lease["relay_token"]}
    message_id = insert_inbound_message(
        db_conn,
        agent_id,
        "Unprocessed user input",
        "user",
        kind="chat",
        bus=event_bus,
        database=database,
    )
    db_conn.commit()
    leases.relay_inbox(
        database, lease["id"], lease["relay_token"], local_machine=machine_identity.name()
    )
    return lease, owner, message_id


def reserve(
    lease: dict[str, Any],
    message_id: int,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> frozenset[int]:
    return delivery.reserve_delivery(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        lease["id"],
        lease["relay_token"],
        [message_id],
        local_machine=machine_identity.name(),
    )


def elapse(db_conn: psycopg.Connection, lease: dict[str, Any], seconds: int = 181) -> None:
    db_conn.execute(
        "UPDATE agent_impersonation_messages SET last_delivery_at="
        "clock_timestamp() - %s*interval '1 second' WHERE lease_id=%s AND delivery_attempts>0",
        (seconds, lease["id"]),
    )
    db_conn.commit()


def assert_message_pending(db_conn: psycopg.Connection, message_id: int) -> None:
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (message_id,)
    ).fetchone() == ("pending",)


def assert_expiry_note_explains_pending_messages(
    db_conn: psycopg.Connection, summary_inbound_id: int
) -> None:
    note = db_conn.execute(
        "SELECT content FROM inbound_messages WHERE id=%s", (summary_inbound_id,)
    ).fetchone()
    assert note is not None
    assert "did not ACK" in note[0] and "Unacknowledged messages remain pending" in note[0]


def _assert_exhaustion_preserved(
    ended: dict[str, Any],
    lease: dict[str, Any],
    mid: int,
    db_conn: psycopg.Connection,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    assert ended["status"] == "active"
    assert f"message {mid} exhausted" in ended["relay_degraded_reason"]
    assert ended["reason"] == "Test ACK exhaustion"
    assert (
        reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity) == set()
    )
    assert_message_pending(db_conn, mid)
    assert ended["expires_at"] == lease["expires_at"]


def test_two_attempts_keep_lease_active_and_late_receipt_possible(
    db_conn: psycopg.Connection,
    active: ActiveSession,
    database: Database,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    lease, owner, mid = active
    assert (lease["ack_window_seconds"], lease["max_delivery_attempts"]) == (180, 2)
    assert reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity) == {
        mid
    }
    assert (
        reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity) == set()
    )  # no early retry
    elapse(db_conn, lease)
    assert (
        leases.relay_get(
            database,
            event_bus,
            lease["id"],
            lease["relay_token"],
            local_machine=machine_identity.name(),
        )["status"]
        == "active"
    )
    assert reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity) == {
        mid
    }
    elapse(db_conn, lease, 179)
    assert (
        leases.relay_get(
            database,
            event_bus,
            lease["id"],
            lease["relay_token"],
            local_machine=machine_identity.name(),
        )["status"]
        == "active"
    )
    assert (
        reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity) == set()
    )
    elapse(db_conn, lease)
    # The native reconciler also enforces the budget without a live relay.
    ended = leases.native_status(database, event_bus, owner.agent_id, owner)
    assert ended is not None
    _assert_exhaustion_preserved(
        ended, lease, mid, db_conn, database_gate=database_gate, machine_identity=machine_identity
    )
    leases.ack(
        database,
        event_bus,
        lease["id"],
        [mid],
        local_machine=machine_identity.name(),
    )
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (mid,)
    ).fetchone() == ("done",)


def test_delivery_reservation_that_expires_lease_refreshes_roster(
    db_conn: psycopg.Connection,
    active: ActiveSession,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    lease, owner, message_id = active
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    wakes: list[int] = []
    timelines: list[int] = []
    rosters: list[int] = []

    def record_wake(_db: object, _bus: object, agent_id: int, _reason: str) -> None:
        wakes.append(agent_id)

    monkeypatch.setattr(delivery, "publish_inbound_wake", record_wake)
    monkeypatch.setattr(delivery, "publish_impersonation_changed_sync", recording(timelines))
    monkeypatch.setattr(delivery, "publish_agent_updated_sync", recording(rosters))
    assert (
        reserve(lease, message_id, database_gate=database_gate, machine_identity=machine_identity)
        == frozenset()
    )
    assert wakes == timelines == rosters == [owner.agent_id]
    assert (
        reserve(lease, message_id, database_gate=database_gate, machine_identity=machine_identity)
        == frozenset()
    )
    assert wakes == timelines == rosters == [owner.agent_id]


def test_ack_in_final_window_prevents_expiry(
    db_conn: psycopg.Connection,
    active: ActiveSession,
    database: Database,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, mid = active
    reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity)
    elapse(db_conn, lease)
    reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity)
    leases.ack(
        database,
        event_bus,
        lease["id"],
        [mid],
        local_machine=machine_identity.name(),
    )
    elapse(db_conn, lease)
    assert (
        leases.relay_get(
            database,
            event_bus,
            lease["id"],
            lease["relay_token"],
            local_machine=machine_identity.name(),
        )["status"]
        == "active"
    )
    assert (
        reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity) == set()
    )


def test_rotating_relay_does_not_reset_budget(
    db_conn: psycopg.Connection,
    active: ActiveSession,
    database: Database,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    lease, owner, mid = active
    reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity)
    elapse(db_conn, lease)
    reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity)
    leases.provision_relay(database, lease["id"], owner, "replacement")
    with pytest.raises(leases.ImpersonationError, match="Invalid relay token"):
        reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity)
    lease["relay_token"] = "replacement"  # noqa: S105 — test-only scoped credential
    rows = leases.relay_inbox(
        database, lease["id"], "replacement", local_machine=machine_identity.name()
    )
    assert rows[0]["delivery_attempts"] == 2
    assert rows[0]["delivery_due"] is False
    elapse(db_conn, lease)
    assert (
        reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity) == set()
    )
    assert (
        leases.relay_get(
            database, event_bus, lease["id"], "replacement", local_machine=machine_identity.name()
        )["status"]
        == "active"
    )


def test_concurrent_relays_only_reserve_once(
    db_conn: psycopg.Connection,
    active: ActiveSession,
    database: Database,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, mid = active
    with ThreadPoolExecutor(2) as pool:
        futures = [
            pool.submit(
                reserve, lease, mid, database_gate=database_gate, machine_identity=machine_identity
            )
            for _ in range(2)
        ]
    assert sorted(len(f.result()) for f in futures) == [0, 1]
    assert (
        leases.relay_inbox(
            database, lease["id"], lease["relay_token"], local_machine=machine_identity.name()
        )[0]["delivery_attempts"]
        == 1
    )


def test_ack_after_read_before_reservation_cannot_be_pushed(
    db_conn: psycopg.Connection,
    active: ActiveSession,
    database: Database,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, mid = active
    leases.ack(
        database,
        event_bus,
        lease["id"],
        [mid],
        local_machine=machine_identity.name(),
    )
    assert (
        reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity) == set()
    )


def test_release_wins_timeout_race_without_being_overwritten(
    db_conn: psycopg.Connection,
    active: ActiveSession,
    database: Database,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, mid = active
    reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity)
    elapse(db_conn, lease)
    reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity)
    leases.release(
        database,
        event_bus,
        lease["id"],
        "Returning control",
        local_machine=machine_identity.name(),
    )
    elapse(db_conn, lease)
    assert (
        reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity) == set()
    )
    assert (
        leases.relay_get(
            database,
            event_bus,
            lease["id"],
            lease["relay_token"],
            local_machine=machine_identity.name(),
        )["status"]
        == "released"
    )


def test_exhausted_message_outside_page_remains_visible_without_ending_lease(
    db_conn: psycopg.Connection,
    active: ActiveSession,
    database: Database,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, first = active
    other = insert_inbound_message(
        db_conn,
        lease["agent_id"],
        "Second message",
        "user",
        kind="chat",
        bus=event_bus,
        database=database,
    )
    db_conn.commit()
    leases.relay_inbox(
        database, lease["id"], lease["relay_token"], local_machine=machine_identity.name()
    )
    reserve(lease, other, database_gate=database_gate, machine_identity=machine_identity)
    elapse(db_conn, lease)
    reserve(lease, other, database_gate=database_gate, machine_identity=machine_identity)
    rows = leases.relay_inbox(
        database, lease["id"], lease["relay_token"], limit=1, local_machine=machine_identity.name()
    )
    assert [row["id"] for row in rows] == [first]
    # Fresh input and ACK of another message cannot forgive the exhausted one.
    reserve(lease, first, database_gate=database_gate, machine_identity=machine_identity)
    leases.ack(
        database,
        event_bus,
        lease["id"],
        [first],
        local_machine=machine_identity.name(),
    )
    elapse(db_conn, lease)
    ended = leases.relay_get(
        database,
        event_bus,
        lease["id"],
        lease["relay_token"],
        local_machine=machine_identity.name(),
    )
    assert ended["status"] == "active"
    assert f"message {other}" in ended["relay_degraded_reason"]


class ClockListener:
    """Relay listener whose every wait advances the DB clock by one ACK window.

    Each wait first checks the lease is still inside the window (nothing due,
    still active), then ages the delivery past it.
    """

    closed = False
    waits = 0

    def __init__(
        self,
        db_conn: psycopg.Connection,
        lease: dict[str, Any],
        database: Database,
        event_bus: EventBus,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
        self._db_conn = db_conn
        self._lease = lease
        self._database = database
        self._event_bus = event_bus
        self._machine = machine_identity

    async def ensure_listening(self) -> None:
        pass

    async def wait_one(self, timeout: float) -> None:
        lease, window = self._lease, self._lease["ack_window_seconds"]
        self.waits += 1
        if self.waits > lease["max_delivery_attempts"]:
            assert self.waits == lease["max_delivery_attempts"] + 1
            assert (
                leases.relay_get(
                    self._database,
                    self._event_bus,
                    lease["id"],
                    lease["relay_token"],
                    local_machine=self._machine.name(),
                )["status"]
                == "active"
            )
            leases.release(
                self._database,
                self._event_bus,
                lease["id"],
                "Explicit test end after delivery exhaustion",
                local_machine=self._machine.name(),
            )
            return
        elapse(self._db_conn, lease, window - 1)
        inbox = leases.relay_inbox(
            self._database, lease["id"], lease["relay_token"], local_machine=self._machine.name()
        )
        assert not inbox[0]["delivery_due"]
        status = leases.relay_get(
            self._database,
            self._event_bus,
            lease["id"],
            lease["relay_token"],
            local_machine=self._machine.name(),
        )
        assert status["status"] == "active"
        elapse(self._db_conn, lease, window + 1)

    async def stop(self) -> tuple[str, ...]:
        self.closed = True
        return ()


def assert_pushes_follow_snapshotted_budget(
    pushes: list[str], mid: int, window: int, attempts: int
) -> None:
    deliveries = [push for push in pushes if f"[id={mid}]" in push]
    assert len(deliveries) == attempts
    for count, push in enumerate(deliveries, 1):
        assert f"ACK within {window}s" in push
        assert f"Delivery attempt {count}/{attempts}" in push
        assert ("final delivery" in push) == (count == attempts)
    assert f"Each message has {attempts} delivery attempts" in pushes[0]
    assert f"with {window}s to ACK each" in pushes[0]
    assert all("Meiva control expired" not in push for push in pushes)


@pytest.mark.parametrize("active", [(180, 2), (7, 1), (60, 3)], indirect=True)
@pytest.mark.parametrize("crash_after_first_submission", [False, True])
async def test_real_relay_uses_snapshotted_config_across_restart(
    db_conn: psycopg.Connection,
    active: ActiveSession,
    monkeypatch: pytest.MonkeyPatch,
    crash_after_first_submission: bool,
    tmp_path: Path,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    from uuid import UUID

    from cli.commands.agents import impersonation_relay as relay

    lease, owner, mid = active
    window, attempts = lease["ack_window_seconds"], lease["max_delivery_attempts"]
    # Later config edits cannot change a lease's promised deadlines or budget.
    (tmp_path / ".env").write_text(
        "MEIVA_IMPERSONATION_ACK_WINDOW_SECONDS=999\nMEIVA_IMPERSONATION_MAX_DELIVERY_ATTEMPTS=9\n"
    )
    pushes: list[str] = []
    monkeypatch.setattr(relay, "MIN_EMIT_INTERVAL_SECONDS", 0)

    async def read() -> relay.InboxSnapshot:
        return relay.read_relay_inbox(
            database,
            event_bus,
            owner.agent_id,
            UUID(lease["id"]),
            lease["relay_token"],
            local_machine=machine_identity.name(),
        )

    claims = 0

    async def claim(ids: list[int]) -> frozenset[int]:
        nonlocal claims
        claims += 1
        assert claims <= attempts + 2  # bound regressions that repeatedly refuse a due attempt
        return delivery.reserve_delivery(
            database,
            event_bus,
            lease["id"],
            lease["relay_token"],
            ids,
            local_machine=machine_identity.name(),
        )

    if crash_after_first_submission:

        def crash(push: str) -> None:
            pushes.append(push)
            if f"[id={mid}]" in push:
                raise RuntimeError("host accepted but relay died")

        first_listener = ClockListener(
            db_conn, lease, database, event_bus, machine_identity=machine_identity
        )
        with pytest.raises(RuntimeError, match="relay died"):
            await relay.relay_inbox(
                owner.agent_id,
                0,
                read_inbox=read,
                reserve=claim,
                listener=first_listener,
                emit=crash,
                debounce=0,
            )
        assert first_listener.closed
        assert (
            leases.relay_inbox(
                database, lease["id"], lease["relay_token"], local_machine=machine_identity.name()
            )[0]["delivery_attempts"]
            == 1
        )

    listener = ClockListener(db_conn, lease, database, event_bus, machine_identity=machine_identity)
    await relay.relay_inbox(
        owner.agent_id,
        0,
        read_inbox=read,
        reserve=claim,
        listener=listener,
        emit=pushes.append,
        debounce=0,
    )
    assert_pushes_follow_snapshotted_budget(pushes, mid, window, attempts)
    assert listener.closed and listener.waits == attempts + 1
    assert_message_pending(db_conn, mid)
