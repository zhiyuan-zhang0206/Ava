"""PGVectorBackend tests — real Postgres + pgvector against the session test DB.

`tests/conftest.py` provisions an isolated `ava_test_<pid>_<ts>` database per
session and `shared.db` dials it, so these tests run against the real engine
the backend targets (CI's `install-pg-redis` action installs
`postgresql-17-pgvector`). The backend owns its schema at `connect()` — same
derived-cache philosophy as the milvus collection, no migrations involved —
so the autouse fixture drops the table between tests and `connect()` rebuilds
it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import numpy as np
import psycopg
import pytest
from psycopg import sql as pgsql

from services.memory_indexer.backends.pgvector import _TABLE, PGVectorBackend, _ensure_schema

_DIM = 8
_FP = "test:gemini:dim=8"


def _vec(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(_DIM).astype(np.float32)


@pytest.fixture(autouse=True)
def _fresh_table(db_conn: psycopg.Connection) -> None:
    """Drop the backend's table before each test (autouse = runs first, so
    `connect()` in the backend fixture recreates it). The extension itself
    stays — idempotent and shared with the dim-mismatch test."""
    with db_conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute("DROP TABLE IF EXISTS memory_embeddings")
    db_conn.commit()


@pytest.fixture
def backend() -> Iterator[PGVectorBackend]:
    b = PGVectorBackend(dim=_DIM, fingerprint=_FP)
    b.connect()
    try:
        yield b
    finally:
        b.close()


def _rows(db_conn: psycopg.Connection) -> list[tuple[Any, ...]]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT pk, path, kind, chunk_idx, mtime, content_hash "
            "FROM memory_embeddings ORDER BY pk"
        )
        return list(cur.fetchall())


# ── schema management ─────────────────────────────────────────────────────


def test_connect_creates_extension_and_table(
    db_conn: psycopg.Connection, backend: PGVectorBackend
) -> None:
    """connect() provisions the extension + table at the provider's dim."""
    with db_conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (_TABLE,))
        row = cur.fetchone()
        assert row is not None and row[0] == _TABLE
        cur.execute(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = to_regclass(%s) AND attname = 'vector'",
            (_TABLE,),
        )
        row = cur.fetchone()
        assert row is not None and row[0] == _DIM


def test_connect_drops_and_recreates_on_dim_mismatch(
    db_conn: psycopg.Connection, backend: PGVectorBackend
) -> None:
    """A table at another provider's dim (or missing a column) is a stale cache — dropped and
    recreated at the current dim (cold-start rebuilds the rows)."""
    wrong_dim = _DIM // 4
    with db_conn.cursor() as cur:
        cur.execute("DROP TABLE memory_embeddings")
        cur.execute(
            pgsql.SQL(
                "CREATE TABLE memory_embeddings ("
                "pk VARCHAR(2048) PRIMARY KEY, path VARCHAR(1024) NOT NULL, "
                "kind VARCHAR(16) NOT NULL, chunk_idx BIGINT NOT NULL, "
                "mtime DOUBLE PRECISION NOT NULL, content_hash VARCHAR(128) NOT NULL, "
                "vector vector({dim}) NOT NULL)"
            ).format(dim=pgsql.Literal(wrong_dim))
        )
    db_conn.commit()

    fresh = PGVectorBackend(dim=_DIM, fingerprint=_FP)
    fresh.connect()
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT atttypmod FROM pg_attribute "
                "WHERE attrelid = to_regclass(%s) AND attname = 'vector'",
                (_TABLE,),
            )
            row = cur.fetchone()
            assert row is not None and row[0] == _DIM
    finally:
        fresh.close()


def test_readonly_ensure_schema_refuses_dim_mismatch_without_dropping_rows(
    db_conn: psycopg.Connection, backend: PGVectorBackend
) -> None:
    """D3 regression: a comparison validation cannot rebuild the live table."""
    backend.upsert("/survives.md", 1.0, "hash", _vec(0))

    with pytest.raises(RuntimeError, match="vector dimension 8, expected 9"):
        _ensure_schema(db_conn, _DIM + 1, readonly=True)

    assert _rows(db_conn) == [("/survives.md\x1fbody\x1f0", "/survives.md", "body", 0, 1.0, "hash")]


def test_readonly_connect_forwards_validation_and_closes_pool_on_mismatch(
    db_conn: psycopg.Connection, backend: PGVectorBackend
) -> None:
    """connect() preserves the read-only mismatch guarantee and pool cleanup."""
    backend.upsert("/survives.md", 1.0, "hash", _vec(0))
    readonly_backend = PGVectorBackend(dim=_DIM + 1, fingerprint=_FP, readonly=True)

    with pytest.raises(RuntimeError, match="vector dimension 8, expected 9"):
        readonly_backend.connect()

    assert readonly_backend._pool is None
    assert _rows(db_conn) == [("/survives.md\x1fbody\x1f0", "/survives.md", "body", 0, 1.0, "hash")]


