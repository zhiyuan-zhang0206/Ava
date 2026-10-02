"""base.deploy.state.host_deploy_state — the per-host posture row (R1, Task #1021).

Covers the R1 host-level model: the posture transitions the pause lifecycle
drives (idle -> paused -> idle), the reads that project the row, and that a
transition writes the posture and nothing else.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from base.deploy.state import host_deploy_state as hds


@pytest.fixture(autouse=True)
def _clean_row(db_conn: psycopg.Connection) -> Iterator[None]:
    """host_deploy_state is infra (not in the conftest TRUNCATE list) — this
    module self-manages its row."""
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM host_deploy_state WHERE machine = %s", (_machine(),))
    db_conn.commit()
    yield
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM host_deploy_state WHERE machine = %s", (_machine(),))
    db_conn.commit()


def _machine() -> str:
    from base.cluster.machine import machine_name

    return machine_name()


def test_no_row_reads_as_none() -> None:
    assert hds.read() is None


def test_read_uses_the_callers_connection(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundled snapshot can read the row without opening or owning another connection."""
    hds.set_posture("paused")

    def _fresh_connect(**_kwargs: object) -> object:
        raise AssertionError("read opened a fresh connection")

    monkeypatch.setattr("base.db.connect", _fresh_connect)

    state = hds.read(conn=db_conn)

    assert state is not None
    assert state.posture == "paused"


@pytest.mark.parametrize("posture", ["bogus", "converging"])
def test_invalid_posture_is_rejected(posture: str) -> None:
    with pytest.raises(ValueError, match="invalid posture"):
        hds.set_posture(posture)


def test_posture_round_trips_and_stamps_the_database_clock() -> None:
    hds.set_posture("paused")
    paused = hds.read()
    assert paused is not None
    assert paused.posture == "paused"
    assert paused.updated_at <= paused.db_now

    hds.set_posture("idle")
    idle = hds.read()
    assert idle is not None
    assert idle.posture == "idle"
    assert idle.updated_at >= paused.updated_at


def test_read_all_returns_every_machines_row() -> None:
    hds.set_posture("paused")
    rows = hds.read_all()
    assert rows[_machine()].posture == "paused"
