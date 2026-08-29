"""The memory search HTTP API — FastAPI over the in-process MemoryStore.

Endpoints mirror the backend protocol one-to-one (upsert / delete / meta /
search), so `backends.numpy.NumPyBackend` is a thin HTTP client and the
indexer daemon + gateway treat this service exactly like the milvus
daemon. Every mutation persists the npz before responding, so a kill
-after-ack never loses a row.

Binds loopback only (the daemon passes host="127.0.0.1") — like milvus,
this is a strictly local service; no LAN port is opened.
"""

from __future__ import annotations

import asyncio

import numpy as np
from fastapi import FastAPI
from pydantic import BaseModel, Field

from services.memory_indexer.embedder import DIM
from services.memory_search.store import MemoryStore

# Loopback-only is the trust model, not a security boundary: any local
# process could still POST a huge body and drive an allocation, so the
# wire models carry hard size bounds. An embedding is exactly DIM floats;
# anything longer is malformed and rejected before numpy ever allocates.
_MAX_K = 1000


class UpsertBody(BaseModel):
    path: str
    mtime: float
    content_hash: str
    kind: str
    chunk_idx: int
    vector: list[float] = Field(max_length=DIM)


class DeleteBody(BaseModel):
    path: str


class SearchBody(BaseModel):
    vector: list[float] = Field(max_length=DIM)
    k: int = Field(ge=1, le=_MAX_K)


def build_app(store: MemoryStore) -> FastAPI:
    """Wire the store into a FastAPI app. One mutation lock serializes every
    operation (search included) — the store is pure in-memory state and a
    full exact scan is microseconds, so the lock is the whole concurrency
    story at this scale."""
    app = FastAPI(title="ava-memory-search")
    lock = asyncio.Lock()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

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
    async def meta() -> dict[str, tuple[float, str]]:
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