def test_readonly_ensure_schema_accepts_matching_table(
    db_conn: psycopg.Connection, backend: PGVectorBackend
) -> None:
    """Validation reads the current schema without provisioning anything."""
    backend.upsert("/present.md", 1.0, "hash", _vec(0))

    _ensure_schema(db_conn, _DIM, readonly=True)

    assert _rows(db_conn) == [("/present.md\x1fbody\x1f0", "/present.md", "body", 0, 1.0, "hash")]


def test_readonly_ensure_schema_refuses_missing_table(db_conn: psycopg.Connection) -> None:
    """Validation cannot create an absent derived-cache table."""
    with pytest.raises(RuntimeError, match="is missing"):
        _ensure_schema(db_conn, _DIM, readonly=True)

    with db_conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (_TABLE,))
        row = cur.fetchone()
    assert row is not None and row[0] is None


# ── write path (indexer daemon contract) ──────────────────────────────────


def test_upsert_all_meta_roundtrip(db_conn: psycopg.Connection, backend: PGVectorBackend) -> None:
    """Chunk rows land as rows; all_meta aggregates per file (max mtime)."""
    backend.upsert("/a.md", 1.0, "ha", _vec(0), kind="body", chunk_idx=0)
    backend.upsert("/a.md", 1.0, "ha", _vec(1), kind="body", chunk_idx=1)
    backend.upsert("/a.md", 1.0, "ha", _vec(2), kind="desc", chunk_idx=0)
    backend.upsert("/b.md", 2.0, "hb", _vec(3), kind="body", chunk_idx=0)

    assert backend.all_meta() == {"/a.md": (1.0, "ha", _FP), "/b.md": (2.0, "hb", _FP)}
    assert len(_rows(db_conn)) == 4


def test_upsert_overwrite_same_pk(db_conn: psycopg.Connection, backend: PGVectorBackend) -> None:
    """Re-upserting the same (path, kind, chunk_idx) overwrites in place."""
    backend.upsert("/a.md", 1.0, "h1", _vec(0), kind="body", chunk_idx=0)
    backend.upsert("/a.md", 2.0, "h2", _vec(1), kind="body", chunk_idx=0)
    assert backend.all_meta() == {"/a.md": (2.0, "h2", _FP)}
    assert len(_rows(db_conn)) == 1


def test_upsert_many_inserts_all_rows_and_overwrites(
    db_conn: psycopg.Connection, backend: PGVectorBackend
) -> None:
    backend.upsert_many(
        [
            ("/a.md", 1.0, "old", _vec(0), "body", 0),
            ("/b.md", 2.0, "hb", _vec(1), "body", 0),
            ("/a.md", 3.0, "new", _vec(2), "body", 0),
        ]
    )
    assert backend.all_meta() == {"/a.md": (3.0, "new", _FP), "/b.md": (2.0, "hb", _FP)}
    assert len(_rows(db_conn)) == 2


def test_readonly_upsert_many_raises_before_connect() -> None:
    backend = PGVectorBackend(dim=_DIM, fingerprint=_FP, readonly=True)
    with pytest.raises(RuntimeError, match="read-only"):
        backend.upsert_many([("/a.md", 1.0, "ha", _vec(0), "body", 0)])


def test_delete_removes_all_chunks_of_path(
    db_conn: psycopg.Connection, backend: PGVectorBackend
) -> None:
    for idx in range(3):
        backend.upsert("/a.md", 1.0, "ha", _vec(idx), kind="body", chunk_idx=idx)
    backend.upsert("/b.md", 2.0, "hb", _vec(9), kind="body", chunk_idx=0)
    backend.delete("/a.md")
    assert backend.all_meta() == {"/b.md": (2.0, "hb", _FP)}
    assert [r[1] for r in _rows(db_conn)] == ["/b.md"]


def test_delete_missing_noop(backend: PGVectorBackend) -> None:
    backend.delete("/never_existed.md")  # must not raise


# ── read path (gateway contract) ──────────────────────────────────────────


def test_search_topk_orders_by_cosine_and_aggregates(backend: PGVectorBackend) -> None:
    """Same-path chunks collapse to one hit (best cosine wins); ordering is
    cosine-ascending — identical contract to the milvus backend."""
    ones = np.ones(_DIM, dtype=np.float32)
    backend.upsert("/a.md", 1.0, "ha", ones, kind="body", chunk_idx=0)
    backend.upsert("/a.md", 1.0, "ha", 0.9 * ones, kind="body", chunk_idx=1)
    backend.upsert("/a.md", 1.0, "ha", 0.5 * ones, kind="body", chunk_idx=2)
    backend.upsert("/b.md", 2.0, "hb", -ones, kind="body", chunk_idx=0)
    assert backend.search_topk(ones, k=5) == ["/a.md", "/b.md"]
    assert backend.search_topk(ones, k=1) == ["/a.md"]


def test_search_topk_empty_table(backend: PGVectorBackend) -> None:
    assert backend.search_topk(_vec(0), k=5) == []


