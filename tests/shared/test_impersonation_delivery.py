"""Database-enforced delivery budget, races, restart and native handoff."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, LiteralString, cast
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from shared.agents import impersonation as leases
from shared.agents.impersonation import impersonation_delivery as delivery
from shared.caller_identity import CallerIdentity
from shared.db import create_agent, insert_inbound_message
from shared.machine import machine_name
from shared.runtime_incarnation import RuntimeIncarnation
from tests.impersonation_support import attested_caller, recorded_tree

type ActiveSession = tuple[dict[str, Any], RuntimeIncarnation, int]


@pytest.fixture
def active(
    db_conn: psycopg.Connection,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> ActiveSession:
    from shared import runtime_config

    window, attempts = getattr(request, "param", (180, 2))
    monkeypatch.setattr(runtime_config, "_ava_home", lambda: tmp_path)
    (tmp_path / ".env").write_text(
        f"AVA_IMPERSONATION_ACK_WINDOW_SECONDS={window}\n"
        f"AVA_IMPERSONATION_MAX_DELIVERY_ATTEMPTS={attempts}\n"
    )
    agent_id = create_agent(db_conn)
    owner = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '1 hour')",
        (agent_id, machine_name(), owner.generation, owner.owner),
    )
    db_conn.commit()
    lease = leases.request(
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
        ttl_seconds=3600,
        reason="Test ACK exhaustion",
        relay_provider="claude",
        process_metadata=recorded_tree(),
    )
    assert (lease["ack_window_seconds"], lease["max_delivery_attempts"]) == (window, attempts)
    leases.accept(lease["id"], agent_id, owner, "Process and ACK the test message")
    leases.activate(lease["id"], owner)
    message_id = insert_inbound_message(
        db_conn, agent_id, "Unprocessed user input", "user", kind="chat"
    )
    db_conn.commit()
    leases.relay_inbox(lease["id"], lease["relay_token"])
    return lease, owner, message_id


def reserve(lease: dict[str, Any], message_id: int) -> frozenset[int]:
    return delivery.reserve_delivery(lease["id"], lease["relay_token"], [message_id])


def elapse(db_conn: psycopg.Connection, lease: dict[str, Any], seconds: int = 181) -> None:
    db_conn.execute(
        "UPDATE agent_impersonation_messages SET last_delivery_at="
        "clock_timestamp() - %s*interval '1 second' WHERE lease_id=%s AND delivery_attempts>0",
        (seconds, lease["id"]),
    )
    db_conn.commit()


def test_two_attempts_each_get_a_full_window_then_native_observes_expiry(
    db_conn: psycopg.Connection, active: ActiveSession
) -> None:
    lease, owner, mid = active
    assert (lease["ack_window_seconds"], lease["max_delivery_attempts"]) == (180, 2)
    assert reserve(lease, mid) == {mid}
    assert reserve(lease, mid) == set()  # no early retry
    elapse(db_conn, lease)
    assert leases.relay_get(lease["id"], lease["relay_token"])["status"] == "active"
    assert reserve(lease, mid) == {mid}
    elapse(db_conn, lease, 179)
    assert leases.relay_get(lease["id"], lease["relay_token"])["status"] == "active"
    assert reserve(lease, mid) == set()
    elapse(db_conn, lease)
    # The native reconciler also enforces the budget without a live relay.
    ended = leases.native_status(owner.agent_id, owner)
    assert ended is not None
    assert ended["status"] == "expired"
    assert f"did not ACK message {mid} after 2 delivery attempts" in ended["rejection_reason"]
    assert ended["reason"] == "Test ACK exhaustion"
    assert reserve(lease, mid) == set()
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (mid,)
    ).fetchone() == ("pending",)
    note = db_conn.execute(
        "SELECT content FROM inbound_messages WHERE id=%s", (ended["summary_inbound_id"],)
    ).fetchone()
    assert note is not None
    assert "did not ACK" in note[0] and "Unacknowledged messages remain pending" in note[0]


def test_delivery_reservation_that_expires_lease_refreshes_roster(
    db_conn: psycopg.Connection, active: ActiveSession, monkeypatch: pytest.MonkeyPatch
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

    def record_wake(agent_id: int, _reason: str) -> None:
        wakes.append(agent_id)

    monkeypatch.setattr(delivery, "publish_inbound_wake", record_wake)
    monkeypatch.setattr(delivery, "publish_impersonation_changed_sync", timelines.append)
    monkeypatch.setattr(delivery, "publish_agent_updated_sync", rosters.append)
    assert reserve(lease, message_id) == frozenset()
    assert wakes == timelines == rosters == [owner.agent_id]
    assert reserve(lease, message_id) == frozenset()
    assert wakes == timelines == rosters == [owner.agent_id]


def test_ack_in_final_window_prevents_expiry(
    db_conn: psycopg.Connection, active: ActiveSession
) -> None:
    lease, _, mid = active
    reserve(lease, mid)
    elapse(db_conn, lease)
    reserve(lease, mid)
    leases.ack(lease["id"], attested_caller(lease), [mid])
    elapse(db_conn, lease)
    assert leases.relay_get(lease["id"], lease["relay_token"])["status"] == "active"
    assert reserve(lease, mid) == set()


def test_rotating_relay_does_not_reset_budget(
    db_conn: psycopg.Connection, active: ActiveSession
) -> None:
    lease, owner, mid = active
    reserve(lease, mid)
    elapse(db_conn, lease)
    reserve(lease, mid)
    leases.provision_relay(lease["id"], owner, "replacement")
    with pytest.raises(leases.ImpersonationError, match="Invalid relay token"):
        reserve(lease, mid)
    lease["relay_token"] = "replacement"  # noqa: S105 — test-only scoped credential
    rows = leases.relay_inbox(lease["id"], "replacement")
    assert rows[0]["delivery_attempts"] == 2
    assert rows[0]["delivery_due"] is False
    elapse(db_conn, lease)
    assert reserve(lease, mid) == set()
    assert leases.relay_get(lease["id"], "replacement")["status"] == "expired"


def test_concurrent_relays_only_reserve_once(
    db_conn: psycopg.Connection, active: ActiveSession
) -> None:
    lease, _, mid = active
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(reserve, lease, mid) for _ in range(2)]
    assert sorted(len(f.result()) for f in futures) == [0, 1]
    assert leases.relay_inbox(lease["id"], lease["relay_token"])[0]["delivery_attempts"] == 1


def test_ack_after_read_before_reservation_cannot_be_pushed(
    db_conn: psycopg.Connection, active: ActiveSession
) -> None:
    lease, _, mid = active
    leases.ack(lease["id"], attested_caller(lease), [mid])
    assert reserve(lease, mid) == set()


def test_release_wins_timeout_race_without_being_overwritten(
    db_conn: psycopg.Connection, active: ActiveSession
) -> None:
    lease, _, mid = active
    reserve(lease, mid)
    elapse(db_conn, lease)
    reserve(lease, mid)
    leases.release(lease["id"], attested_caller(lease), "Returning control")
    elapse(db_conn, lease)
    assert reserve(lease, mid) == set()
    assert leases.relay_get(lease["id"], lease["relay_token"])["status"] == "released"


def test_exhausted_message_outside_page_still_ends_lease(
    db_conn: psycopg.Connection, active: ActiveSession
) -> None:
    lease, _, first = active
    other = insert_inbound_message(
        db_conn, lease["agent_id"], "Second message", "user", kind="chat"
    )
    db_conn.commit()
    leases.relay_inbox(lease["id"], lease["relay_token"])
    reserve(lease, other)
    elapse(db_conn, lease)
    reserve(lease, other)
    rows = leases.relay_inbox(lease["id"], lease["relay_token"], limit=1)
    assert [row["id"] for row in rows] == [first]
    # Fresh input and ACK of another message cannot forgive the exhausted one.
    reserve(lease, first)
    leases.ack(lease["id"], attested_caller(lease), [first])
    elapse(db_conn, lease)
    ended = leases.relay_get(lease["id"], lease["relay_token"])
    assert ended["status"] == "expired"
    assert f"message {other}" in ended["rejection_reason"]


def test_migration_roundtrip_initializes_old_unacked_rows(
    db_conn: psycopg.Connection, active: ActiveSession
) -> None:
    lease, _, mid = active
    root = Path(__file__).parents[2] / "migrations"
    stem = "20260922T053200_impersonation-delivery-budget"
    with db_conn.transaction(force_rollback=True):
        db_conn.execute(sql.SQL(cast(LiteralString, (root / f"{stem}.down.sql").read_text())))
        db_conn.execute(sql.SQL(cast(LiteralString, (root / f"{stem}.sql").read_text())))
        assert db_conn.execute(
            "SELECT delivery_attempts,last_delivery_at,acknowledged_at "
            "FROM agent_impersonation_messages WHERE lease_id=%s AND inbound_id=%s",
            (lease["id"], mid),
        ).fetchone() == (0, None, None)


@pytest.mark.parametrize("active", [(180, 2), (7, 1), (60, 3)], indirect=True)
@pytest.mark.parametrize("crash_after_first_submission", [False, True])
async def test_real_relay_uses_snapshotted_config_across_restart(
    db_conn: psycopg.Connection,
    active: ActiveSession,
    monkeypatch: pytest.MonkeyPatch,
    crash_after_first_submission: bool,
    tmp_path: Path,
) -> None:
    from uuid import UUID

    from cli.commands import impersonation_relay as relay

    lease, owner, mid = active
    window, attempts = lease["ack_window_seconds"], lease["max_delivery_attempts"]
    # Later config edits cannot change a lease's promised deadlines or budget.
    (tmp_path / ".env").write_text(
        "AVA_IMPERSONATION_ACK_WINDOW_SECONDS=999\nAVA_IMPERSONATION_MAX_DELIVERY_ATTEMPTS=9\n"
    )
    pushes: list[str] = []
    monkeypatch.setattr(relay, "_MIN_EMIT_INTERVAL_SECONDS", 0)

    async def read() -> relay.InboxSnapshot:
        return relay._read_inbox(owner.agent_id, UUID(lease["id"]), lease["relay_token"])

    claims = 0

    async def claim(ids: list[int]) -> frozenset[int]:
        nonlocal claims
        claims += 1
        assert claims <= attempts + 2  # bound regressions that repeatedly refuse a due attempt
        return delivery.reserve_delivery(lease["id"], lease["relay_token"], ids)

    class ClockListener:
        closed = False
        waits = 0

        async def ensure_listening(self) -> None:
            pass

        async def wait_one(self, timeout: float) -> None:
            self.waits += 1
            assert self.waits <= attempts  # ignore-config regressions must fail, not hang
            elapse(db_conn, lease, window - 1)
            assert not leases.relay_inbox(lease["id"], lease["relay_token"])[0]["delivery_due"]
            assert leases.relay_get(lease["id"], lease["relay_token"])["status"] == "active"
            elapse(db_conn, lease, window + 1)

        async def close(self) -> None:
            self.closed = True

    if crash_after_first_submission:

        def crash(push: str) -> None:
            pushes.append(push)
            if f"[id={mid}]" in push:
                raise RuntimeError("host accepted but relay died")

        first_listener = ClockListener()
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
        assert leases.relay_inbox(lease["id"], lease["relay_token"])[0]["delivery_attempts"] == 1

    listener = ClockListener()
    await relay.relay_inbox(
        owner.agent_id,
        0,
        read_inbox=read,
        reserve=claim,
        listener=listener,
        emit=pushes.append,
        debounce=0,
    )
    deliveries = [push for push in pushes if f"[id={mid}]" in push]
    assert len(deliveries) == attempts
    for count, push in enumerate(deliveries, 1):
        assert f"ACK within {window}s" in push
        assert f"Delivery attempt {count}/{attempts}" in push
        assert ("final delivery" in push) == (count == attempts)
    assert f"Each message has {attempts} delivery attempts" in pushes[0]
    assert f"with {window}s to ACK each" in pushes[0]
    assert "Ava control expired" in pushes[-1]
    assert "did not ACK" in pushes[-1]
    assert listener.closed and listener.waits == attempts
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (mid,)
    ).fetchone() == ("pending",)


def test_rollback_refuses_to_reset_an_active_budget(
    db_conn: psycopg.Connection, active: ActiveSession
) -> None:
    lease, _, mid = active
    reserve(lease, mid)
    rollback = (
        Path(__file__).parents[2]
        / "migrations/20260922T053200_impersonation-delivery-budget.down.sql"
    )
    with (
        db_conn.transaction(force_rollback=True),
        pytest.raises(psycopg.errors.RaiseException, match="End active impersonations"),
    ):
        db_conn.execute(sql.SQL(cast(LiteralString, rollback.read_text())))


@pytest.mark.parametrize("status", ["active", "released"])
def test_config_migration_preserves_existing_policy_and_attempts(
    db_conn: psycopg.Connection,
    active: ActiveSession,
    status: str,
) -> None:
    lease, _, mid = active
    reserve(lease, mid)
    root = Path(__file__).parents[2] / "migrations"
    stem = "20260922T075826_impersonation-delivery-config"
    with db_conn.transaction(force_rollback=True):
        db_conn.execute(
            "UPDATE agent_impersonations SET ack_window_seconds=300,status=%s WHERE id=%s",
            (status, lease["id"]),
        )
        before = db_conn.execute(
            "SELECT delivery_attempts,last_delivery_at FROM agent_impersonation_messages "
            "WHERE lease_id=%s AND inbound_id=%s",
            (lease["id"], mid),
        ).fetchone()
        for suffix in (".down.sql", ".sql"):
            db_conn.execute(sql.SQL(cast(LiteralString, (root / (stem + suffix)).read_text())))
        assert db_conn.execute(
            "SELECT ack_window_seconds,max_delivery_attempts FROM agent_impersonations WHERE id=%s",
            (lease["id"],),
        ).fetchone() == (300, 2)
        assert (
            db_conn.execute(
                "SELECT delivery_attempts,last_delivery_at FROM agent_impersonation_messages "
                "WHERE lease_id=%s AND inbound_id=%s",
                (lease["id"], mid),
            ).fetchone()
            == before
        )
        assert db_conn.execute(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name='agent_impersonations' AND column_name='ack_window_seconds'"
        ).fetchone() == ("180",)


@pytest.mark.parametrize("historical_attempts", [False, True])
def test_config_rollback_refuses_policy_or_history_loss(
    db_conn: psycopg.Connection,
    active: ActiveSession,
    historical_attempts: bool,
) -> None:
    lease, _, mid = active
    rollback = (
        Path(__file__).parents[2]
        / "migrations/20260922T075826_impersonation-delivery-config.down.sql"
    )
    with db_conn.transaction(force_rollback=True):
        if historical_attempts:
            db_conn.execute(
                "UPDATE agent_impersonations SET status='released' WHERE id=%s", (lease["id"],)
            )
            db_conn.execute(
                "UPDATE agent_impersonation_messages SET delivery_attempts=3,last_delivery_at=clock_timestamp() "
                "WHERE lease_id=%s AND inbound_id=%s",
                (lease["id"], mid),
            )
        with (
            pytest.raises(psycopg.errors.RaiseException, match="Cannot downgrade"),
            db_conn.transaction(),
        ):
            db_conn.execute(sql.SQL(cast(LiteralString, rollback.read_text())))
