"""Real database resurrection: guarded status transition, durable inbound, and one host wake."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from ops import agent_wake
from ops.agent_spawn import create_agent_row
from shared.machine import machine_name

_DEAD_PID = 424243


@pytest.fixture
def wakes() -> Iterator[list[tuple[int, str]]]:
    captured: list[tuple[int, str]] = []
    yield captured


@pytest.fixture(autouse=True)
def _capture_wakes(monkeypatch: pytest.MonkeyPatch, wakes: list[tuple[int, str]]) -> Iterator[None]:
    def _record(agent_id: int, payload: str) -> None:
        wakes.append((agent_id, payload))

    # Both wake modules publish through their own namespace (the split moved
    # swap-in / revive to `ops.agent_revive`), so capture both.
    monkeypatch.setattr(agent_wake, "publish_inbound_wake", _record)
    yield


def _park(
    db: psycopg.Connection,
    *,
    status: str,
    pid: int | None = None,
) -> int:
    """Seed a row WITHOUT any launch — hosted spawn is row-only, and the
    guard above turns a stray process launch into a loud failure."""
    aid, _birth, _prompt_id, _attempt_id = create_agent_row(spawner="user", machine=machine_name())
    with db.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status=%s, pid=%s WHERE id=%s", (status, pid, aid))
    db.commit()
    return aid


def _row(db: psycopg.Connection, aid: int) -> tuple[str, int | None]:
    with db.cursor() as cur:
        cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (aid,))
        row = cur.fetchone()
    assert row is not None, f"agents_meta row {aid} missing"
    return row[0], row[1]


def _kind_rows(db: psycopg.Connection, aid: int, kind: str) -> int:
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM inbound_messages WHERE agent_id = %s AND kind = %s",
            (aid, kind),
        )
        row = cur.fetchone()
    assert row is not None, f"inbound count row for agent {aid} missing"
    return row[0]


# ── resurrect ────────────────────────────────────────────────────────────────


def test_resurrect_agent_hosted_flips_and_wakes(
    db_conn: psycopg.Connection, wakes: list[tuple[int, str]]
) -> None:
    """terminated -> idling + resurrect inbound + one wake; no launch, no
    pid-confirm polling."""
    aid = _park(db_conn, status="terminated")
    out = agent_wake.resurrect_agent(aid, resurrected_by="user")
    assert out == aid
    assert _row(db_conn, aid) == ("idling", None)
    assert _kind_rows(db_conn, aid, "resurrect") == 1
    assert wakes == [(aid, "0")]


def test_resurrect_agent_hosted_clears_the_corpse_marker(
    db_conn: psycopg.Connection, wakes: list[tuple[int, str]]
) -> None:
    """A reaper-terminated corpse keeps `last_turn_fatal_at` stamped; the
    resurrect transition must clear it or the reaper's next beat would
    re-terminate the freshly revived row (the marker is already past grace)."""
    aid = _park(db_conn, status="terminated")
    db_conn.execute(
        "UPDATE agents_meta SET termination_source='reaper', "
        "last_turn_fatal_at = now() - interval '1 hour' WHERE id = %s",
        (aid,),
    )
    db_conn.commit()
    agent_wake.resurrect_agent(aid, resurrected_by="user")
    assert db_conn.execute(
        "SELECT last_turn_fatal_at FROM agents_meta WHERE id = %s", (aid,)
    ).fetchone() == (None,)


def test_resurrect_agent_hosted_keeps_trigger_guard(
    db_conn: psycopg.Connection, wakes: list[tuple[int, str]]
) -> None:
    """The auto-resurrect trigger CAS semantics are mode-independent: a stale
    trigger still refuses the transition."""
    aid = _park(db_conn, status="terminated")
    with pytest.raises(agent_wake.ResurrectTriggerStaleError):
        agent_wake.resurrect_agent(
            aid,
            resurrected_by="system",
            trigger_inbound_id=999999,
            trigger_inbound_kind="chat",
        )
    assert _row(db_conn, aid)[0] == "terminated"
    assert wakes == []


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("managed", [False, True])
async def test_resurrection_admits_a_new_incarnation_on_the_same_host(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    guarded: bool,
    managed: bool,
) -> None:
    """An observed termination never revalidates its original runtime token."""
    from uuid import uuid4

    from psycopg.types.json import Jsonb

    from agent.db import claim_inbound_batch
    from agent.hosted_ownership import admit_hosted_runtime, apply_hosted_lifecycle
    from agent.inbound_ownership import RuntimeOwnershipLostError
    from shared.db import insert_inbound_message
    from shared.incarnation_resources import ResourceBirth
    from shared.turn_identity import bind_turn_identity

    aid = _park(db_conn, status="idling")
    if managed:
        db_conn.execute(
            "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
            (Jsonb(ResourceBirth(birth=uuid4()).model_dump(mode="json")), aid),
        )
        db_conn.commit()
    owner = uuid4()
    old = await admit_hosted_runtime(aops_pool, aid, machine_name(), owner, expected_from="idling")
    assert old is not None
    command = insert_inbound_message(db_conn, aid, "", "self", kind="terminate")
    with bind_turn_identity(aid, incarnation=old):
        assert [item.id for item in await claim_inbound_batch(aops_pool, aid)] == [command]
        assert await apply_hosted_lifecycle(aops_pool, old) == "terminate"
    trigger = insert_inbound_message(db_conn, aid, "continue", "user")
    agent_wake.resurrect_agent(
        aid,
        resurrected_by="system" if guarded else "user",
        trigger_inbound_id=trigger if guarded else None,
        trigger_inbound_kind="chat" if guarded else None,
    )
    successor = await admit_hosted_runtime(
        aops_pool, aid, machine_name(), owner, expected_from="idling"
    )
    assert successor is not None and successor.generation != old.generation
    with bind_turn_identity(aid, incarnation=old), pytest.raises(RuntimeOwnershipLostError):
        await claim_inbound_batch(aops_pool, aid)
    with bind_turn_identity(aid, incarnation=successor):
        assert {item.kind for item in await claim_inbound_batch(aops_pool, aid)} == {
            "chat",
            "resurrect",
        }


# ── relaxed trigger guard: system-reaped crash rows (task #3617) ─────────────


def _backdate_before_status(db: psycopg.Connection, aid: int, iid: int) -> None:
    with db.cursor() as cur:
        cur.execute(
            "UPDATE inbound_messages SET created_at = "
            "(SELECT status_changed_at FROM agents_meta WHERE id = %s) - interval '1 second' "
            "WHERE id = %s",
            (aid, iid),
        )
    db.commit()


def _reaped_crash_park(db: psycopg.Connection) -> tuple[int, int]:
    """terminated + reaper source + retained crash marker + a leftover chat
    that predates the termination (the relaxed-trigger shape)."""
    from shared.db import insert_inbound_message

    aid = _park(db, status="terminated")
    trigger = insert_inbound_message(db, aid, "leftover work", "user")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET termination_source = 'reaper', "
            "last_turn_fatal_at = now() - interval '1 hour' WHERE id = %s",
            (aid,),
        )
    db.commit()
    _backdate_before_status(db, aid, trigger)
    return aid, trigger


def _guarded_resurrect(aid: int, trigger: int) -> None:
    agent_wake.resurrect_agent(
        aid,
        resurrected_by="system",
        trigger_inbound_id=trigger,
        trigger_inbound_kind="chat",
    )


def test_reaped_crash_row_resumes_leftover_work(
    db_conn: psycopg.Connection, wakes: list[tuple[int, str]]
) -> None:
    """A reaper death is not an operator decision, so a chat that predates it
    still qualifies as the pending-work trigger (task #3617, design section 6)."""
    aid, trigger = _reaped_crash_park(db_conn)

    assert (
        agent_wake.resurrect_agent(
            aid, resurrected_by="system", trigger_inbound_id=trigger, trigger_inbound_kind="chat"
        )
        == aid
    )
    assert _row(db_conn, aid) == ("idling", None)
    assert wakes == [(aid, "0")]


def test_operator_death_still_refuses_leftover_work(db_conn: psycopg.Connection) -> None:
    """The crash marker alone never relaxes the fence: a user kill keeps its
    contract — the leftover chat cannot undo it."""
    from shared.db import insert_inbound_message

    aid = _park(db_conn, status="terminated")
    trigger = insert_inbound_message(db_conn, aid, "leftover work", "user")
    db_conn.execute(
        "UPDATE agents_meta SET termination_source = 'user', "
        "last_turn_fatal_at = now() - interval '1 hour' WHERE id = %s",
        (aid,),
    )
    db_conn.commit()
    _backdate_before_status(db_conn, aid, trigger)

    with pytest.raises(agent_wake.ResurrectTriggerStaleError):
        _guarded_resurrect(aid, trigger)
    assert _row(db_conn, aid)[0] == "terminated"


def test_suppressed_wakes_refuse_the_reaped_crash_trigger(db_conn: psycopg.Connection) -> None:
    """The relaxed fence does not bypass an active automatic-wake suppression."""
    aid, trigger = _reaped_crash_park(db_conn)
    db_conn.execute(
        "UPDATE agents_meta SET wake_suppressed_until = now() + interval '1 hour', "
        "wake_suppress_reason = 'resurrect_failed' WHERE id = %s",
        (aid,),
    )
    db_conn.commit()

    with pytest.raises(agent_wake.ResurrectTriggerStaleError):
        _guarded_resurrect(aid, trigger)
    assert _row(db_conn, aid)[0] == "terminated"


def test_tripped_recovery_breaker_refuses_the_reaped_crash_trigger(
    db_conn: psycopg.Connection,
) -> None:
    """After consecutive permanent provider rejections the automatic trigger is
    refused at the final CAS; the durable streak refuses even with no
    suppression window set (a claim clears the window by design)."""
    aid, trigger = _reaped_crash_park(db_conn)
    db_conn.execute("UPDATE agents_meta SET permanent_reject_streak = 2 WHERE id = %s", (aid,))
    db_conn.commit()

    with pytest.raises(agent_wake.ResurrectTriggerStaleError):
        _guarded_resurrect(aid, trigger)
    assert _row(db_conn, aid)[0] == "terminated"


def test_reaped_crash_row_keeps_the_failed_restart_fence(
    db_conn: psycopg.Connection,
) -> None:
    """A failed-restart target keeps its hard fence even for a reaped crash
    row: the relaunch observation must settle first."""
    aid, trigger = _reaped_crash_park(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET runtime_generation = gen_random_uuid(), "
            "runtime_owner = gen_random_uuid() WHERE id = %s",
            (aid,),
        )
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, status, "
            "target_generation, target_owner, claimed_at, applied_at, payload) "
            "SELECT id, '', 'restart', 'system', 'done', runtime_generation, "
            "runtime_owner, now(), now(), "
            '\'{"lifecycle_result": {"outcome": "failed", '
            '"reason": "restart_deadline_expired"}}\'::jsonb '
            "FROM agents_meta WHERE id = %s",
            (aid,),
        )
    db_conn.commit()

    with pytest.raises(agent_wake.ResurrectTriggerStaleError):
        _guarded_resurrect(aid, trigger)
    assert _row(db_conn, aid)[0] == "terminated"


