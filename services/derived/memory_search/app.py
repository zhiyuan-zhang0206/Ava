"""The memory search HTTP API — FastAPI over the in-process MemoryStore.

Endpoints mirror the backend protocol one-to-one (upsert / upsert_batch /
delete / meta / search), so `backends.numpy.NumPyBackend` is a thin HTTP client (the indexer daemon
and the gateway talk to it over HTTP). Every mutation persists the npz before responding, so a kill
-after-ack never loses a row.

`GET /stats` is the one non-protocol endpoint: current chunk rows plus the
duration of the most recent npz save — the ops surface for the memory-search
row-growth monitoring. A lifespan background task samples the same two
numbers every 60s and emits them as a `memory_search_stats` telemetry
event, so they reach Prometheus through the existing OTLP path under this
process's own job label (the `service_started` series rides the same path).

Binds loopback only (the daemon passes host="127.0.0.1") — a strictly local
service; no LAN port is opened.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import numpy as np
from fastapi import FastAPI
from pydantic import BaseModel, Field

from base import telemetry
from services.derived.memory_search.store import MemoryStore

_log = logging.getLogger("services.derived.memory_search.app")

# Loopback-only is the trust model, not a security boundary. Each app's
# composition root supplies its embedding width so the wire models reject
# oversized vectors before numpy allocates; the store also checks exact width.
_MAX_K = 1000

# One stats sample per minute — bounded row rate, same cadence as the
# gateway's gauge flushers (agent_registry / auth401 / latency). A 60s
# gauge is plenty for a growth curve that moves with note churn.
_STATS_FLUSH_INTERVAL_S = 60.0


class DeleteBody(BaseModel):
    path: str


class DeleteStaleRow(BaseModel):
    path: str
    kind_limits: dict[str, int]


class DeleteStaleBatchBody(BaseModel):
    entries: list[DeleteStaleRow] = Field(min_length=1)


def emit_memory_search_stats(rows: int, last_save_seconds: float | None) -> None:
    """Emit one `memory_search_stats` telemetry event carrying the store's
    absolute state.

    `rows` and `last_save_seconds` are state, never sums — the
    `_METRIC_DISPOSITION` override records both as ObservableGauges
    (`ava_memory_search_stats_rows_ratio` /
    `ava_memory_search_stats_last_save_seconds`), so a flat store does not
    accrue value the way Counters would. `last_save_seconds` is None until
    the first save since boot; the field is omitted then (an absent optional
    metric is not zero).

    Exposed separately from the flusher so tests can drive it directly
    (mirrors `services/upkeep/events_maintenance/registry_gauge.py:emit_max_agent_id`).
    """
    attributes: dict[str, int | float] = {"rows": rows}
    if last_save_seconds is not None:
        attributes["last_save_seconds"] = last_save_seconds
    telemetry.emit("telemetry", "memory_search_stats", attributes=attributes)


async def _stats_flusher(store: MemoryStore, lock: asyncio.Lock) -> None:
    """Sample the store's stats every `_STATS_FLUSH_INTERVAL_S` and emit.

    Runs as a lifespan background task, cancelled on teardown. The read
    takes the mutation lock so rows + last_save_seconds are one consistent
    snapshot (save runs in a worker thread under that same lock); a failed
    emit never kills the loop — a dropped sample is only a monitoring gap.
    """
    # quiesce-exempt: samples an in-memory store; no database
    while True:
        await asyncio.sleep(_STATS_FLUSH_INTERVAL_S)
        try:
            async with lock:
                rows = len(store)
                last_save_seconds = store.last_save_seconds
        except Exception:
            _log.warning("[memory_search] stats read failed", exc_info=True)
            continue
        try:
            emit_memory_search_stats(rows, last_save_seconds)
        except Exception:
            _log.warning("[memory_search] stats emit failed", exc_info=True)


def _mount_mutations(
    app: FastAPI, store: MemoryStore, lock: asyncio.Lock, max_batch_rows: int, embedding_dim: int
) -> None:
    """The four protocol write endpoints (upsert / upsert_batch / delete /
    delete_stale_batch) — extracted so build_app stays a router, not a wall
    of handlers. Each mutation persists the npz before responding."""
    from fastapi import HTTPException

    class UpsertBody(BaseModel):
        path: str
        mtime: float
        content_hash: str
        kind: str
        chunk_idx: int
        vector: list[float] = Field(max_length=embedding_dim)

    class UpsertBatchBody(BaseModel):
        rows: list[UpsertBody] = Field(min_length=1)

    def check_batch(size: int) -> None:
        if size > max_batch_rows:
            raise HTTPException(
                status_code=422, detail=f"batch of {size} rows exceeds the limit {max_batch_rows}"
            )

    @app.post("/upsert")
    async def upsert(body: UpsertBody) -> dict[str, str]:
        vector = np.asarray(body.vector, dtype=np.float32)
        try:
            async with lock:
                store.upsert(
                    body.path,
                    body.mtime,
                    body.content_hash,
                    vector,
                    kind=body.kind,
                    chunk_idx=body.chunk_idx,
                )
                await asyncio.to_thread(store.save)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "ok"}

    @app.post("/upsert_batch")
    async def upsert_batch(body: UpsertBatchBody) -> dict[str, str]:
        check_batch(len(body.rows))
        rows = [
            (
                row.path,
                row.mtime,
                row.content_hash,
                np.asarray(row.vector, dtype=np.float32),
                row.kind,
                row.chunk_idx,
            )
            for row in body.rows
        ]
        try:
            async with lock:
                store.upsert_many(rows)
                await asyncio.to_thread(store.save)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "ok"}

    @app.post("/delete")
    async def delete(body: DeleteBody) -> dict[str, str]:
        async with lock:
            store.delete(body.path)
            await asyncio.to_thread(store.save)
        return {"status": "ok"}

    @app.post("/delete_stale_batch")
    async def delete_stale_batch(body: DeleteStaleBatchBody) -> dict[str, str]:
        """Tail-cleanup companion to /upsert_batch (issue #1946): one lock +
        one npz save for every path the current files no longer fully cover."""
        check_batch(len(body.entries))
        async with lock:
            store.delete_stale_rows([(row.path, row.kind_limits) for row in body.entries])
            await asyncio.to_thread(store.save)
        return {"status": "ok"}


def build_app(store: MemoryStore, max_batch_rows: int, *, embedding_dim: int) -> FastAPI:
    """Wire the store into a FastAPI app. One mutation lock serializes every
    operation (search included) — the store is pure in-memory state and a
    full exact scan is microseconds, so the lock is the whole concurrency
    story at this scale. The stats flusher runs as a lifespan task so the
    metrics stream lives and dies with the serving process. `max_batch_rows` is
    the cluster's batch-size bound (`services.memory_search_max_batch_rows`)."""

    class SearchBody(BaseModel):
        vector: list[float] = Field(max_length=embedding_dim)
        k: int = Field(ge=1, le=_MAX_K)

    lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        async with asyncio.TaskGroup() as background:
            flusher = background.create_task(_stats_flusher(store, lock))
            try:
                yield
            finally:
                flusher.cancel()

    app = FastAPI(title="ava-memory-search", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/stats")
    async def stats() -> dict[str, int | float | None]:
        async with lock:
            return {"rows": len(store), "last_save_seconds": store.last_save_seconds}

    _mount_mutations(app, store, lock, max_batch_rows, embedding_dim)

    @app.get("/meta")
    async def meta() -> dict[str, tuple[float, str, str]]:
        """Per-path (mtime, content_hash, provider_fingerprint) — same shape
        the backend protocol's `all_meta` returns (the reconcile key)."""
        async with lock:
            return store.all_meta()

    @app.post("/search")
    async def search(body: SearchBody) -> dict[str, list[str]]:
        vector = np.asarray(body.vector, dtype=np.float32)
        try:
            async with lock:
                paths = store.search_topk(vector, body.k)
        except ValueError as exc:
            from fastapi import HTTPException

            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"paths": paths}

    return app
