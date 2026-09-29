"""`shared.cluster_pin` — the legacy pinned commit (`cluster_pin.target_sha`).

Real-DB tests (the pin IS a Postgres singleton row). No current lifecycle
writes it; the tests seed the historical value with SQL and verify the one
remaining read."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from shared.cluster_pin import get_cluster_target_sha


@pytest.fixture(autouse=True)
def _clear_pin(db_conn: psycopg.Connection) -> Iterator[None]:
    """Reset the singleton pin to NULL before + after each test — cluster_pin is
    infra, not in the conftest TRUNCATE list, so this module self-manages it (same
    pattern as tests/shared/test_cluster_lock.py)."""

    def _clear() -> None:
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE cluster_pin SET target_sha=NULL, updated_at=NULL, updated_by=NULL, "
                "last_known_good_sha=NULL, last_known_good_at=NULL, "
                "pending_known_good_sha=NULL, pending_known_good_at=NULL WHERE id=1"
            )
        db_conn.commit()

    _clear()
    yield
    _clear()


def _seed(db_conn: psycopg.Connection, sha: str) -> None:
    with db_conn.cursor() as cur:
        cur.execute("UPDATE cluster_pin SET target_sha=%s WHERE id=1", (sha,))
    db_conn.commit()


def test_get_returns_none_when_unset() -> None:
    assert get_cluster_target_sha() is None


def test_get_reads_the_historical_pin(db_conn: psycopg.Connection) -> None:
    _seed(db_conn, "abc1234")
    assert get_cluster_target_sha() == "abc1234"


def test_get_reuses_supplied_connection(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db_conn, "abc1234")

    def _unexpected_connect(**_kwargs: object) -> None:
        raise AssertionError("pin read opened a second connection")

    monkeypatch.setattr("shared.db.connect", _unexpected_connect)
    assert get_cluster_target_sha(conn=db_conn) == "abc1234"


def test_get_raises_when_singleton_row_missing(db_conn: psycopg.Connection) -> None:
    """A *missing* row (vs target_sha NULL) is an invariant breach — get raises
    rather than masquerading as an unset pin. Restored by the _clear_pin fixture's
    teardown."""
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM cluster_pin WHERE id=1")
    db_conn.commit()
    try:
        with pytest.raises(RuntimeError, match="singleton row missing"):
            get_cluster_target_sha()
    finally:
        with db_conn.cursor() as cur:
            cur.execute("INSERT INTO cluster_pin (id, target_sha) VALUES (1, NULL)")
        db_conn.commit()
