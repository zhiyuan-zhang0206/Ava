"""The hosted corpse reaper's recrash trigger against real rows — `agent/corpse_reap.py`.

The beat reaper terminates marked idling corpses once the grace window
elapses; the prompt reap (task #3616) terminates the row this incarnation just
re-crashed, at the settle point that witnessed the crash, with the same
termination shape and events plus the confirmed crash count. These lock the
row-visible contract: only the crashed incarnation's own marked idling row is
touched, a row that moved on is refused, and the reap's CAS composes with the
admission and resurrection transitions without tearing.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.corpse_reap import RECRASH_CONFIRMED_CRASHES, reap_recrashed_corpse
from agent.hosted_ownership import admit_hosted_runtime, settle_hosted_runtime
from ops import agent_wake
from shared.db import create_agent
from shared.runtime_incarnation import RuntimeIncarnation


def _agent(conn: psycopg.Connection[Any], machine: str = "host-test") -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, status, machine) VALUES (%s, 'idling', %s) "
        "ON CONFLICT (id) DO UPDATE SET status = 'idling', machine = %s",
        (agent_id, machine, machine),
    )
    conn.commit()
    return agent_id


async def _recrashed_row(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    *,
    machine: str = "host-test",
) -> tuple[int, RuntimeIncarnation]:
    """The row at a re-crash's settle point: idling, marked, owned by the incarnation.

    Built the way the settle boundary leaves it — admission, then the first
    crash's mark — so the prompt reap reads a pre-existing stamp under the
    live incarnation, exactly what `settle_and_stamp_turn` hands it.
    """
    agent_id, owner = _agent(db_conn, machine=machine), uuid4()
    incarnation = await admit_hosted_runtime(
        aops_pool, agent_id, machine, owner, expected_from="idling"
    )
    assert incarnation is not None
    db_conn.execute(
        "UPDATE agents_meta SET last_turn_fatal_at = now() - interval '1 minute' WHERE id = %s",
        (agent_id,),
    )
    db_conn.commit()
    assert await settle_hosted_runtime(aops_pool, incarnation)
    return agent_id, incarnation


@pytest.fixture
def reap_spies(monkeypatch: pytest.MonkeyPatch) -> tuple[list[dict[str, object]], list[int]]:
    """Capture the reap's durable event and its frontend announce."""
    events: list[dict[str, object]] = []
    published: list[int] = []

    async def _event(
        event_type: str, agent_id: int, *, payload: dict[str, object] | None = None, **_kw: object
    ) -> None:
        del event_type
        if payload is not None and payload.get("reason") == "corpse_reaper":
            events.append(payload)

    async def _publish(_pool: object, agent_id: int) -> None:
        published.append(agent_id)

    monkeypatch.setattr("agent.corpse_reap.insert_event_log_async", _event)
    monkeypatch.setattr("agent.corpse_reap.publish_agent_updated", _publish)
    return events, published


async def test_prompt_reap_terminates_the_incarnations_marked_idling_row(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    reap_spies: tuple[list[dict[str, object]], list[int]],
    loguru_records: list[dict[str, Any]],
) -> None:
    events, published = reap_spies
    agent_id, incarnation = await _recrashed_row(db_conn, aops_pool)
    published.clear()

    assert await reap_recrashed_corpse(aops_pool, incarnation) == [agent_id]

    row = db_conn.execute(
        "SELECT status, termination_source, lease_expires_at, "
        "last_turn_fatal_at IS NOT NULL FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone()
    assert row is not None
    # The beat reaper's terminal shape: terminated by the reaper, lease
    # released, and the mark kept — it names the death.
    assert row == ("terminated", "reaper", None, True)

    assert events == [
        {
            "from": "idling",
            "to": "terminated",
            "reason": "corpse_reaper",
            "crash_count": RECRASH_CONFIRMED_CRASHES,
        }
    ]
    assert published == [agent_id]

    # The reap itself neither resurrects nor consumes recovery bookkeeping —
    # the age/budget/suppression boundaries of the recovery paths stay out of
    # this mechanism's hands.
    assert db_conn.execute(
        "SELECT wake_suppressed_until, last_resurrect_at FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone() == (None, None)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id = %s AND kind = 'resurrect'",
        (agent_id,),
    ).fetchone() == (0,)
    reaped_records = [
        r for r in loguru_records if r["extra"].get("event") == "corpse_reaper_terminated"
    ]
    assert [r["extra"]["crash_count"] for r in reaped_records] == [RECRASH_CONFIRMED_CRASHES]

    # Already terminated: a second prompt reap has no subject.
    assert await reap_recrashed_corpse(aops_pool, incarnation) == []
    assert len(events) == 1


async def test_prompt_reap_refuses_rows_that_are_not_its_marked_idling_own(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    reap_spies: tuple[list[dict[str, object]], list[int]],
) -> None:
    events, published = reap_spies
    agent_id, incarnation = await _recrashed_row(db_conn, aops_pool)
    published.clear()

    # A running row (a live turn or claim park) is never touched.
    db_conn.execute("UPDATE agents_meta SET status='running' WHERE id=%s", (agent_id,))
    db_conn.commit()
    assert await reap_recrashed_corpse(aops_pool, incarnation) == []

    # Nor is an unmarked live row.
    db_conn.execute(
        "UPDATE agents_meta SET status='idling', last_turn_fatal_at=NULL WHERE id=%s",
        (agent_id,),
    )
    db_conn.commit()
    assert await reap_recrashed_corpse(aops_pool, incarnation) == []

    # Nor a marked row owned by a different incarnation (concurrent replacement).
    db_conn.execute(
        "UPDATE agents_meta SET last_turn_fatal_at=now(), runtime_generation=%s, "
        "runtime_owner=%s WHERE id=%s",
        (uuid4(), uuid4(), agent_id),
    )
    db_conn.commit()
    assert await reap_recrashed_corpse(aops_pool, incarnation) == []

    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id = %s", (agent_id,)
    ).fetchone() == ("idling",)
    assert events == [] and published == []