def test_search_topk_async_matches_sync(backend: PGVectorBackend) -> None:
    """The thread-pooled async variant returns the same order as the sync one."""
    ones = np.ones(_DIM, dtype=np.float32)
    backend.upsert("/a.md", 1.0, "ha", ones, kind="body", chunk_idx=0)
    backend.upsert("/b.md", 2.0, "hb", -ones, kind="body", chunk_idx=0)
    sync = backend.search_topk(ones, k=5)
    async_result = asyncio.run(backend.search_topk_async(ones, 5, timeout=5.0))
    assert async_result == sync == ["/a.md", "/b.md"]


def test_backend_without_connect_uses_short_lived_connections(db_conn: psycopg.Connection) -> None:
    """The gateway path never calls connect(): each operation dials a
    short-lived connection, and close() is a no-op. The table pre-exists
    (the indexer daemon's connect() created it at startup)."""
    with db_conn.cursor() as cur:
        # typmod is a DDL literal, not a bindable param — composed via psycopg.sql.
        cur.execute(
            pgsql.SQL(
                "CREATE TABLE memory_embeddings ("
                "pk VARCHAR(2048) PRIMARY KEY, path VARCHAR(1024) NOT NULL, "
                "kind VARCHAR(16) NOT NULL, chunk_idx BIGINT NOT NULL, "
                "mtime DOUBLE PRECISION NOT NULL, content_hash VARCHAR(128) NOT NULL, "
                "embedder VARCHAR(64) NOT NULL, "
                "vector vector({dim}) NOT NULL)"
            ).format(dim=pgsql.Literal(_DIM))
        )
    db_conn.commit()
    backend = PGVectorBackend(dim=_DIM, fingerprint=_FP)
    ones = np.ones(_DIM, dtype=np.float32)
    backend.upsert("/a.md", 1.0, "ha", ones, kind="body", chunk_idx=0)
    assert backend.search_topk(ones, k=5) == ["/a.md"]
    assert backend.all_meta() == {"/a.md": (1.0, "ha", _FP)}
    backend.close()  # no pool — must not raise


def test_zero_vector_query_returns_defined_ranking(backend: PGVectorBackend) -> None:
    """A zero query vector is defined for pgvector (similarity fixed at 0,
    distance 1.0 for every row) — a defined ranking, never a crash."""
    backend.upsert("/a.md", 1.0, "h", _vec(0), kind="body", chunk_idx=0)
    backend.upsert("/b.md", 2.0, "hb", _vec(1), kind="body", chunk_idx=0)
    result = backend.search_topk(np.zeros(_DIM, dtype=np.float32), 5)
    assert set(result) == {"/a.md", "/b.md"}


def test_nan_query_fails_loudly(backend: PGVectorBackend) -> None:
    """NaN is rejected by Postgres itself (DataException) before scoring — a
    loud failure, not a silent wrong ranking (pinned: this is the pgvector
    behavior the NULL-skip defensive branch never sees)."""
    backend.upsert("/a.md", 1.0, "h", _vec(0), kind="body", chunk_idx=0)
    with pytest.raises(psycopg.errors.DataException):
        backend.search_topk(np.full(_DIM, np.nan, dtype=np.float32), 5)


def test_path_with_quotes_and_backslashes_roundtrips(backend: PGVectorBackend) -> None:
    """Every value is bound, so a hostile path never touches SQL text —
    regression pin for the injection surface."""
    path = '/notes/"quoted"\\back\\slash.md'
    backend.upsert(path, 1.0, "h", _vec(0), kind="body", chunk_idx=0)
    assert backend.all_meta() == {path: (1.0, "h", _FP)}
    assert backend.search_topk(_vec(0), 5) == [path]
    backend.delete(path)
    assert backend.all_meta() == {}


# ── preflight probe (CTO direction ②: fail-fast, actionable) ────────────────


def test_probe_healthy_when_extension_available(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.memory_indexer.backends import probe

    class _FakeConn:
        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def execute(self, sql: str, params: tuple[object, ...] | None = None) -> _FakeConn:
            self._rows = [(1,)]  # count of pg_available_extensions rows
            return self

        def fetchone(self) -> tuple[int]:
            return self._rows[0]

    import shared.db

    monkeypatch.setattr(shared.db, "connect", _FakeConn)
    result = probe.probe_backend("pgvector")
    assert result.message is None
    assert result.fatal is False


def test_probe_fatal_with_actionable_fix_when_extension_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.memory_indexer.backends import probe

    class _FakeConn:
        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def execute(self, sql: str, params: tuple[object, ...] | None = None) -> _FakeConn:
            self._rows = [(0,)]
            return self

        def fetchone(self) -> tuple[int]:
            return self._rows[0]

    import shared.db

    monkeypatch.setattr(shared.db, "connect", _FakeConn)
    result = probe.probe_backend("pgvector")
    assert result.fatal is True
    assert result.message is not None
    assert "fallback-only" in result.message
    assert "milvus" in result.message and "numpy" in result.message  # the actionable switch


def test_probe_transient_when_postgres_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    import shared.db
    from services.memory_indexer.backends import probe

    def _raise(*_a: object, **_kw: object) -> None:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(shared.db, "connect", _raise)
    result = probe.probe_backend("pgvector")
    assert result.fatal is False
    assert result.message is not None
    assert "not reachable" in result.message
