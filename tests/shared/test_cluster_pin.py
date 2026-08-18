"""`shared.cluster_pin` — the cluster's pinned commit (`cluster_target_sha`).

Real-DB tests (the pin IS a Postgres singleton row). Verifies set writes the SHA,
get reads it back, overwrite replaces, and get returns None when unset. The
gateway writes it after a rollout; `ava status` reads it to surface drift."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from shared.cluster_pin import (
    get_cluster_target_sha,
    get_last_known_good_sha,
    seed_last_known_good_sha_if_null,
    set_cluster_target_sha,
)
from shared.config import settings


@pytest.fixture(autouse=True)
def _clear_pin(db_conn: psycopg.Connection) -> Iterator[None]:
    """Reset the singleton pin to NULL before + after each test — cluster_pin is
    infra, not in the conftest TRUNCATE list, so this module self-manages it (same
    pattern as tests/shared/test_cluster_lock.py)."""

    def _clear() -> None:
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE cluster_pin SET target_sha=NULL, updated_at=NULL, updated_by=NULL, "
                "last_known_good_sha=NULL, last_known_good_at=NULL WHERE id=1"
            )
        db_conn.commit()

    _clear()
    yield
    _clear()


def test_get_returns_none_when_unset() -> None:
    assert get_cluster_target_sha() is None


def test_set_then_get_roundtrips() -> None:
    set_cluster_target_sha("abc1234", set_by="cloud:pid1")
    assert get_cluster_target_sha() == "abc1234"


def test_set_overwrites_previous_pin() -> None:
    set_cluster_target_sha("aaaaaaa")
    set_cluster_target_sha("bbbbbbb")
    assert get_cluster_target_sha() == "bbbbbbb"


def test_set_records_provenance() -> None:
    """`updated_by` + `updated_at` are recorded for ops (who pinned it, when)."""
    set_cluster_target_sha("abc1234", set_by="cloud:pid42")
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT updated_by, updated_at FROM cluster_pin WHERE id=1")
        row = cur.fetchone()
        assert row[0] == "cloud:pid42"  # type: ignore[index]
        assert row[1] is not None  # type: ignore[index]  # updated_at stamped


def test_seed_sets_when_null() -> None:
    """NULL last_known_good -> seeded to the passed sha, returns True."""
    assert get_last_known_good_sha() is None
    assert seed_last_known_good_sha_if_null("headsha1", set_by="seed-on-first-start") is True
    assert get_last_known_good_sha() == "headsha1"


def test_seed_is_noop_when_already_set() -> None:
    """An existing known-good is never overwritten; the second seed returns False."""
    assert seed_last_known_good_sha_if_null("first") is True
    assert seed_last_known_good_sha_if_null("second") is False
    assert get_last_known_good_sha() == "first"


def test_seed_records_provenance() -> None:
    """`set_by` is stored as the provenance + `last_known_good_at` is stamped.
    The historical append form (`COALESCE(updated_by,'') || ' last_known_good_seed=...'`)
    grew the TEXT column without bound — one line per rollout + manual set — so
    provenance is now last-writer-wins (audit 2026-08-08 P2)."""
    seed_last_known_good_sha_if_null("headsha2", set_by="seed-on-first-start")
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT updated_by, last_known_good_at FROM cluster_pin WHERE id=1")
        row = cur.fetchone()
        assert row[0] == "seed-on-first-start"  # type: ignore[index]  # last writer wins
        assert row[1] is not None  # type: ignore[index]  # last_known_good_at stamped


def test_seed_raises_when_singleton_row_missing(db_conn: psycopg.Connection) -> None:
    """A *missing* row is an invariant breach — seed raises rather than silently
    treating it as an unset pin. Restored by the _clear_pin teardown."""
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM cluster_pin WHERE id=1")
    db_conn.commit()
    try:
        with pytest.raises(RuntimeError, match="singleton row missing"):
            seed_last_known_good_sha_if_null("headsha3")
    finally:
        with db_conn.cursor() as cur:
            cur.execute("INSERT INTO cluster_pin (id, target_sha) VALUES (1, NULL)")
        db_conn.commit()


def test_get_raises_when_singleton_row_missing(db_conn: psycopg.Connection) -> None:
    """A *missing* row (vs target_sha NULL) is an invariant breach — get raises
    rather than masquerading as an unset pin, so a vanished row can't make the
    drift net silently blind. Restored by the _clear_pin fixture's teardown."""
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
