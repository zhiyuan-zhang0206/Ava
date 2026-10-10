"""Legacy and partial runtime identities need this lifetime's actual closure receipt."""

from typing import Protocol

import psycopg
import pytest

from base.agents import ResurrectRefused
from base.cluster.machine import machine_name
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from ops.agents import wake
from ops.agents.spawn import create_agent_row
from tests.components.ops.resurrection_support import (
    force,
    legacy_row,
    status,
    terminated,
    unowned_receipt,
)
from tests.components.ops.test_resurrection_admission import wakes as wakes


class _LegacyArrange(Protocol):
    def __call__(
        self,
        conn: psycopg.Connection,
        /,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> int: ...


def _legacy_unowned_forced(
    db: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> int:
    """Unowned, but no lifecycle of this runtime left it so."""
    aid = legacy_row(
        db,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
    )
    force(aid)
    return aid


def _legacy_forced_beside_a_marked_neighbour(
    db: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> int:
    """As above while another agent carries both facts: a lifecycle release
    (its resurrection) and an unowned termination receipt (the force before
    it). Both facts are per agent, so a neighbour's prove nothing here."""
    other, _, _, _ = create_agent_row(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        spawner="user",
        machine=machine_name(),
        authority=config_authority,
        catalog=model_catalog,
    )
    db.commit()
    assert unowned_receipt(db, force(other))
    wake.resurrect_agent(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        other,
        resurrected_by="user",
    )
    released = db.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='resurrect' "
        "AND payload->'lifecycle_release' = 'true'::jsonb",
        (other,),
    ).fetchone()
    db.commit()
    assert released == (1,)
    return _legacy_unowned_forced(
        db,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
    )


def _legacy_termination_swept(
    db: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> int:
    """Terminated with no identity and no receipt, as every row the cutover
    inherits: a later force (a machine-pause sweep) finds it terminated already
    and records nothing, whatever the row's origin."""
    aid = terminated(
        db,
        None,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
    )
    force(aid)
    return aid


def _partial_identity_forced(
    db: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> int:
    """A born row whose identity is not empty: a historical process kind."""
    aid, _, _, _ = create_agent_row(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        spawner="user",
        machine=machine_name(),
        authority=config_authority,
        catalog=model_catalog,
    )
    db.execute("UPDATE agents_meta SET runtime_kind='process' WHERE id=%s", (aid,))
    db.commit()
    force(aid)
    return aid


def _earlier_life_receipt(
    db: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> int:
    """A receipt ended an earlier life; this life ended without one."""
    aid, _, _, _ = create_agent_row(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        spawner="user",
        machine=machine_name(),
        authority=config_authority,
        catalog=model_catalog,
    )
    force(aid)
    wake.resurrect_agent(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        aid,
        resurrected_by="user",
    )
    # This later life has an unknown allocation; its earlier force receipt cannot close it.
    db.execute(
        "UPDATE agents_meta SET status='terminated',termination_source='user', "
        "incarnation_resources=NULL WHERE id=%s",
        (aid,),
    )
    db.commit()
    return aid


@pytest.mark.parametrize(
    "arrange",
    [
        _legacy_unowned_forced,
        _legacy_forced_beside_a_marked_neighbour,
        _legacy_termination_swept,
        _partial_identity_forced,
        _earlier_life_receipt,
    ],
)
def test_an_unowned_end_without_this_lifes_receipt_still_refuses(
    db_conn: psycopg.Connection,
    wakes: list[tuple[int, str]],
    arrange: _LegacyArrange,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    aid = arrange(
        db_conn,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
    )
    receipts = db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='terminate' "
        "AND id > COALESCE((SELECT last_resurrect_inbound_id FROM agents_meta WHERE id=%s), 0) "
        "AND payload ? 'unowned_termination'",
        (aid, aid),
    ).fetchone()
    db_conn.commit()
    assert receipts == (0,)
    wakes.clear()

    with pytest.raises(ResurrectRefused, match="runtime_cutover_required"):
        wake.resurrect_agent(database, event_bus, aid, resurrected_by="user")

    assert status(db_conn, aid)[0] == "terminated"
    assert wakes == []
