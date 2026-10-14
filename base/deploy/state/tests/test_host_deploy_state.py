"""base.deploy.state.host_deploy_state — the per-host posture row (R1, Task #1021).

Covers the R1 host-level model: the posture transitions the pause lifecycle
drives (idle -> paused -> idle), the reads that project the row, and that a
transition writes the posture and nothing else.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from base import db
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.deploy.state import host_deploy_state as hds


def _db(database_gate: ProcessDbGate) -> Database:
    return Database.from_settings(gate=database_gate)


@pytest.fixture(autouse=True)
def _clean_row(db_conn: psycopg.Connection, *, machine_identity: MachineIdentity) -> Iterator[None]:
    """host_deploy_state is infra (not in the conftest TRUNCATE list) — this
    module self-manages its row."""
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM host_deploy_state WHERE machine = %s", (machine_identity.name(),))
    db_conn.commit()
    yield
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM host_deploy_state WHERE machine = %s", (machine_identity.name(),))
    db_conn.commit()


def test_no_row_reads_as_none(database: Database, *, machine_identity: MachineIdentity) -> None:
    assert hds.read(database, machine=machine_identity.name()) is None


def test_read_uses_the_callers_connection(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """A bundled snapshot can read the row without opening or owning another connection."""
    hds.set_posture(database, "paused", machine=machine_identity.name())

    def _fresh_connect(**_kwargs: object) -> object:
        raise AssertionError("read opened a fresh connection")

    monkeypatch.setattr(db, "connect", _fresh_connect)

    state = hds.read(database, conn=db_conn, machine=machine_identity.name())

    assert state is not None
    assert state.posture == "paused"


@pytest.mark.parametrize("posture", ["bogus", "converging"])
def test_invalid_posture_is_rejected(
    posture: str, database: Database, *, machine_identity: MachineIdentity
) -> None:
    with pytest.raises(ValueError, match="invalid posture"):
        hds.set_posture(database, posture, machine=machine_identity.name())


def test_posture_round_trips_and_stamps_the_database_clock(
    database: Database, *, machine_identity: MachineIdentity
) -> None:
    hds.set_posture(database, "paused", machine=machine_identity.name())
    paused = hds.read(database, machine=machine_identity.name())
    assert paused is not None
    assert paused.posture == "paused"
    assert paused.updated_at <= paused.db_now

    hds.set_posture(database, "idle", machine=machine_identity.name())
    idle = hds.read(database, machine=machine_identity.name())
    assert idle is not None
    assert idle.posture == "idle"
    assert idle.updated_at >= paused.updated_at


def test_read_all_returns_every_machines_row(
    database: Database,
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentity,
) -> None:
    hds.set_posture(database, "paused", machine=machine_identity.name())
    rows = hds.read_all(_db(database_gate=database_gate))
    assert rows[machine_identity.name()].posture == "paused"
