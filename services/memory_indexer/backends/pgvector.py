"""PGVector-backed memory search backend — the cluster Postgres as vector store.

Storage: a `memory_embeddings` table in the cluster's own Postgres (the same
database every other gateway table lives in), schema aligned with the chunk
rows the milvus backend keeps:

- `pk` VARCHAR(2048) PK — the same folded `{path}\x1f{kind}\x1f{chunk_idx}`
  key (`shared.pk_of`), so reconciliation tooling can compare rows across
  backends 1:1
- `path` VARCHAR(1024), `kind` VARCHAR(16), `chunk_idx` BIGINT,
  `mtime` DOUBLE PRECISION, `content_hash` VARCHAR(128)
- `embedder` VARCHAR(64) — the embedding provider `fingerprint` the row
  was produced with (semantic-space id; a change re-embeds the row)
- `vector` vector(dim) — pgvector; dim = the provider's width, injected by
  the factory and checked at connect (mismatch drops + recreates)

The table is a **derived cache** (rebuildable from the memory pool by the
cold-start reconcile), exactly like the milvus collection — so schema
management lives in `connect()` (CREATE EXTENSION IF NOT EXISTS vector +
CREATE TABLE IF NOT EXISTS + dim check, drop + recreate on mismatch) rather
than in the migrations system, which governs business data. It requires the
pgvector binary in the cluster's Postgres — provisioned by
`scripts/provision/database.sh` and CI's `install-pg-redis` action.

Search is exact: `vector <=> %s::vector` over every row (a few thousand rows
x 3072 dims is single-digit ms), aggregated per path (minimum distance wins —
the same contract as milvus COSINE ascending) and returned as top-k paths. No
approximate index yet: at the current pool size an exact scan is cheaper than
an HNSW build, and exactness is what makes this backend a useful
reconciliation baseline for the approximate ones.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Generator
from contextlib import contextmanager

import numpy as np
import psycopg
from psycopg import sql as pgsql
from psycopg_pool import ConnectionPool

import shared.db
from services.memory_indexer.backends.base import KIND_BODY, pk_of

_log = logging.getLogger("services.memory_indexer.backends.pgvector")

_TABLE = "memory_embeddings"
_RAW_SEARCH_LIMIT = 200
"""Raw chunk rows scanned per query; aggregation then reduces to top-k paths
(same value as the milvus backend's _RAW_SEARCH_LIMIT — keeps the two
read paths' contracts aligned)."""

# The table name is a module constant and appears verbatim in every
# statement (a plain LiteralString satisfies psycopg's execute typing); only
# the vector dim is runtime data (the provider's width), composed with
# psycopg.sql. The table is a derived cache — width mismatch or a missing
# column (a pre-provider-era table) drops + recreates, see `_ensure_schema`.
_CREATE_TABLE_SQL = pgsql.SQL(
    """
CREATE TABLE IF NOT EXISTS memory_embeddings (
    pk           VARCHAR(2048) PRIMARY KEY,
    path         VARCHAR(1024) NOT NULL,
    kind         VARCHAR(16) NOT NULL,
    chunk_idx    BIGINT NOT NULL,
    mtime        DOUBLE PRECISION NOT NULL,
    content_hash VARCHAR(128) NOT NULL,
    embedder     VARCHAR(64) NOT NULL,
    vector       vector({dim}) NOT NULL
)
"""
)


def _create_table_sql(dim: int) -> pgsql.Composed:
    """The CREATE TABLE statement at the provider's `dim`."""
    return _CREATE_TABLE_SQL.format(dim=pgsql.Literal(dim))


_UPSERT_SQL = """
INSERT INTO memory_embeddings (pk, path, kind, chunk_idx, mtime, content_hash, embedder, vector)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector)
ON CONFLICT (pk) DO UPDATE SET
    path = EXCLUDED.path,
    kind = EXCLUDED.kind,
    chunk_idx = EXCLUDED.chunk_idx,
    mtime = EXCLUDED.mtime,
    content_hash = EXCLUDED.content_hash,
    embedder = EXCLUDED.embedder,
    vector = EXCLUDED.vector
"""

_DELETE_SQL = "DELETE FROM memory_embeddings WHERE path = %s"

_ALL_META_SQL = "SELECT path, mtime, content_hash, embedder FROM memory_embeddings"

_SEARCH_SQL = """
SELECT path, (vector <=> %s::vector) AS distance
FROM memory_embeddings
ORDER BY distance
LIMIT %s
"""


def _vector_text(embedding: np.ndarray) -> str:
    """The pgvector literal for a float32 vector — `[v0, v1, ...]`.

    Passed as a bound parameter cast with `::vector`, so the string never
    touches SQL text (no injection surface) and no client-side vector adapter
    dependency is needed.
    """
    return "[" + ",".join(str(float(v)) for v in embedding.astype(np.float32)) + "]"


def _dim_of(client: psycopg.Connection) -> int | None:
    """The `vector(N)` typmod dim of the table's vector column, or None when
    the table does not exist.

    pgvector encodes the dim as the typmod itself (verified against real
    vector(3072) / vector(768) columns: atttypmod == dim)."""
    row = client.execute(
        "SELECT atttypmod FROM pg_attribute "
        "WHERE attrelid = to_regclass(%s) AND attname = 'vector'",
        (_TABLE,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


_EXPECTED_COLUMNS = {
    "pk",
    "path",
    "kind",
    "chunk_idx",
    "mtime",
    "content_hash",
    "embedder",
    "vector",
}


def _columns_of(client: psycopg.Connection) -> set[str]:
    """The table's live column set, or empty when the table does not exist."""
    rows = client.execute(
        "SELECT attname FROM pg_attribute "
        "WHERE attrelid = to_regclass(%s) AND attnum > 0 AND NOT attisdropped",
        (_TABLE,),
    ).fetchall()
    return {r[0] for r in rows}


def _ensure_schema(client: psycopg.Connection, dim: int) -> None:
    """CREATE EXTENSION IF NOT EXISTS + table, with drop + recreate on dim
    drift or a missing column — the same derived-cache philosophy as the
    milvus collection: a mismatched cache (another provider's width, or a
    pre-provider-era table without the `embedder` column) is rebuilt by the
    cold-start reconcile, never migrated."""
    client.execute("CREATE EXTENSION IF NOT EXISTS vector")
    table_dim = _dim_of(client)
    stale: str | None = None
    if table_dim is not None and table_dim != dim:
        stale = f"vector dim mismatch ({table_dim} != {dim})"
    elif table_dim is not None and _columns_of(client) != _EXPECTED_COLUMNS:
        stale = "column set mismatch with the current schema"
    if stale is not None:
        _log.warning(
            "[pgvector] table %s %s — dropping + recreating; cold-start will rebuild the index",
            _TABLE,
            stale,
        )
        client.execute("DROP TABLE memory_embeddings")
    client.execute(_create_table_sql(dim))
    client.commit()


class PGVectorBackend:
    """Protocol-compliant backend over the cluster Postgres + pgvector.

    The vector space is injected (factory wiring): `dim` is the table's
    vector width and `fingerprint` is stamped on every row — no import of
    provider constants. `connect()` opens a long-lived connection pool and
    ensures the schema — the indexer daemon's path. The gateway's read path
    never calls `connect()`; each `search_topk_async` opens one short-lived
    connection instead (bounded by the caller's deadline, no pool lifecycle
    to leak).
    """

    name = "pgvector"

    def __init__(self, dim: int, fingerprint: str) -> None:
        self._dim = dim
        self._fingerprint = fingerprint
        self._pool: ConnectionPool | None = None

    @contextmanager
    def _conn(self, *, timeout: float | None = None) -> Generator[psycopg.Connection, None, None]:
        """A connection to run one operation on: a pool borrow when connected,
        a short-lived dial otherwise. Both arms commit on success; a failing
        block rolls back (pool reset / close)."""
        if self._pool is not None:
            with self._pool.connection(timeout=timeout) as conn:
                yield conn
                conn.commit()
        else:
            with shared.db.connect() as conn:
                yield conn
                conn.commit()

    def connect(self) -> None:
        """Open the connection pool + ensure extension/table/dim.

        The pool exists so the indexer daemon's batched upserts do not pay a
        dial per batch; `shared.db.pool()` is the only sanctioned pool
        constructor (keepalives + statement ceiling)."""
        self._pool = shared.db.pool()
        with self._pool.connection() as conn:
            _ensure_schema(conn, self._dim)

    def close(self) -> None:
        """Close the pool when one exists; idempotent."""
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def upsert(
        self,
        path: str,
        mtime: float,
        content_hash: str,
        embedding: np.ndarray,
        *,
        kind: str = KIND_BODY,
        chunk_idx: int = 0,
    ) -> None:
        """Write / update one chunk row, keyed by (path, kind, chunk_idx).

        Re-upserting the same triple overwrites in place via the folded pk —
        identical semantics to the milvus backend."""
        with self._conn() as conn:
            conn.execute(
                _UPSERT_SQL,
                (
                    pk_of(path, kind, chunk_idx),
                    path,
                    kind,
                    int(chunk_idx),
                    float(mtime),
                    content_hash,
                    self._fingerprint,
                    _vector_text(embedding),
                ),
            )

    def delete(self, path: str) -> None:
        """Delete every chunk row of `path`. No-ops when the path has no rows.

        Plain equality — no filter-expression escaping worries the way milvus
        boolean filters have them."""
        with self._conn() as conn:
            conn.execute(_DELETE_SQL, (path,))

    def all_meta(self) -> dict[str, tuple[float, str, str]]:
        """Per-path (mtime, content_hash, provider_fingerprint) — one entry
        per **file**, not per chunk (aggregation keeps the max mtime; same
        contract as milvus). The fingerprint is the reconcile key's third
        element.

        No row cap: the milvus backend truncates its all_meta at 16384 chunk
        rows (a milvus-side quirk), so a pool past that size would show a
        spurious row-set diff in reconciliation — this side is the more
        correct one, not the regression."""
        meta: dict[str, tuple[float, str, str]] = {}
        with self._conn() as conn:
            rows = conn.execute(_ALL_META_SQL).fetchall()
        for row in rows:
            path, mtime, hash_, fingerprint = row
            prev = meta.get(path)
            if prev is None or mtime > prev[0]:
                meta[path] = (mtime, hash_, fingerprint)
        return meta

    def search_topk(self, query_vector: np.ndarray, k: int) -> list[str]:
        """Cosine top-k **paths**, aggregated over chunk rows — see
        `_search_topk`."""
        return self._search_topk(query_vector, k, None)

    def _search_topk(self, query_vector: np.ndarray, k: int, timeout: float | None) -> list[str]:
        """Cosine top-k **paths**, aggregated over chunk rows.

        `vector <=> %s` is pgvector's cosine distance (1 - cosine_similarity,
        ascending — identical semantics to milvus COSINE). Raw rows aggregate
        per path (minimum distance wins) and the top-k paths return in that
        order; fewer than k when the table has fewer distinct paths; empty
        when the table is empty. `timeout` bounds the pool-borrow wait when
        connected (None = the pool's default)."""
        with self._conn(timeout=timeout) as conn:
            rows = conn.execute(
                _SEARCH_SQL,
                (_vector_text(query_vector), max(_RAW_SEARCH_LIMIT, k)),
            ).fetchall()
        best: dict[str, float] = {}
        for path, distance in rows:
            if distance is None:
                # Defensive branch, unreachable against current pgvector
                # (verified on 0.8.6: a zero query vector is defined —
                # similarity fixed at 0, distance 1.0 for every row — and NaN
                # is rejected by Postgres itself with DataException before
                # any row is scored). Kept so a future NULL distance can never
                # become `float(None)` -> TypeError -> 503: an unrankable row
                # is skipped, not ranked and not crashed on.
                continue
            if path not in best or distance < best[path]:
                best[path] = float(distance)
        return sorted(best, key=best.__getitem__)[:k]

    async def search_topk_async(
        self, query_vector: np.ndarray, k: int, *, timeout: float
    ) -> list[str]:
        """Async twin of `search_topk` — thread-pooled over the sync query
        (the protocol allows it; psycopg is sync here). `timeout` bounds the
        pool-borrow wait when connected; the caller's `asyncio.timeout` covers
        the whole call either way."""
        return await asyncio.to_thread(self._search_topk, query_vector, k, timeout)
