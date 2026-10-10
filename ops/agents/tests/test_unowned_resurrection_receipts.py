"""Live-incarnation and maintenance boundaries for unowned resurrection receipts."""

from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from base.agents.incarnation.resources import ResourceEvidenceError
from base.cluster.machine import machine_name
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.deploy.maintenance import cohort, pause_owner
from base.deploy.maintenance.state import MaintenancePhase
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from ops.agents import wake
from ops.agents.resurrection_retry import ResurrectSettlementDeferredError
from ops.agents.spawn import create_agent_row
from tests.components.ops.test_resurrection_admission import (
    _admitted,
    _force,
    _managed_restarted,
    _resources,
    _restarted,
    _status,
    _unowned_receipt,
)
from tests.components.ops.test_resurrection_admission import wakes as wakes


async def test_a_force_on_a_live_incarnation_records_no_unowned_receipt(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
) -> None:
    """An idle agent its host still owns: the force targets that incarnation,
    and resurrection waits for the original host to observe it, whatever origin
    the row has."""
    aid, _, _, _ = create_agent_row(
        database,
        event_bus,
        spawner="user",
        machine=machine_name(),
        authority=config_authority,
        catalog=model_catalog,
    )
    await _admitted(aops_pool, aid)
    db_conn.execute("UPDATE agents_meta SET status='idling' WHERE id=%s", (aid,))
    db_conn.commit()

    force = _force(aid)

    assert not _unowned_receipt(db_conn, force)
    with pytest.raises(ResurrectSettlementDeferredError):
        wake.resurrect_agent(database, event_bus, aid, resurrected_by="user")
    assert _status(db_conn, aid) == ("terminated", "hosted")


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
) -> None:
    """A resurrected row is the unowned idle row it was before the force, which
    the drain parks; a terminated row carrying a receipt is outside its cohort."""
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")
    resurrected = await _restarted(
        db_conn, aops_pool, config_authority=config_authority, model_catalog=model_catalog
    )
    _force(resurrected)
    wake.resurrect_agent(database, event_bus, resurrected, resurrected_by="user")
    ended, _, _, _ = create_agent_row(
        database,
        event_bus,
        spawner="user",
        machine=machine_name(),
        authority=config_authority,
        catalog=model_catalog,
    )
    assert _unowned_receipt(db_conn, _force(ended))
    when = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)
    holder = "ops:test:unowned"
    pause_owner.begin_maintenance(holder, when)

    hold = cohort.prepare(
        db_conn,
        machine=machine_name(),
        host_owner=None,
        holder=holder,
        acquired_at=when,
    )

    assert hold.phase == MaintenancePhase.DRAINING
    assert hold.parked == (resurrected,)
    assert hold.commands == {}


@pytest.mark.parametrize("invalid", ["unapplied", "failed"])
async def test_unowned_force_cannot_invent_predecessor_restart_closure(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    invalid: str,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
) -> None:
    aid = await _managed_restarted(
        db_conn, aops_pool, config_authority=config_authority, model_catalog=model_catalog
    )
    _force(aid)
    if invalid == "unapplied":
        db_conn.execute(
            "UPDATE inbound_messages SET applied_at=NULL WHERE agent_id=%s AND kind='restart'",
            (aid,),
        )
    else:
        db_conn.execute(
            "UPDATE inbound_messages SET payload=jsonb_set(payload,'{lifecycle_result}',%s) "
            "WHERE agent_id=%s AND kind='restart'",
            (Jsonb({"outcome": "failed", "reason": "restart_deadline_expired"}), aid),
        )
    db_conn.commit()
    wake.resurrect_agent(database, event_bus, aid, resurrected_by="user")
    before = _resources(db_conn, aid)
    with pytest.raises(ResourceEvidenceError, match="predecessor resource/lifecycle closure"):
        await _admitted(aops_pool, aid)
    assert _resources(db_conn, aid) == before
