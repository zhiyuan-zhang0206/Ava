"""shared.host_deploy_state — posture/updater-lease row (R1, Task #1021).

Covers the R1 host-level explicit model: the posture transitions the pause
lifecycle drives (idle -> paused -> idle) and the retained updater-lease
liveness reader. Nothing writes the lease any more, so its rows are raw SQL
fixtures shaped like what the retired updater left behind.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from shared import host_deploy_state as hds


@pytest.fixture(autouse=True)
def _clean_row(db_conn: psycopg.Connection) -> Iterator[None]:
    """host_deploy_state is infra (not in the conftest TRUNCATE list) — this
    module self-manages its row the way test_cluster_lock.py manages the
    singleton lease row."""
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM host_deploy_state WHERE machine = %s", (_machine(),))
    db_conn.commit()
    yield
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM host_deploy_state WHERE machine = %s", (_machine(),))
    db_conn.commit()


def _machine() -> str:
    from shared.machine import machine_name

    return machine_name()


def _retained_lease(db_conn: psycopg.Connection, *, expires_in_s: float) -> None:
    """A converging row carrying an updater lease, as the retired updater wrote it."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO host_deploy_state (machine, posture, updater_lease_expires_at) "
            "VALUES (%s, 'converging', now() + make_interval(secs => %s)) "
            "ON CONFLICT (machine) DO UPDATE SET posture = EXCLUDED.posture, "
            "updater_lease_expires_at = EXCLUDED.updater_lease_expires_at",
            (_machine(), expires_in_s),
        )
    db_conn.commit()


def test_no_row_reads_as_none() -> None:
    assert hds.read() is None


def test_read_uses_the_callers_connection(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundled snapshot can read the row without opening or owning another connection."""
    hds.set_posture("paused")

    def _fresh_connect(**_kwargs: object) -> object:
        raise AssertionError("read opened a fresh connection")

    monkeypatch.setattr("shared.db.connect", _fresh_connect)

    state = hds.read(conn=db_conn)

    assert state is not None
    assert state.posture == "paused"


def test_invalid_posture_is_rejected() -> None:
    with pytest.raises(ValueError):
        hds.set_posture("bogus")


def test_retained_lease_reads_live_until_it_expires(db_conn: psycopg.Connection) -> None:
    _retained_lease(db_conn, expires_in_s=600)
    state = hds.read()
    assert state is not None
    assert state.posture == "converging"
    assert state.updater_live is True
    assert hds.updater_lease_live() is True


def test_expired_lease_reads_as_not_live(db_conn: psycopg.Connection) -> None:
    _retained_lease(db_conn, expires_in_s=-10)
    assert hds.updater_lease_live() is False


def test_no_row_has_no_live_lease() -> None:
    assert hds.updater_lease_live() is False


def test_set_posture_paused_stamps_paused_at() -> None:
    """The pause window's anchor (R1 PR5): entering `paused` stamps the moment,
    exactly where the retired `cluster_paused` file's mtime used to be."""
    hds.set_posture("paused")
    state = hds.read()
    assert state is not None
    assert state.paused_at is not None


def test_converging_preserves_paused_at() -> None:
    """Transitions INSIDE the window must not move the anchor: `updated_at` is
    bumped by `converging`, `paused_at` is the pause moment and stays."""
    hds.set_posture("paused")
    first = hds.read()
    assert first is not None and first.paused_at is not None

    hds.set_posture("converging")
    state = hds.read()
    assert state is not None
    assert state.posture == "converging"
    assert state.paused_at == first.paused_at


def test_set_posture_idle_clears_paused_at() -> None:
    """Returning to serving clears the anchor: a host that is not paused has no
    pause window for the updater-outcome reader to anchor on."""
    hds.set_posture("paused")
    hds.set_posture("idle")
    state = hds.read()
    assert state is not None
    assert state.posture == "idle"
    assert state.paused_at is None


def test_repause_refreshes_paused_at() -> None:
    """A second pause is a NEW window: the anchor must move to the new pause, not
    keep the old one (a fresh update's logs must not be dated against the
    previous pause)."""
    hds.set_posture("paused")
    first = hds.read()
    assert first is not None and first.paused_at is not None
    hds.set_posture("idle")
    hds.set_posture("paused")
    second = hds.read()
    assert second is not None and second.paused_at is not None
    assert second.paused_at > first.paused_at


def test_set_posture_preserves_updater_lease(db_conn: psycopg.Connection) -> None:
    """Posture and the updater lease are orthogonal (audit 2026-08-08 P2): a
    pause/unpause must not clear a retained updater's liveness claim."""
    _retained_lease(db_conn, expires_in_s=600)

    hds.set_posture("paused")
    state = hds.read()
    assert state is not None
    assert state.posture == "paused"
    assert state.updater_live, "set_posture must not clear the updater lease"

    hds.set_posture("idle")
    state = hds.read()
    assert state is not None
    assert state.updater_live, "unpause must not clear the updater lease either"
