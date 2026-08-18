"""`_notify_agents_of_rollback` — the rollback system-note fan-out + Redis wake.

Symmetric with `shared.db.signal_live_agents_restart`: it bulk-INSERTs one
`chat` rollback note per live (running/idling) agent and publishes a per-agent
Redis wake, so an idling agent sees the note now instead of at its next inbound-
wait SELECT recheck. Each helper opens its own connection, so it sees rows
committed by the `db_conn` fixture.
"""

from __future__ import annotations

import asyncio
import time

import psycopg
import pytest

from cli.commands._cluster_rollback import _notify_agents_of_rollback
from shared import db
from shared.config import settings
from shared.redis_listener import RedisInboundListener


def _seed_agent(db_conn: psycopg.Connection, status: str, *, live_lease: bool = True) -> int:
    """Create an agent + its agents_meta row in the given status, return id.

    `live_lease` grants the R1 liveness lease (default True — a seeded live
    agent renews like a real one); pass False to seed a lease-less (pre-lease /
    zombie) row, which the alive predicate reads as dead."""
    from datetime import UTC, datetime, timedelta

    agent_id = db.create_agent(db_conn)
    lease = datetime.now(UTC) + timedelta(seconds=600) if live_lease else None
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status, lease_expires_at) "
            "VALUES (%s, 'test', %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, "
            "    lease_expires_at = EXCLUDED.lease_expires_at",
            (agent_id, status, lease),
        )
    db_conn.commit()
    return agent_id


def _rollback_notes(db_conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str]]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT kind, source FROM inbound_messages WHERE agent_id = %s",
            (agent_id,),
        )
        return cur.fetchall()


def test_notify_only_live_agents(db_conn: psycopg.Connection) -> None:
    """One rollback chat note per running/idling agent; terminated gets none.
    Guards the `RETURNING agent_id` change — the returned set drives the wake
    fan-out, so it must be exactly the live agents."""
    running = _seed_agent(db_conn, "running")
    idling = _seed_agent(db_conn, "idling")
    terminated = _seed_agent(db_conn, "terminated")

    _notify_agents_of_rollback("aaaaaaa", "bbbbbbb")

    assert _rollback_notes(db_conn, running) == [("chat", "system:rollback")]
    assert _rollback_notes(db_conn, idling) == [("chat", "system:rollback")]
    assert _rollback_notes(db_conn, terminated) == []


async def test_publishes_redis_wake_per_agent(db_conn: psycopg.Connection) -> None:
    """The fan-out wakes each notified agent over Redis (symmetric with
    signal_live_agents_restart) — park a per-agent listener, fire the notify,
    assert the parked wait wakes."""
    tid = _seed_agent(db_conn, "idling")
    listener = RedisInboundListener(settings.data_plane.redis_url, tid)
    try:
        wait_task = asyncio.create_task(listener.wait_one(timeout=10.0))
        await asyncio.sleep(0.2)  # let the subscribe take effect before the publish
        t0 = time.monotonic()
        await asyncio.to_thread(_notify_agents_of_rollback, "aaaaaaa", "bbbbbbb")
        await asyncio.wait_for(wait_task, timeout=5.0)
        assert time.monotonic() - t0 < 5.0, "rollback notify did not wake the parked listener"
    finally:
        await listener.close()


# ─── the rollback is also an OBSERVER of the update it is cleaning up after ───


def test_the_rollback_records_what_it_did_on_the_last_update(db_conn: psycopg.Connection) -> None:
    """The orchestration that died filed no report, but the process cleaning up after
    it provably witnessed the death. Without this the operator sees the pin move to an
    older commit and no statement of why — the 2026-07-30 puzzle exactly.

    The rollback still only reports: it writes the sentence and leaves the stored
    verdict alone. Turning "an orphaned update, since recovered" into `RECOVERED` is
    the reader's move, so nothing here can overwrite a verdict another process filed.
    """
    from cli.commands._cluster_rollback import _note_rollback_on_last_update
    from shared.last_update import UpdateOutcome, begin_update, read_last_update

    begin_update(target_sha="8bdd3667", origin="frontend", holder="mini:pid999")

    _note_rollback_on_last_update("8bdd3667aa", "7e571b49aa")

    record = read_last_update()
    assert record is not None
    assert record.observed_by == "rolled back 8bdd366 -> 7e571b4"
    assert record.outcome is UpdateOutcome.RECOVERED
    assert record.failed is True, "a recovered update is still one the operator has to be told of"
    with db_conn.cursor() as cur:
        cur.execute("SELECT outcome FROM cluster_last_update WHERE id=1")
        stored = cur.fetchone()
        assert stored is not None and stored[0] is None, "the observer wrote no verdict"
        cur.execute("UPDATE cluster_last_update SET started_at=NULL, observed_by=NULL WHERE id=1")
    db_conn.commit()


def test_a_failed_annotation_never_fails_the_rollback(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The rollback already succeeded by the time this runs. A note that cannot be
    written must not turn a working recovery into a reported failure."""
    from cli.commands import _cluster_rollback as mod

    def _boom(_reason: str) -> None:
        raise RuntimeError("db gone")

    monkeypatch.setattr("shared.last_update.note_observed_recovery", _boom)

    mod._note_rollback_on_last_update("8bdd3667aa", "7e571b49aa")  # must not raise

    assert "could not annotate" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
