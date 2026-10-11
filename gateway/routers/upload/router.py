"""File upload endpoints — /api/agents/{agent_id}/uploads.

POST saves each uploaded file silently and returns each file's reference url —
the native-image-attachment path, where the frontend then sends the url inside
the next multimodal message. It requires `Idempotency-Key` and
`Idempotency-Scope: principal-v1`: the batch is claimed under that identity, then
published as immutable objects, and a retry of the same key replays the receipt.
Delivering a batch to the agent is `POST /api/keyed/v1/agents/{id}/uploads`
(`gateway/upload_delivery/router.py`).

GET serves a saved file back (image thumbnails in the timeline, and any client
that wants to fetch an upload by its reference url).
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from base.agents import AgentNotFound
from base.agents.upload_delivery.paths import (
    MAX_AGENT_UPLOAD_BYTES,
    MAX_AGENT_UPLOAD_FILES,
    MAX_UPLOAD_BYTES,
    agent_upload_dir,
    render_safe_headers,
    resolve_upload_path,
    sanitize_upload_name,
)
from base.db import agent_exists
from gateway.http.auth.request_principal import (
    PRINCIPAL_SCOPE,
    SCOPE_HEADER,
    AuthPrincipal,
    principal_key,
)
from gateway.routers.upload.batches import save_keyed_batch
from gateway.schemas.uploads import UploadedBatch

router = APIRouter()

type _UploadBatchItem = tuple[str, bytes, str]


def operation_key(
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    idempotency_scope: str = Header(alias=SCOPE_HEADER),
) -> str:
    """The stored identity of one upload operation: verified principal, route and caller key."""
    principal = getattr(request.state, "auth_principal", None)
    if idempotency_scope != PRINCIPAL_SCOPE or not isinstance(principal, AuthPrincipal):
        raise HTTPException(422, "uploads require verified principal-v1 scope")
    return principal_key(principal, request.method, request.url.path, idempotency_key)


def _per_file_quota_error(safe_name: str) -> HTTPException:
    return HTTPException(
        status_code=413,
        detail=f"upload {safe_name!r} exceeds the {MAX_UPLOAD_BYTES:,}-byte per-file limit",
    )


async def read_upload_batch(files: list[UploadFile]) -> list[_UploadBatchItem]:
    """Read and size-check every file before the batch can write a final name."""
    batch: list[_UploadBatchItem] = []
    for file in files:
        safe_name = sanitize_upload_name(file.filename or "untitled")
        contents = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(contents) > MAX_UPLOAD_BYTES:
            raise _per_file_quota_error(safe_name)
        batch.append((safe_name, contents, file.content_type or "application/octet-stream"))
    return batch


@router.post("/api/agents/{agent_id}/uploads", response_model=UploadedBatch)
async def upload_files(
    agent_id: int,
    request: Request,
    key: Annotated[str, Depends(operation_key)],
    files: list[UploadFile] = File(...),  # noqa: B008
    deliver: bool = Query(default=False),  # noqa: FBT001 — FastAPI query param, not a flag arg
) -> UploadedBatch:
    """Save one or more files silently and return each file's reference url.

    No inbound is queued: the caller carries the returned urls into a later
    multimodal message. `deliver=true` is refused; delivering a batch to the
    agent is `POST /api/keyed/v1/agents/{id}/uploads`.

    response_model makes FastAPI validate the return + emit the schema into
    OpenAPI components; the frontend codegen auto-syncs to types-generated.ts."""

    if deliver:
        raise HTTPException(
            422,
            "this route saves silently; deliver a batch with POST /api/keyed/v1/agents/{id}/uploads",
        )
    batch = await read_upload_batch(files)
    return await asyncio.to_thread(
        save_keyed_batch,
        request.app.state.db_pool,
        key,
        agent_id,
        agent_upload_dir(agent_id, create=False),
        batch,
        [file.filename or "untitled" for file in files],
        max_bytes=MAX_AGENT_UPLOAD_BYTES,
        max_files=MAX_AGENT_UPLOAD_FILES,
    )


@router.get("/api/agents/{agent_id}/uploads/{filename}")
def serve_upload(agent_id: int, filename: str, request: Request) -> FileResponse:
    """Serve one saved upload back (image thumbnails in the timeline).

    Traversal-safe: `resolve_upload_path` refuses any name escaping the agent's
    upload dir. 404 when the agent or the file does not exist.
    """
    with request.app.state.db_pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise AgentNotFound(agent_id)
    try:
        path = resolve_upload_path(agent_id, filename)
    except ValueError as e:
        # A traversal attempt is a bad request, not a server error.
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"upload {filename!r} not found")
    return FileResponse(path, headers=render_safe_headers(filename))