async def test_prompt_reap_and_admission_resolve_to_one_winner(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    reap_spies: tuple[list[dict[str, object]], list[int]],
) -> None:
    """The reap and the next wake's admission are CASes over one row.

    Concurrently, each interleaving must leave exactly one transition applied
    — the reap (idling -> terminated) or the admission (idling -> running) —
    and the loser must refuse, never both and never a torn mix.
    """
    for _ in range(4):
        agent_id, incarnation = await _recrashed_row(db_conn, aops_pool)
        reaped, admitted = await asyncio.gather(
            reap_recrashed_corpse(aops_pool, incarnation),
            admit_hosted_runtime(
                aops_pool, agent_id, "host-test", incarnation.owner, expected_from="idling"
            ),
        )
        status = db_conn.execute(
            "SELECT status FROM agents_meta WHERE id = %s", (agent_id,)
        ).fetchone()
        if reaped:
            assert reaped == [agent_id]
            assert admitted is None
            assert status == ("terminated",)
        else:
            assert admitted is not None
            assert status == ("running",)

    # Serialized both ways: whichever transition runs second refuses.
    agent_id, incarnation = await _recrashed_row(db_conn, aops_pool)
    assert await reap_recrashed_corpse(aops_pool, incarnation) == [agent_id]
    assert (
        await admit_hosted_runtime(
            aops_pool, agent_id, "host-test", incarnation.owner, expected_from="idling"
        )
        is None
    )

    agent_id2, incarnation2 = await _recrashed_row(db_conn, aops_pool)
    assert (
        await admit_hosted_runtime(
            aops_pool, agent_id2, "host-test", incarnation2.owner, expected_from="idling"
        )
        is not None
    )
    assert await reap_recrashed_corpse(aops_pool, incarnation2) == []


async def test_prompt_reap_then_resurrect_composes_without_tearing(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    reap_spies: tuple[list[dict[str, object]], list[int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reaped corpse's next transition is the resurrect — run it against a
    second reap attempt and assert one winner per state and no torn row.

    The resurrect transition (terminated -> idling) clears the mark, the
    ownership, and the lease in one statement; the second reap can never
    re-fire against any state. Whatever the interleaving, the row ends either
    still terminated-and-owned or fully revived — never a blend.
    """
    wakes: list[int] = []

    def _wake(agent_id: int, payload: str) -> None:
        del payload
        wakes.append(agent_id)

    monkeypatch.setattr(agent_wake, "publish_inbound_wake", _wake)

    agent_id, incarnation = await _recrashed_row(db_conn, aops_pool)
    assert await reap_recrashed_corpse(aops_pool, incarnation) == [agent_id]

    second_reap, resurrected = await asyncio.gather(
        reap_recrashed_corpse(aops_pool, incarnation),
        asyncio.to_thread(agent_wake.resurrect_agent, agent_id, resurrected_by="user"),
    )

    assert second_reap == []
    assert resurrected == agent_id
    assert wakes == [agent_id]
    row = db_conn.execute(
        "SELECT status, termination_source, last_turn_fatal_at, runtime_owner, lease_expires_at "
        "FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone()
    # The resurrect's full cleanup landed atomically: no residue that would
    # let the reaper re-terminate the fresh life.
    assert row == ("idling", None, None, None, None)
