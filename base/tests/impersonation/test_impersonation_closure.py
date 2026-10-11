"""Closure restore: native-owner restoration, guarded rows, and rollback with an open lease; split from base/tests/impersonation/test_impersonation.py (task #4922)."""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest

from base.agents import impersonation as leases
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.tests.impersonation._impersonation_helpers import _active, _agent, _request
from tests.impersonation_support import attested_caller


@pytest.mark.parametrize("ending", ["released", "expired"])
def test_closure_restores_six_native_owners(
    db_conn: psycopg.Connection,
    ending: str,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:
    from base.agents.impersonation.maintenance import reap_impersonations
    from base.db import pool
    from base.deploy.maintenance.cohort import _classify, _RuntimeRow
    from base.deploy.maintenance.state import MaintenanceHold

    host = uuid4()
    owners: list[int] = []
    for _ in range(6):
        original = _agent(db_conn)
        owner = RuntimeIncarnation(original.agent_id, original.generation, host)
        db_conn.execute(
            "UPDATE agents_meta SET runtime_owner=%s,runtime_kind='hosted' WHERE id=%s",
            (host, owner.agent_id),
        )
        db_conn.commit()
        lease = _active(owner, authority=config_authority, database_gate=database_gate)
        before = db_conn.execute(
            "SELECT runtime_kind,runtime_protocol_version,lease_expires_at FROM agents_meta "
            "WHERE id=%s",
            (owner.agent_id,),
        ).fetchone()
        assert before is not None
        db_conn.execute(
            "UPDATE agents_meta SET runtime_generation=%s,runtime_owner=%s WHERE id=%s",
            (uuid4(), uuid4(), owner.agent_id),
        )
        db_conn.commit()
        if ending == "released":
            leases.release(database, event_bus, lease["id"], attested_caller(lease), "Complete")
            leases.release(
                database, event_bus, lease["id"], attested_caller(lease), "Complete again"
            )
        else:
            db_conn.execute(
                "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
                "WHERE id=%s",
                (lease["id"],),
            )
            db_conn.commit()
            with pool(max_size=2, gate=database_gate) as reaper_pool:
                assert reap_impersonations(reaper_pool, database, event_bus) == 1
                assert reap_impersonations(reaper_pool, database, event_bus) == 0
        assert db_conn.execute(
            "SELECT runtime_generation,runtime_owner,runtime_kind,runtime_protocol_version,"
            "lease_expires_at FROM agents_meta WHERE id=%s",
            (owner.agent_id,),
        ).fetchone() == (owner.generation, host, *before)
        db_conn.commit()
        owners.append(owner.agent_id)
    rows = db_conn.execute(
        "SELECT id,status,runtime_kind,runtime_owner,runtime_generation,"
        "lease_expires_at>clock_timestamp(),pid,incarnation_resources FROM agents_meta "
        "WHERE id=ANY(%s)",
        (owners,),
    ).fetchall()
    hold = _classify([_RuntimeRow(*row) for row in rows], MaintenanceHold(), host, set())
    assert set(hold.commands) == set(owners)


@pytest.mark.parametrize("guard", ["consistent", "unowned", "terminated", "machine", "requested"])
def test_closure_preserves_guarded_rows(
    db_conn: psycopg.Connection,
    guard: str,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:
    owner = _agent(db_conn)
    lease = (
        _request(owner, authority=config_authority, database_gate=database_gate)
        if guard == "requested"
        else _active(owner, authority=config_authority, database_gate=database_gate)
    )
    foreign = (uuid4(), uuid4())
    if guard != "consistent":
        db_conn.execute(
            "UPDATE agents_meta SET runtime_generation=%s,runtime_owner=%s WHERE id=%s",
            (*foreign, owner.agent_id),
        )
    if guard == "unowned":
        db_conn.execute(
            "UPDATE agents_meta SET runtime_owner=NULL,runtime_generation=NULL WHERE id=%s",
            (owner.agent_id,),
        )
    elif guard == "terminated":
        db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (owner.agent_id,))
    elif guard == "machine":
        db_conn.execute("UPDATE agents_meta SET machine='other' WHERE id=%s", (owner.agent_id,))
    expected = (
        (None, None)
        if guard == "unowned"
        else (owner.generation, owner.owner)
        if guard == "consistent"
        else foreign
    )
    assert (
        db_conn.execute(
            "SELECT runtime_generation,runtime_owner FROM agents_meta WHERE id=%s",
            (owner.agent_id,),
        ).fetchone()
        == expected
    )
    before = db_conn.execute(
        "SELECT xmin::text,ctid::text,* FROM agents_meta WHERE id=%s",
        (owner.agent_id,),
    ).fetchone()
    for _ in range(2):
        db_conn.execute(
            "UPDATE agent_impersonations SET status='expired' WHERE id=%s",
            (lease["id"],),
        )
        assert (
            db_conn.execute(
                "SELECT xmin::text,ctid::text,* FROM agents_meta WHERE id=%s",
                (owner.agent_id,),
            ).fetchone()
            == before
        )


@pytest.mark.parametrize("lease_status", ["accepted", "active"])
def test_closure_restore_rolls_back_with_lease(
    db_conn: psycopg.Connection,
    lease_status: str,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner, authority=config_authority, database_gate=database_gate)
    leases.accept(database, event_bus, lease["id"], owner.agent_id, owner, "Handoff brief")
    if lease_status == "active":
        leases.activate(database, event_bus, lease["id"], owner)
    foreign = (uuid4(), uuid4())
    db_conn.execute(
        "UPDATE agents_meta SET status='running',runtime_generation=%s,runtime_owner=%s WHERE id=%s",
        (*foreign, owner.agent_id),
    )
    db_conn.commit()
    db_conn.execute("UPDATE agent_impersonations SET status='expired' WHERE id=%s", (lease["id"],))
    assert db_conn.execute(
        "SELECT runtime_generation,runtime_owner FROM agents_meta WHERE id=%s", (owner.agent_id,)
    ).fetchone() == (owner.generation, owner.owner)
    db_conn.rollback()
    assert (
        db_conn.execute(
            "SELECT runtime_generation,runtime_owner FROM agents_meta WHERE id=%s",
            (owner.agent_id,),
        ).fetchone()
        == foreign
    )
    assert db_conn.execute(
        "SELECT status FROM agent_impersonations WHERE id=%s", (lease["id"],)
    ).fetchone() == (lease_status,)
