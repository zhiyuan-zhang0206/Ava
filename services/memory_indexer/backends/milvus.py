"""Milvus-backed embedding index (the `milvus` memory search backend).

This module holds the raw pymilvus storage primitives (module-level
functions) plus the `MilvusBackend` class implementing the shared
`MemorySearchBackend` protocol. New code goes through
`backends.factory.get_backend()`, never these functions directly.

connect via `MilvusClient(uri=<server_uri>)` — goes through the
dedicated `ava-milvus` session
(`services/milvus/daemon.py`); multi processes connect via the same URI.
This module is decoupled from the specific backing (milvus-lite local
server / full Milvus docker / Zilliz Cloud); switching only edits the
`AVA_MILVUS_URI` env, no code changes.

Schema: single collection `memory_embeddings`, one row per **chunk**:
- pk VARCHAR(2048) PK — `"{path}\x1f{kind}\x1f{chunk_idx}"`. milvus-lite
  3.x allows only one primary-key field, so the (path, kind, chunk_idx)
  triple folds into one VARCHAR key (deterministic, reversible); a
  re-upsert of the same triple overwrites in place.
- path VARCHAR(1024) — filter field, used by delete-by-path
- kind VARCHAR(16) — "desc" (frontmatter description, chunk_idx=0) or
  "body" (a body chunk, chunk_idx=0..N-1)
- chunk_idx INT64
- mtime DOUBLE — used by cold-start reconcile
- content_hash VARCHAR(128) — secondary gate when mtime is touched but
  content unchanged; skips re-embed
- `embedder` VARCHAR(64) — the embedding provider `fingerprint` the row
  was produced with (semantic-space id; a change re-embeds the row)
- `vector` FLOAT_VECTOR({dim}) COSINE AUTOINDEX — width = the provider's dim
  (injected by the factory; the indexer daemon rebuilds mismatched collections)

One file maps to 0-or-1 desc row + N body-chunk rows; embedding the
frontmatter description on its own keeps short entity-bearing lines
(which used to drown inside a long body) searchable. `search_topk`
pulls raw chunk hits, aggregates them per path (best cosine wins) and
returns top-k **paths** — the caller-facing contract (list of paths) is
unchanged.

The indexer daemon's writable cold-start reconcile detects the pre-chunking
collection (path as PK, no kind/chunk_idx) and rebuilds it so the cold-start
scan can repopulate the whole index chunked. Read-only connections refuse a
missing or mismatched collection instead of changing persistent storage.

Milvus COSINE returns "distance" = 1 - cosine_similarity, ascending (0
= identical); aggregation keeps the minimum distance per path.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import Any, cast

import numpy as np
from pymilvus import AsyncMilvusClient, DataType, MilvusClient

from services.memory_indexer.backends.base import _PK_MAX_LENGTH, KIND_BODY, pk_of
from shared.config import settings

_log = logging.getLogger("services.memory_indexer.index")

_COLLECTION = "memory_embeddings"


_EXPECTED_FIELDS = frozenset(
    {"pk", "path", "kind", "chunk_idx", "mtime", "content_hash", "embedder", "vector"}
)

_RAW_SEARCH_LIMIT = 200
"""Raw chunk hits pulled per query; aggregation then reduces to top-k paths."""


def server_uri() -> str:
    """Milvus server URI. env override -> default `http://127.0.0.1:19530`.

    prod / dev / eval / CI all connect to standalone server; no
    lite-in-process mixing — avoids "test vs prod" behavior drift
    (milvus-lite in-process flock prevents multi-process access).
    """
    return settings.services.milvus_uri


def _create_collection(client: MilvusClient, dim: int) -> None:
    """Create the chunked collection (schema + AUTOINDEX) on `client`.

    `dim` is the provider's embedding width — the vector field's declared
    width; a collection built for another width is a mismatched cache and is
    rebuilt by a writable indexer-daemon connection."""
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)  # pyright: ignore[reportUnknownMemberType]
    schema.add_field("pk", DataType.VARCHAR, is_primary=True, max_length=_PK_MAX_LENGTH)  # pyright: ignore[reportUnknownMemberType]
    schema.add_field("path", DataType.VARCHAR, max_length=1024)  # pyright: ignore[reportUnknownMemberType]
    schema.add_field("kind", DataType.VARCHAR, max_length=16)  # pyright: ignore[reportUnknownMemberType]
    schema.add_field("chunk_idx", DataType.INT64)  # pyright: ignore[reportUnknownMemberType]
    schema.add_field("mtime", DataType.DOUBLE)  # pyright: ignore[reportUnknownMemberType]
    schema.add_field("content_hash", DataType.VARCHAR, max_length=128)  # pyright: ignore[reportUnknownMemberType]
    schema.add_field("embedder", DataType.VARCHAR, max_length=64)  # pyright: ignore[reportUnknownMemberType]
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)  # pyright: ignore[reportUnknownMemberType]
    idx = client.prepare_index_params()  # pyright: ignore[reportUnknownMemberType]
    idx.add_index(field_name="vector", metric_type="COSINE", index_type="AUTOINDEX")  # pyright: ignore[reportUnknownMemberType]
    client.create_collection(collection_name=_COLLECTION, schema=schema, index_params=idx)  # pyright: ignore[reportUnknownMemberType]


def _schema_problem(client: MilvusClient, dim: int) -> str | None:
    """Describe why the collection cannot serve vectors at `dim`, if anything.

    The legacy collection had `path` as its primary key and no `kind` /
    `chunk_idx` fields; the chunked schema adds those and folds the triple
    into a `pk` key. This detail makes read-only errors actionable without
    loosening their no-mutation guarantee.
    """
    try:
        # pymilvus's stubs type every client method as an async Unknown; the
        # real MilvusClient is sync and returns a dict here.
        info = cast(
            Any,
            client.describe_collection(collection_name=_COLLECTION),  # pyright: ignore[reportUnknownMemberType]
        )
    except Exception:
        return "could not be inspected"
    # pymilvus 3.x returns a plain dict here; older versions return an object
    # with a `.fields` attribute — accept both.
    # isinstance-narrowing an Any yields dict[Unknown, ...], so the fields
    # access below is silenced the same way the other pymilvus calls are.
    raw: Any = info.get("fields") if isinstance(info, dict) else getattr(info, "fields", None)  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    raw_list: list[Any] = raw if isinstance(raw, list) else []  # pyright: ignore[reportUnknownVariableType]
    names: set[str] = {f.get("name") for f in raw_list if isinstance(f.get("name"), str)}
    primaries: set[str] = {
        f.get("name") for f in raw_list if isinstance(f.get("name"), str) and f.get("is_primary")
    }
    vec_dim: int | None = None
    for f in raw_list:
        if isinstance(f.get("name"), str) and f.get("name") == "vector":
            params: dict[str, Any] = cast(dict[str, Any], f.get("params") or {})  # pyright: ignore[reportUnknownVariableType]
            if isinstance(params.get("dim"), int):
                vec_dim = params.get("dim")
    details: list[str] = []
    missing = sorted(_EXPECTED_FIELDS - names)
    extra = sorted(names - _EXPECTED_FIELDS)
    if missing:
        details.append(f"missing fields {missing}")
    if extra:
        details.append(f"unexpected fields {extra}")
    if primaries != {"pk"}:
        details.append(f"primary key fields are {sorted(primaries)} instead of ['pk']")
    if vec_dim != dim:
        details.append(f"vector dimension is {vec_dim}, expected {dim}")
    return "; ".join(details) if details else None


def _schema_current(client: MilvusClient, dim: int) -> bool:
    """True when the existing collection has the chunked schema at `dim`."""
    return _schema_problem(client, dim) is None


def _readonly_schema_error(problem: str) -> RuntimeError:
    """The safe failure for a read-only backend that cannot search."""
    return RuntimeError(
        f"Milvus collection {_COLLECTION!r} {problem}; a read-only connection will not "
        "create or rebuild it. The indexer daemon's cold-start reconcile on startup is "
        "the legitimate writer."
    )


def _connect(dim: int, *, readonly: bool = False) -> MilvusClient:
    """Connect to milvus server + ensure collection exists + load into
    memory (at the provider's `dim`). Caller is responsible for `client.close()`.

    A writable connection creates a missing collection or replaces a stale
    derived cache; that path belongs to the indexer daemon's cold-start
    reconcile. A read-only connection rejects either state before issuing a
    persistent mutation. The first `create_collection` auto-loads, but after
    a milvus server restart the collection switches to "released" state
    (memory cleared, persistent data remains), and subsequent query/search
    hit `MilvusException(code=101) Collection ... is in state 'released'`.
    This function unconditionally `load_collection` as a defensive guard
    (already-loaded is idempotent) so callers do not need to worry about the
    load lifecycle.
    """
    client = MilvusClient(uri=server_uri())
    # pymilvus's sync MilvusClient.has_collection returns bool, but its untyped
    # internals make pyright infer a coroutine (async client shape) — the guard
    # is real, not always-true. Every pymilvus client/schema method below also types
    # its **kwargs as Unknown (reportUnknownMemberType); the call args are fully typed.
    if not client.has_collection(collection_name=_COLLECTION):  # pyright: ignore[reportUnnecessaryComparison, reportUnknownMemberType]
        if readonly:
            client.close()
            raise _readonly_schema_error("is missing")
        _create_collection(client, dim)
    else:
        problem = _schema_problem(client, dim)
        if problem is not None:
            if readonly:
                client.close()
                raise _readonly_schema_error(f"has a schema mismatch: {problem}")
            _log.warning(
                "[index] collection %s schema mismatch (%s) — dropping + recreating; "
                "the indexer daemon cold-start reconcile will rebuild the index chunked",
                _COLLECTION,
                problem,
            )
            client.drop_collection(collection_name=_COLLECTION)  # pyright: ignore[reportUnknownMemberType]
            _create_collection(client, dim)
    client.load_collection(collection_name=_COLLECTION)  # pyright: ignore[reportUnknownMemberType]
    return client


def _path_filter(path: str) -> str | None:
    """The Milvus boolean expression `path == "<path>"`, or None when the
    path cannot be expressed (it contains a double quote or a backslash — the expression
    parser has no escape sequences). Memory-pool filenames never contain
    those characters in practice; a None here is logged and skipped rather
    than crashing the daemon.
    """
    if '"' in path or "\\" in path:
        return None
    return f'path == "{path}"'


def _upsert(
    client: MilvusClient,
    path: str,
    mtime: float,
    hash_: str,
    embedding: np.ndarray,
    *,
    kind: str = KIND_BODY,
    chunk_idx: int = 0,
    fingerprint: str = "",
) -> None:
    """Write / update one chunk row, keyed by (path, kind, chunk_idx).

    `kind` is KIND_DESC (the frontmatter description, chunk_idx=0) or
    KIND_BODY (a body chunk). Re-upserting the same triple overwrites in
    place via the folded pk. `fingerprint` is the embedding provider's
    semantic-space id, stamped on the row for the reconcile diff.
    """
    # pymilvus client.upsert types its **kwargs as Unknown; the call args are fully typed.
    client.upsert(  # pyright: ignore[reportUnknownMemberType]
        collection_name=_COLLECTION,
        data=[
            {
                "pk": pk_of(path, kind, chunk_idx),
                "path": path,
                "kind": kind,
                "chunk_idx": int(chunk_idx),
                "mtime": float(mtime),
                "content_hash": hash_,
                "embedder": fingerprint,
                "vector": embedding.astype(np.float32).tolist(),
            }
        ],
    )


def _delete(client: MilvusClient, path: str) -> None:
    """Delete **every** chunk row of `path`. Milvus no-ops without raising
    when the path does not exist.

    Filter-based (`path == "<path>"`), so one call clears the desc row and
    all body chunks. A path containing a double quote or a backslash cannot be expressed in a
    Milvus boolean expression; it is logged and skipped (leaving stale rows)
    instead of raising into the daemon.
    """
    expr = _path_filter(path)
    if expr is None:
        _log.warning("[index] cannot express delete filter for %r — skipping", path)
        return
    # pymilvus client.delete types its **kwargs as Unknown; the call args are fully typed.
    client.delete(collection_name=_COLLECTION, filter=expr)  # pyright: ignore[reportUnknownMemberType]


def _all_meta(client: MilvusClient) -> dict[str, tuple[float, str, str]]:
    """Per-path (mtime, content_hash, provider_fingerprint) — one entry per
    **file**, not per chunk.

    Chunk rows of one path share the file's mtime, content_hash and
    provider fingerprint; the aggregation keeps the max mtime. The
    fingerprint is the reconcile key's third element: the cold-start
    reconcile diffs disk against this dict to decide which paths need
    re-embedding (a changed provider fingerprint re-embeds everything).

    Does not load vector BLOBs — the meta query only decides which paths need
    re-embedding; a few thousand files takes a few ms.
    """
    # pymilvus client.query types its **kwargs as Unknown and returns rows as
    # dict[Unknown, Unknown]; declare the intended row type (which cleans all
    # downstream use) and suppress the library's residual unknown at the call site.
    rows: list[dict[str, Any]] = client.query(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        collection_name=_COLLECTION,
        filter="",
        output_fields=["path", "mtime", "content_hash", "embedder"],
        limit=16384,
    )
    meta: dict[str, tuple[float, str, str]] = {}
    for r in rows:
        prev = meta.get(r["path"])
        if prev is None or r["mtime"] > prev[0]:
            meta[r["path"]] = (r["mtime"], r["content_hash"], r["embedder"])
    return meta


async def _connect_async(*, timeout: float) -> AsyncMilvusClient:
    """Async milvus client for the gateway's search path — keeps the whole
    request off the event loop without a thread hop (pymilvus 3.x ships an
    async client over the same gRPC endpoint).

    ``timeout`` is mandatory because pymilvus treats "no timeout" as "wait
    forever", and it waits while holding a process-global lock: every connect
    and every close goes through one ``AsyncConnectionManager`` asyncio.Lock
    shared by the whole process, so a single unbounded connect wedges every
    later milvus call in the gateway. Passing it through bounds
    ``ensure_channel_ready``'s channel wait.
    """
    return AsyncMilvusClient(uri=server_uri(), timeout=timeout)  # pyright: ignore[reportUnknownMemberType]


async def _search_topk_async(
    client: AsyncMilvusClient, query_vector: np.ndarray, k: int, *, timeout: float
) -> list[str]:
    """Async twin of ``search_topk`` — same chunk-aggregation contract.

    ``timeout`` is mandatory: without one, pymilvus's ``retry_on_rpc_failure``
    runs up to 75 backoff retries AND awaits each attempt with no
    ``asyncio.wait_for``, so one unresponsive RPC never returns. With a timeout
    every attempt is deadline-wrapped and the retry loop is bounded by it.
    """
    result: list[list[dict[str, Any]]] = await client.search(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        collection_name=_COLLECTION,
        data=[query_vector.astype(np.float32).tolist()],
        limit=max(_RAW_SEARCH_LIMIT, k),
        output_fields=["path"],
        timeout=timeout,
    )
    if not result or not result[0]:
        return []
    best: dict[str, float] = {}
    for hit in result[0]:
        path = hit["entity"]["path"]
        distance = hit["distance"]
        if path not in best or distance < best[path]:
            best[path] = distance
    return sorted(best, key=best.__getitem__)[:k]


def _search_topk(client: MilvusClient, query_vector: np.ndarray, k: int) -> list[str]:
    """Cosine top-k **paths**, aggregated over chunk rows.

    Milvus returns the top-`_RAW_SEARCH_LIMIT` chunk rows by distance
    ascending (identical=0); rows of the same path collapse to their best
    (minimum) distance, then the top-k paths are returned in that order.
    Returns fewer than k when the collection has fewer distinct paths; an
    empty list when the collection is empty.

    The caller-facing contract (a list of path strings) is unchanged from the
    single-row-per-file era — only the internals became chunk-aware.
    """
    # pymilvus client.search types its **kwargs as Unknown and returns nested
    # dict[Unknown, Unknown]; declare the intended result type (which cleans all
    # downstream use) and suppress the library's residual unknown at the call site.
    result: list[list[dict[str, Any]]] = client.search(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        collection_name=_COLLECTION,
        data=[query_vector.astype(np.float32).tolist()],
        limit=max(_RAW_SEARCH_LIMIT, k),
        output_fields=["path"],
    )
    if not result or not result[0]:
        return []
    best: dict[str, float] = {}
    for hit in result[0]:
        path = hit["entity"]["path"]
        distance = hit["distance"]
        if path not in best or distance < best[path]:
            best[path] = distance
    return sorted(best, key=best.__getitem__)[:k]


class MilvusBackend:
    """Protocol-compliant backend over the module-level milvus primitives.

    The module functions above are the raw pymilvus storage engine — moved
    verbatim from the pre-abstraction `services.memory_indexer.index` and
    still re-exported there for the legacy regression suite. New code goes
    through this class via `backends.factory.get_backend()`.

    The vector space is injected (factory wiring): `dim` is the collection's
    vector width and `fingerprint` is stamped on every row — no import of
    provider constants. `client` is injectable for tests that already hold a
    connected client (the `milvus_client` fixture); production paths call
    `connect()`.
    """

    name = "milvus"

    def __init__(
        self,
        dim: int,
        fingerprint: str,
        client: MilvusClient | None = None,
        *,
        readonly: bool = False,
    ) -> None:
        self._dim = dim
        self._fingerprint = fingerprint
        self._client = client
        self._readonly = readonly

    def _require_client(self) -> MilvusClient:
        """The connected client, or fail fast — using an unconnected
        backend is a caller bug, not something to paper over."""
        if self._client is None:
            raise RuntimeError(f"{self.name} backend not connected — call connect() first")
        return self._client

    def connect(self) -> None:
        """Connect + ensure collection + load (see `_connect`)."""
        self._client = _connect(self._dim, readonly=self._readonly)

    def _require_writable(self) -> None:
        """Reject mutations through a backend created for read-only work."""
        if self._readonly:
            raise RuntimeError(
                "milvus backend is read-only; only the indexer daemon's cold-start "
                "reconcile on startup may write this collection"
            )

    def close(self) -> None:
        """Close the held client; idempotent, safe to call twice."""
        if self._client is not None:
            self._client.close()
            self._client = None

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
        """Write / update one chunk row — see `_upsert`."""
        self._require_writable()
        _upsert(
            self._require_client(),
            path,
            mtime,
            content_hash,
            embedding,
            kind=kind,
            chunk_idx=chunk_idx,
            fingerprint=self._fingerprint,
        )

    def delete(self, path: str) -> None:
        """Delete every chunk row of `path` — see `_delete`."""
        self._require_writable()
        _delete(self._require_client(), path)

    def all_meta(self) -> dict[str, tuple[float, str, str]]:
        """Per-path (mtime, content_hash, provider_fingerprint) — see
        `_all_meta`."""
        return _all_meta(self._require_client())

    def search_topk(self, query_vector: np.ndarray, k: int) -> list[str]:
        """Cosine top-k paths — see `_search_topk`."""
        return _search_topk(self._require_client(), query_vector, k)

    async def search_topk_async(
        self, query_vector: np.ndarray, k: int, *, timeout: float
    ) -> list[str]:
        """Async twin of `search_topk` — the gateway's search path.

        Connects a per-call async client so the whole request stays off the
        event loop, hands `timeout` to connect and search (mandatory — see
        `_connect_async` for why), and closes the client in `finally` so a
        failing search cannot leak the connection.
        """
        client = await _connect_async(timeout=timeout)
        try:
            return await _search_topk_async(client, query_vector, k, timeout=timeout)
        finally:
            with suppress(Exception):
                await client.close()
