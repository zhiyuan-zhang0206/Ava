from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from base.agents.birth.spawn import create_agent_row
from base.agents.incarnation.resources import IncarnationResources, decode_resources
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.deploy.maintenance import cohort, pause_owner
from base.deploy.maintenance.state import MaintenancePhase
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from ops.agents import wake
from ops.agents.resurrection_retry import ResurrectSettlementDeferredError
from services.agent_runner.agent_host.tests.lifecycle.resurrection.resurrection_support import (
    force,
    status,
    unowned_receipt,
)
from services.agent_runner.agent_host.tests.lifecycle.resurrection.test_resurrection_admission import (
    admit_hosted_incarnation,
    managed_restarted,
    resources,
    restarted,
)
from services.agent_runner.agent_host.tests.lifecycle.resurrection.test_resurrection_admission import (
    wakes as wakes,
)


async def test_a_force_on_a_live_incarnation_records_no_unowned_receipt(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    aid, _, _, _ = create_agent_row(
        database,
        event_bus,
        spawner="user",
        machine=machine_identity.name(),
        authority=config_authority,
        catalog=model_catalog,
    )
    await admit_hosted_incarnation(
        aops_pool, aid, database_gate=database_gate, machine_identity=machine_identity
    )
    db_conn.execute("UPDATE agents_meta SET status='idling' WHERE id=%s", (aid,))
    db_conn.commit()

    force_receipt = force(aid)

    assert not unowned_receipt(db_conn, force_receipt)
    with pytest.raises(ResurrectSettlementDeferredError):
        wake.resurrect_agent(
            database, event_bus, aid, resurrected_by="user", local_machine=machine_identity.name()
        )
    assert status(db_conn, aid) == ("terminated", "hosted")


async def test_maintenance_parks_a_resurrected_unowned_row_and_ignores_its_receipt(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")
    resurrected = await restarted(
        db_conn,
        aops_pool,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    force(resurrected)
    wake.resurrect_agent(
        database,
        event_bus,
        resurrected,
        resurrected_by="user",
        local_machine=machine_identity.name(),
    )
    ended, _, _, _ = create_agent_row(
        database,
        event_bus,
        spawner="user",
        machine=machine_identity.name(),
        authority=config_authority,
        catalog=model_catalog,
    )
    assert unowned_receipt(db_conn, force(ended))
    when = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)
    holder = "ops:test:unowned"
    pause_owner.begin_maintenance(holder, when)

    hold = cohort.prepare(
        db_conn,
        machine=machine_identity.name(),
        host_owner=None,
        holder=holder,
        acquired_at=when,
    )

    assert hold.phase == MaintenancePhase.DRAINING
    assert hold.parked == (resurrected,)
    assert hold.commands == {}


@pytest.mark.parametrize("invalid", ["unapplied", "other_result"])
async def test_released_resources_do_not_depend_on_superseded_restart_history(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    invalid: str,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    aid = await managed_restarted(
        db_conn,
        aops_pool,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    force(aid)
    if invalid == "unapplied":
        db_conn.execute(
            "UPDATE inbound_messages SET applied_at=NULL WHERE agent_id=%s AND kind='restart'",
            (aid,),
        )
    else:
        db_conn.execute(
            "UPDATE inbound_messages SET payload=jsonb_set(payload,'{lifecycle_result}',%s) "
            "WHERE agent_id=%s AND kind='restart'",
            (Jsonb({"outcome": "superseded", "reason": "target_replaced"}), aid),
        )
    db_conn.commit()
    wake.resurrect_agent(
        database, event_bus, aid, resurrected_by="user", local_machine=machine_identity.name()
    )
    admitted = await admit_hosted_incarnation(
        aops_pool, aid, database_gate=database_gate, machine_identity=machine_identity
    )
    state = decode_resources(resources(db_conn, aid))
    assert isinstance(state, IncarnationResources)
    assert (state.generation, state.owner) == (admitted.generation, admitted.owner)
