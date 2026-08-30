"""The memory search HTTP API — FastAPI over the in-process MemoryStore.

Endpoints mirror the backend protocol one-to-one (upsert / delete / meta /
search), so `backends.numpy.NumPyBackend` is a thin HTTP client and the
indexer daemon + gateway treat this service exactly like the milvus
daemon. Every mutation persists the npz before responding, so a kill
-after-ack never loses a row.

`GET /stats` is the one non-protocol endpoint: current chunk rows plus the
duration of the most recent npz save — the ops surface for the memory-search
row-growth monitoring. A lifespan background task samples the same two
numbers every 60s and emits them as a `memory_search_stats` telemetry
event, so they reach Prometheus through the existing OTLP path under this
process's own job label (the `service_started` series rides the same path).

Binds loopback only (the daemon passes host="127.0.0.1") — like milvus,
this is a strictly local service; no LAN port is opened.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import numpy as np
from fastapi import FastAPI
from pydantic import BaseModel, Field

from services.memory_indexer.embeddings.factory import get_provider
from services.memory_search.store import MemoryStore
from shared import telemetry

_log = logging.getLogger("services.memory_search.app")

# Loopback-only is the trust model, not a security boundary: any local
# process could still POST a huge body and drive an allocation, so the
# wire models carry hard size bounds. An embedding is exactly
# `_EMBED_DIM` floats — the configured provider's width, resolved at
# import (an unknown AVA_EMBEDDING_BACKEND fails the service boot loudly)
# — and anything longer is malformed and rejected before numpy ever
# allocates. The store keeps its own exact-dim check as the last gate.
_EMBED_DIM = get_provider().dim
_MAX_K = 1000

# One stats sample per minute — bounded row rate, same cadence as the
# gateway's gauge flushers (agent_registry / auth401 / latency). A 60s
# gauge is plenty for a growth curve that moves with note churn.
_STATS_FLUSH_INTERVAL_S = 60.0


class UpsertBody(BaseModel):
    path: str
    mtime: float
    content_hash: str
    kind: str
    chunk_idx: int
    vector: list[float] = Field(max_length=_EMBED_DIM)


class DeleteBody(BaseModel):
    path: str


class SearchBody(BaseModel):
    vector: list[float] = Field(max_length=_EMBED_DIM)
    k: int = Field(ge=1, le=_MAX_K)


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
    (mirrors `gateway/_agent_max_id.py:emit_max_agent_id`).
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


def build_app(store: MemoryStore) -> FastAPI:
    """Wire the store into a FastAPI app. One mutation lock serializes every
    operation (search included) — the store is pure in-memory state and a
    full exact scan is microseconds, so the lock is the whole concurrency
    story at this scale. The stats flusher runs as a lifespan task so the
    metrics stream lives and dies with the serving process."""
    lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        flusher = asyncio.create_task(_stats_flusher(store, lock))
        try:
            yield
        finally:
            flusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await flusher

    app = FastAPI(title="ava-memory-search", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/stats")
    async def stats() -> dict[str, int | float | None]:
        async with lock:
            return {"rows": len(store), "last_save_seconds": store.last_save_seconds}

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
            from fastapi import HTTPException

            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "ok"}

    @app.post("/delete")
    async def delete(body: DeleteBody) -> dict[str, str]:
        async with lock:
            store.delete(body.path)
            await asyncio.to_thread(store.save)
        return {"status": "ok"}

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
