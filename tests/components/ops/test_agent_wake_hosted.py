"""Real database resurrection: guarded status transition, durable inbound, and one host wake."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from base.agents.messages.inbound import InboundKind
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from ops.agents import wake
from ops.agents.spawn import create_agent_row


@pytest.fixture
def wakes() -> Iterator[list[tuple[int, str]]]:
    captured: list[tuple[int, str]] = []
    yield captured


@pytest.fixture(autouse=True)
def _capture_wakes(monkeypatch: pytest.MonkeyPatch, wakes: list[tuple[int, str]]) -> Iterator[None]:
    def _record(_db: object, _bus: object, agent_id: int, payload: str) -> None:
        wakes.append((agent_id, payload))

    # Both wake modules publish through their own namespace (the split moved
    # swap-in / revive to `ops.agent_revive`), so capture both.
    monkeypatch.setattr(wake, "publish_inbound_wake", _record)
    yield


def _park(
    db: psycopg.Connection,
    *,
    status: str,
    pid: int | None = None,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> int:
    """Seed admission input or a terminated hosted incarnation without launching."""
    aid, _birth, _prompt_id, _attempt_id = create_agent_row(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        spawner="user",
        machine=machine_identity.name(),
        authority=config_authority,
        catalog=model_catalog,
    )
    with db.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status=%s, pid=%s WHERE id=%s", (status, pid, aid))
        if status == "terminated":
            cur.execute(
                "UPDATE agents_meta SET runtime_kind='hosted', "
                "runtime_generation=gen_random_uuid(), runtime_owner=gen_random_uuid(), "
                "incarnation_resources=NULL "
                "WHERE id=%s",
                (aid,),
            )
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
    db_conn: psycopg.Connection,
    wakes: list[tuple[int, str]],
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """terminated -> idling + resurrect inbound + one wake; no launch, no
    pid-confirm polling."""
    aid = _park(
        db_conn,
        status="terminated",
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    out = wake.resurrect_agent(
        database, event_bus, aid, resurrected_by="user", local_machine=machine_identity.name()
    )
    assert out == aid
    assert _row(db_conn, aid) == ("idling", None)
    assert _kind_rows(db_conn, aid, "resurrect") == 1
    assert wakes == [(aid, "0")]


def test_resurrect_agent_hosted_clears_the_corpse_marker(
    db_conn: psycopg.Connection,
    wakes: list[tuple[int, str]],
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """A reaper-terminated corpse keeps `last_turn_fatal_at` stamped; the
    resurrect transition must clear it or the reaper's next beat would
    re-terminate the freshly revived row (the marker is already past grace)."""
    aid = _park(
        db_conn,
        status="terminated",
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    db_conn.execute(
        "UPDATE agents_meta SET termination_source='reaper', "
        "last_turn_fatal_at = now() - interval '1 hour' WHERE id = %s",
        (aid,),
    )
    db_conn.commit()
    wake.resurrect_agent(
        database, event_bus, aid, resurrected_by="user", local_machine=machine_identity.name()
    )
    assert db_conn.execute(
        "SELECT last_turn_fatal_at FROM agents_meta WHERE id = %s", (aid,)
    ).fetchone() == (None,)


def test_resurrect_agent_hosted_keeps_trigger_guard(
    db_conn: psycopg.Connection,
    wakes: list[tuple[int, str]],
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """The auto-resurrect trigger CAS semantics are mode-independent: a stale
    trigger still refuses the transition."""
    aid = _park(
        db_conn,
        status="terminated",
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    with pytest.raises(wake.ResurrectTriggerStaleError):
        wake.resurrect_agent(
            database,
            event_bus,
            aid,
            resurrected_by="system",
            trigger_inbound_id=999999,
            trigger_inbound_kind=InboundKind.CHAT,
            local_machine=machine_identity.name(),
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
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """An observed termination never revalidates its original runtime token."""
    from uuid import uuid4

    from psycopg.types.json import Jsonb

    from agent.db import claim_inbound_batch
    from agent.ownership.hosted import admit_hosted_runtime, apply_hosted_lifecycle
    from agent.ownership.inbound import RuntimeOwnershipLostError
    from base.agents.incarnation.resources import ResourceBirth
    from base.db import insert_inbound_message

    aid = _park(
        db_conn,
        status="idling",
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    if managed:
        db_conn.execute(
            "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
            (Jsonb(ResourceBirth(birth=uuid4()).model_dump(mode="json")), aid),
        )
        db_conn.commit()
    owner = uuid4()
    old = await admit_hosted_runtime(
        aops_pool,
        aid,
        machine_identity.name(),
        owner,
        expected_from="idling",
        db=database,
    )
    assert old is not None
    command = insert_inbound_message(
        db_conn, aid, "", "self", kind="terminate", bus=event_bus, database=database
    )
    assert [
        item.id for item in await claim_inbound_batch(aops_pool, aid, incarnation=old, work=None)
    ] == [command]
    assert (
        await apply_hosted_lifecycle(aops_pool, old, bus=event_bus, resources=None) == "terminate"
    )
    trigger = insert_inbound_message(
        db_conn, aid, "continue", "user", bus=event_bus, database=database
    )
    wake.resurrect_agent(
        database,
        event_bus,
        aid,
        resurrected_by="system" if guarded else "user",
        trigger_inbound_id=trigger if guarded else None,
        trigger_inbound_kind=InboundKind.CHAT if guarded else None,
        local_machine=machine_identity.name(),
    )
    successor = await admit_hosted_runtime(
        aops_pool,
        aid,
        machine_identity.name(),
        owner,
        expected_from="idling",
        db=database,
    )
    assert successor is not None and successor.generation != old.generation
    with pytest.raises(RuntimeOwnershipLostError):
        await claim_inbound_batch(aops_pool, aid, incarnation=old, work=None)
    assert {
        item.kind
        for item in await claim_inbound_batch(aops_pool, aid, incarnation=successor, work=None)
    } == {
        "chat",
        "resurrect",
    }


# ── stale trigger guard: system-reaped crash rows ───────────────────────────


def _backdate_before_status(db: psycopg.Connection, aid: int, iid: int) -> None:
    with db.cursor() as cur:
        cur.execute(
            "UPDATE inbound_messages SET created_at = "
            "(SELECT status_changed_at FROM agents_meta WHERE id = %s) - interval '1 second' "
            "WHERE id = %s",
            (aid, iid),
        )
    db.commit()


def _reaped_crash_park(
    db: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> tuple[int, int]:
    """terminated + reaper source + retained crash marker + a leftover chat
    that predates the termination (a legacy recovery-trigger shape)."""
    from base.db import insert_inbound_message

    aid = _park(
        db,
        status="terminated",
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    trigger = insert_inbound_message(
        db,
        aid,
        "leftover work",
        "user",
        bus=EventBus.from_settings(),
        database=Database.from_settings(gate=database_gate),
    )
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET termination_source = 'reaper', "
            "last_turn_fatal_at = now() - interval '1 hour' WHERE id = %s",
            (aid,),
        )
    db.commit()
    _backdate_before_status(db, aid, trigger)
    return aid, trigger


def _guarded_resurrect(
    aid: int, trigger: int, database_gate: ProcessDbGate, *, machine_identity: MachineIdentity
) -> None:
    wake.resurrect_agent(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        aid,
        resurrected_by="system",
        trigger_inbound_id=trigger,
        trigger_inbound_kind=InboundKind.CHAT,
        local_machine=machine_identity.name(),
    )


def test_reaped_crash_row_refuses_leftover_work(
    db_conn: psycopg.Connection,
    wakes: list[tuple[int, str]],
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """A reaper termination cannot grant old work a fresh activation attempt."""
    from ops.agents.resurrection_retry import ResurrectTriggerStaleError

    aid, trigger = _reaped_crash_park(
        db_conn,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )

    with pytest.raises(ResurrectTriggerStaleError):
        wake.resurrect_agent(
            database,
            event_bus,
            aid,
            resurrected_by="system",
            trigger_inbound_id=trigger,
            trigger_inbound_kind=InboundKind.CHAT,
            local_machine=machine_identity.name(),
        )
    assert _row(db_conn, aid) == ("terminated", None)
    assert wakes == []


def test_operator_death_still_refuses_leftover_work(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """The crash marker alone never relaxes the fence: a user kill keeps its
    contract — the leftover chat cannot undo it."""
    from base.db import insert_inbound_message

    aid = _park(
        db_conn,
        status="terminated",
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    trigger = insert_inbound_message(
        db_conn, aid, "leftover work", "user", bus=event_bus, database=database
    )
    db_conn.execute(
        "UPDATE agents_meta SET termination_source = 'user', "
        "last_turn_fatal_at = now() - interval '1 hour' WHERE id = %s",
        (aid,),
    )
    db_conn.commit()
    _backdate_before_status(db_conn, aid, trigger)

    with pytest.raises(wake.ResurrectTriggerStaleError):
        _guarded_resurrect(
            aid, trigger, database_gate=database_gate, machine_identity=machine_identity
        )
    assert _row(db_conn, aid)[0] == "terminated"


def test_suppressed_wakes_refuse_the_reaped_crash_trigger(
    redis_url: str,
    db_conn: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """The relaxed fence does not bypass an active automatic-wake suppression."""
    aid, trigger = _reaped_crash_park(
        db_conn,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    db_conn.execute(
        "UPDATE agents_meta SET wake_suppressed_until = now() + interval '1 hour', "
        "wake_suppress_reason = 'resurrect_failed' WHERE id = %s",
        (aid,),
    )
    db_conn.commit()

    with pytest.raises(wake.ResurrectTriggerStaleError):
        _guarded_resurrect(
            aid, trigger, database_gate=database_gate, machine_identity=machine_identity
        )
    assert _row(db_conn, aid)[0] == "terminated"


def test_tripped_recovery_breaker_refuses_the_reaped_crash_trigger(
    redis_url: str,
    db_conn: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """After consecutive permanent provider rejections the automatic trigger is
    refused at the final CAS; the durable streak refuses even with no
    suppression window set (a claim clears the window by design)."""
    aid, trigger = _reaped_crash_park(
        db_conn,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    db_conn.execute("UPDATE agents_meta SET permanent_reject_streak = 2 WHERE id = %s", (aid,))
    db_conn.commit()

    with pytest.raises(wake.ResurrectTriggerStaleError):
        _guarded_resurrect(
            aid, trigger, database_gate=database_gate, machine_identity=machine_identity
        )
    assert _row(db_conn, aid)[0] == "terminated"


def test_system_resurrect_is_not_capped_by_unconsumed_resurrect_rows(
    db_conn: psycopg.Connection,
    wakes: list[tuple[int, str]],
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """There is no attempt budget: a new message still resurrects a terminated
    agent however many earlier resurrect rows were never consumed."""
    from base.db import insert_inbound_message

    aid = _park(
        db_conn,
        status="terminated",
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    for _ in range(5):
        insert_inbound_message(
            db_conn, aid, "", "system", kind="resurrect", bus=event_bus, database=database
        )
    trigger = insert_inbound_message(
        db_conn, aid, "continue", "user", bus=event_bus, database=database
    )

    _guarded_resurrect(aid, trigger, database_gate=database_gate, machine_identity=machine_identity)

    assert _row(db_conn, aid) == ("idling", None)
    assert wakes == [(aid, "0")]


def test_manual_resurrect_stays_exempt_from_the_gates(
    db_conn: psycopg.Connection,
    wakes: list[tuple[int, str]],
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """A manual resurrect passes no trigger: the explicit human override
    bypasses both suppression and the tripped breaker (the breaker record —
    streak and suppression reason — is retained, auditable)."""
    aid = _park(
        db_conn,
        status="terminated",
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    db_conn.execute(
        "UPDATE agents_meta SET wake_suppressed_until = now() + interval '300 days', "
        "wake_suppress_reason = 'permanent_provider_reject', permanent_reject_streak = 2 "
        "WHERE id = %s",
        (aid,),
    )
    db_conn.commit()

    assert (
        wake.resurrect_agent(
            database, event_bus, aid, resurrected_by="user", local_machine=machine_identity.name()
        )
        == aid
    )
    assert _row(db_conn, aid) == ("idling", None)
    assert wakes == [(aid, "0")]
    row = db_conn.execute(
        "SELECT permanent_reject_streak, wake_suppress_reason FROM agents_meta WHERE id = %s",
        (aid,),
    ).fetchone()
    assert row == (2, "permanent_provider_reject")


@pytest.mark.parametrize("runtime_kind", [None, "process"])
@pytest.mark.parametrize("has_identity", [False, True])
@pytest.mark.parametrize("guarded", [False, True])
def test_historical_runtime_cannot_be_resurrected(
    db_conn: psycopg.Connection,
    wakes: list[tuple[int, str]],
    runtime_kind: str | None,
    has_identity: bool,
    guarded: bool,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """An absent command pointer cannot adopt a historical or unknown runtime."""
    from base.agents import ResurrectRefused
    from base.db import insert_inbound_message

    aid = _park(
        db_conn,
        status="terminated",
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    db_conn.execute(
        "UPDATE agents_meta SET runtime_kind=%s, incarnation_resources=NULL, "
        "runtime_generation=CASE WHEN %s THEN runtime_generation ELSE NULL END, "
        "runtime_owner=CASE WHEN %s THEN runtime_owner ELSE NULL END WHERE id=%s",
        (runtime_kind, has_identity, has_identity, aid),
    )
    db_conn.commit()
    trigger = insert_inbound_message(
        db_conn, aid, "continue", "user", bus=event_bus, database=database
    )
    query = "SELECT row_to_json(a) FROM agents_meta a WHERE id=%s"
    before = db_conn.execute(query, (aid,)).fetchone()
    db_conn.commit()

    with pytest.raises(ResurrectRefused, match="runtime_cutover_required"):
        wake.resurrect_agent(
            database,
            event_bus,
            aid,
            resurrected_by="system" if guarded else "user",
            trigger_inbound_id=trigger if guarded else None,
            trigger_inbound_kind=InboundKind.CHAT if guarded else None,
            local_machine=machine_identity.name(),
        )

    assert db_conn.execute(query, (aid,)).fetchone() == before
    assert _kind_rows(db_conn, aid, "resurrect") == 0
    assert wakes == []


@pytest.mark.parametrize("missing", ["runtime_generation", "runtime_owner", "pid"])
def test_incomplete_hosted_target_requires_cutover(
    db_conn: psycopg.Connection,
    wakes: list[tuple[int, str]],
    missing: str,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """A hosted label alone cannot replace the retained incarnation authority."""
    from psycopg import sql

    from base.agents import ResurrectRefused

    aid = _park(
        db_conn,
        status="terminated",
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    db_conn.execute(
        sql.SQL("UPDATE agents_meta SET {}=%s WHERE id=%s").format(sql.Identifier(missing)),
        (424243 if missing == "pid" else None, aid),
    )
    db_conn.commit()
    with pytest.raises(ResurrectRefused, match="runtime_cutover_required"):
        wake.resurrect_agent(
            database, event_bus, aid, resurrected_by="user", local_machine=machine_identity.name()
        )
    assert _row(db_conn, aid)[0] == "terminated"
    assert _kind_rows(db_conn, aid, "resurrect") == 0
    assert wakes == []
