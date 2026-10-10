"""Authority, terminal receipts and recovery CAS under actual database contention."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import psycopg
import pytest

from base.agents import impersonation as leases
from base.agents.impersonation import host_transport, relay, terminal_notices
from base.agents.messages.caller_identity import CallerIdentity
from base.cluster.machine import machine_name
from base.config.service_read import ConfigAuthority
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from tests.impersonation_support import attested_caller, recorded_tree


@pytest.fixture
def active(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> tuple[dict[str, Any], RuntimeIncarnation]:
    aid = create_agent(db_conn)
    owner = RuntimeIncarnation(aid, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',clock_timestamp()+interval '1 hour')",
        (aid, machine_name(), owner.generation, owner.owner),
    )
    db_conn.commit()
    session = leases.request(
        database,
        event_bus,
        aid,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
        ttl_seconds=3600,
        reason="Lifecycle contract test",
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
        relay_codex_remote="unix:///tmp/recorded-codex.sock",
        process_metadata=recorded_tree(),
        authority=config_authority,
    )
    leases.accept(database, event_bus, session["id"], aid, owner, "Continue this task")
    leases.activate(database, event_bus, session["id"], owner)
    return session, owner


def end(database: Database, event_bus: EventBus, session: dict[str, Any]) -> None:
    leases.release(
        database, event_bus, session["id"], attested_caller(session), "Saved unfinished task"
    )


def test_generation_claim_is_single_winner_and_stale_token_loses_authority(
    active: tuple[dict[str, Any], RuntimeIncarnation],
    database: Database,
    event_bus: EventBus,
) -> None:
    session, owner = active
    tokens = ["first-private-token", "second-private-token"]

    def claim(token: str) -> dict[str, Any] | None:
        return relay.provision_relay(database, session["id"], owner, token, expected_generation=0)

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(claim, tokens))
    assert sum(value is not None for value in outcomes) == 1
    winner = tokens[next(i for i, value in enumerate(outcomes) if value is not None)]
    other = tokens[1 - tokens.index(winner)]
    with pytest.raises(leases.ImpersonationError, match="Invalid relay token"):
        leases.relay_heartbeat(database, session["id"], other)
    latest = leases.relay_get(database, event_bus, session["id"], winner)
    assert latest["relay_generation"] == 1
    assert latest["expires_at"] <= session["expires_at"] + __import__("datetime").timedelta(
        seconds=1
    )
    assert (
        relay.provision_relay(database, session["id"], owner, "loser", expected_generation=0)
        is None
    )
    assert leases.relay_get(database, event_bus, session["id"], winner)["relay_generation"] == 1


def test_terminal_notice_claim_has_one_sender_and_persists_host_acceptance(
    active: tuple[dict[str, Any], RuntimeIncarnation],
    database: Database,
    event_bus: EventBus,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _ = active
    end(database, event_bus, session)
    entered, complete = Event(), Event()
    calls: list[tuple[str, str, str]] = []

    def submit(thread: str, message: str, *, endpoint: str) -> None:
        calls.append((thread, message, endpoint))
        entered.set()
        assert complete.wait(5)

    monkeypatch.setattr(host_transport, "live_submit", submit)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(terminal_notices.deliver_pending_notice, database, machine_name())
        assert entered.wait(5)
        second = pool.submit(terminal_notices.deliver_pending_notice, database, machine_name())
        assert second.result(timeout=5) is False
        complete.set()
        assert first.result(timeout=5) is True
    assert len(calls) == 1
    assert calls[0][0] == session["relay_thread_id"]
    assert calls[0][2] == session["relay_codex_remote"]
    assert session["id"] in calls[0][1] and "does not end or cancel any newer" in calls[0][1]
    assert db_conn.execute(
        "SELECT terminal_notice_attempts,terminal_notice_accepted_at IS NOT NULL FROM agent_impersonations WHERE id=%s",
        (session["id"],),
    ).fetchone() == (1, True)
    assert terminal_notices.deliver_pending_notice(database, machine_name()) is False
    db_conn.execute(
        "UPDATE agent_impersonations SET status='released' WHERE id=%s", (session["id"],)
    )
    db_conn.commit()
    assert db_conn.execute(
        "SELECT terminal_notice_attempts,terminal_notice_accepted_at IS NOT NULL FROM agent_impersonations WHERE id=%s",
        (session["id"],),
    ).fetchone() == (1, True)


def test_notice_transient_failure_remains_pending_beyond_three_attempts(
    active: tuple[dict[str, Any], RuntimeIncarnation],
    database: Database,
    event_bus: EventBus,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, owner = active
    end(database, event_bus, session)
    monkeypatch.setattr(
        host_transport,
        "live_submit",
        Mock(return_value="TimeoutError: ambiguous acceptance"),
    )
    for attempt in range(4):
        db_conn.execute(
            "UPDATE agent_impersonations SET terminal_notice_attempt_at=clock_timestamp()-interval '1 hour' WHERE id=%s",
            (session["id"],),
        )
        db_conn.commit()
        assert terminal_notices.deliver_pending_notice(database, machine_name())
        assert db_conn.execute(
            "SELECT terminal_notice_attempts,terminal_notice_accepted_at IS NULL FROM agent_impersonations WHERE id=%s",
            (session["id"],),
        ).fetchone() == (attempt + 1, True)
    assert leases.native_status(database, event_bus, owner.agent_id, owner) is None
    monkeypatch.setattr(host_transport, "live_submit", Mock(return_value=None))
    db_conn.execute(
        "UPDATE agent_impersonations SET terminal_notice_attempt_at=clock_timestamp()-interval '1 hour' WHERE id=%s",
        (session["id"],),
    )
    db_conn.commit()
    assert terminal_notices.deliver_pending_notice(database, machine_name())
    assert db_conn.execute(
        "SELECT terminal_notice_accepted_at IS NOT NULL FROM agent_impersonations WHERE id=%s",
        (session["id"],),
    ).fetchone() == (True,)


def test_unsupported_terminal_destination_is_visible_and_not_accepted(
    active: tuple[dict[str, Any], RuntimeIncarnation],
    database: Database,
    event_bus: EventBus,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _ = active
    db_conn.execute(
        "UPDATE agent_impersonations SET relay_provider='claude',relay_thread_id=NULL,relay_codex_remote=NULL WHERE id=%s",
        (session["id"],),
    )
    db_conn.commit()
    end(database, event_bus, session)
    monkeypatch.setattr(
        host_transport,
        "live_submit",
        Mock(side_effect=AssertionError("Unsupported destination must not submit")),
    )
    assert terminal_notices.deliver_pending_notice(database, machine_name())
    assert db_conn.execute(
        "SELECT terminal_notice_unsupported_at IS NOT NULL,terminal_notice_accepted_at IS NULL,terminal_notice_error FROM agent_impersonations WHERE id=%s",
        (session["id"],),
    ).fetchone() == (
        True,
        True,
        "Independent ended-lease delivery unsupported for this recorded destination",
    )
    assert terminal_notices.deliver_pending_notice(database, machine_name()) is False


def test_old_notice_keeps_destination_after_replacement_takeover(
    active: tuple[dict[str, Any], RuntimeIncarnation],
    database: Database,
    event_bus: EventBus,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config_authority: ConfigAuthority,
) -> None:
    session, owner = active
    end(database, event_bus, session)
    replacement = leases.request(
        database,
        event_bus,
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="replacement"),
        ttl_seconds=3600,
        reason="Replacement task",
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
        relay_codex_remote="unix:///tmp/replacement.sock",
        process_metadata=recorded_tree(),
        authority=config_authority,
    )
    leases.accept(database, event_bus, replacement["id"], owner.agent_id, owner, "Replacement task")
    leases.activate(database, event_bus, replacement["id"], owner)
    # A later edit of old routing columns does not redirect its immutable notice.
    db_conn.execute(
        "UPDATE agent_impersonations SET relay_thread_id=%s,relay_codex_remote=%s WHERE id=%s",
        (replacement["relay_thread_id"], replacement["relay_codex_remote"], session["id"]),
    )
    db_conn.commit()
    submitted = Mock(return_value=None)
    monkeypatch.setattr(host_transport, "live_submit", submitted)
    assert terminal_notices.deliver_pending_notice(database, machine_name())
    assert submitted.call_args.args[0] == session["relay_thread_id"]
    assert submitted.call_args.kwargs["endpoint"] == session["relay_codex_remote"]
    assert session["id"] in submitted.call_args.args[1]
    assert replacement["id"] not in submitted.call_args.args[1]
    assert (
        leases.require_active(database, replacement["id"], attested_caller(replacement))["status"]
        == "active"
    )


def test_terminated_agent_notice_is_scanned_without_native_runtime(
    active: tuple[dict[str, Any], RuntimeIncarnation],
    database: Database,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, owner = active
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    submitted = Mock(return_value=None)
    monkeypatch.setattr(host_transport, "live_submit", submitted)
    assert terminal_notices.deliver_pending_notice(database, machine_name())
    assert "terminated: agent was terminated" in submitted.call_args.args[1]
    assert db_conn.execute(
        "SELECT terminal_notice_accepted_at IS NOT NULL FROM agent_impersonations WHERE id=%s",
        (session["id"],),
    ).fetchone() == (True,)


def test_already_terminal_insert_is_not_historically_backfilled(
    active: tuple[dict[str, Any], RuntimeIncarnation],
    database: Database,
    event_bus: EventBus,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, owner = active
    end(database, event_bus, session)
    submitted = Mock(return_value=None)
    monkeypatch.setattr(host_transport, "live_submit", submitted)
    assert terminal_notices.deliver_pending_notice(database, machine_name())
    legacy_id = uuid4()
    db_conn.execute(
        "INSERT INTO agent_impersonations(id,agent_id,source,machine,status,ttl_seconds,expires_at,ended_at) "
        "VALUES(%s,%s,'external_agent:legacy',%s,'expired',300,clock_timestamp(),clock_timestamp())",
        (legacy_id, owner.agent_id, machine_name()),
    )
    db_conn.commit()
    assert terminal_notices.deliver_pending_notice(database, machine_name()) is False
    assert db_conn.execute(
        "SELECT terminal_notice_pending_at FROM agent_impersonations WHERE id=%s", (legacy_id,)
    ).fetchone() == (None,)
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        db_conn.execute(
            "UPDATE agent_impersonations SET terminal_notice_snapshot='{}'::jsonb WHERE id=%s",
            (session["id"],),
        )
    db_conn.rollback()


def test_notice_belongs_to_original_machine_after_agent_moves(
    active: tuple[dict[str, Any], RuntimeIncarnation],
    database: Database,
    event_bus: EventBus,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, owner = active
    end(database, event_bus, session)
    db_conn.execute(
        "UPDATE agents_meta SET machine='replacement-machine' WHERE id=%s", (owner.agent_id,)
    )
    db_conn.commit()
    submitted = Mock(return_value=None)
    monkeypatch.setattr(host_transport, "live_submit", submitted)
    assert terminal_notices.deliver_pending_notice(database, "replacement-machine") is False
    submitted.assert_not_called()
    assert terminal_notices.deliver_pending_notice(database, machine_name())
    submitted.assert_called_once()