def test_reaped_crash_row_keeps_the_auto_resurrect_budget(
    db_conn: psycopg.Connection,
) -> None:
    """The relaxed fence is not a budget bypass: an exhausted auto-resurrect
    budget refuses even a reaper-marked leftover."""
    from shared.agents import ResurrectBudgetExhausted
    from shared.db import insert_inbound_message

    aid, trigger = _reaped_crash_park(db_conn)
    for _ in range(agent_wake._auto_resurrect_max_attempts()):
        insert_inbound_message(db_conn, aid, "", "system", kind="resurrect")

    with pytest.raises(ResurrectBudgetExhausted):
        _guarded_resurrect(aid, trigger)
    assert _row(db_conn, aid)[0] == "terminated"


def test_manual_resurrect_stays_exempt_from_the_gates(
    db_conn: psycopg.Connection, wakes: list[tuple[int, str]]
) -> None:
    """A manual resurrect passes no trigger: the explicit human override
    bypasses both suppression and the tripped breaker (the breaker record —
    streak and suppression reason — is retained, auditable)."""
    aid = _park(db_conn, status="terminated")
    db_conn.execute(
        "UPDATE agents_meta SET wake_suppressed_until = now() + interval '300 days', "
        "wake_suppress_reason = 'permanent_provider_reject', permanent_reject_streak = 2 "
        "WHERE id = %s",
        (aid,),
    )
    db_conn.commit()

    assert agent_wake.resurrect_agent(aid, resurrected_by="user") == aid
    assert _row(db_conn, aid) == ("idling", None)
    assert wakes == [(aid, "0")]
    row = db_conn.execute(
        "SELECT permanent_reject_streak, wake_suppress_reason FROM agents_meta WHERE id = %s",
        (aid,),
    ).fetchone()
    assert row == (2, "permanent_provider_reject")
