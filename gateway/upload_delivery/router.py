"""Strong versioned delivered uploads; legacy consumers are not activated here."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Header, HTTPException, Path, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import ValidationError

from base.agents.upload_delivery import source
from base.agents.upload_delivery.models import (
    Acceptance,
    DeliveryStatus,
    UploadDeliveryConflictError,
    UploadQuotaExceededError,
)
from base.agents.upload_delivery.paths import render_safe_headers
from gateway.agents.inbound_provenance import request_inbound_provenance
from gateway.http.auth.request_principal import (
    PRINCIPAL_SCOPE,
    SCOPE_HEADER,
    AuthPrincipal,
    principal_key,
)
from gateway.routers.upload.router import read_upload_batch

router = APIRouter()


def operation_key(
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    idempotency_scope: str = Header(alias=SCOPE_HEADER),
) -> str:
    principal = getattr(request.state, "auth_principal", None)
    if idempotency_scope != PRINCIPAL_SCOPE or not isinstance(principal, AuthPrincipal):
        raise HTTPException(422, "delivered uploads require verified principal-v1 scope")
    return principal_key(principal, request.method, request.url.path, idempotency_key)


@router.post("/api/keyed/v1/agents/{agent_id}/uploads", status_code=202)
async def upload(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)],
    request: Request,
    key: Annotated[str, Depends(operation_key)],
    files: Annotated[list[UploadFile], File()],
) -> Acceptance:
    batch, _ = await read_upload_batch(files)
    names = [file.filename or "untitled" for file in files]
    try:
        return await request.app.state.upload_recovery.native(
            source.accept,
            request.app.state.db_pool,
            key,
            agent_id,
            names,
            batch,
            request_inbound_provenance(request),
        )
    except UploadDeliveryConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except UploadQuotaExceededError as exc:
        raise HTTPException(413, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(422, "invalid immutable upload manifest") from exc


@router.get("/api/keyed/v1/agents/{agent_id}/uploads/{batch_id}")
async def status(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)],
    request: Request,
    batch_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
) -> DeliveryStatus:
    return await request.app.state.upload_recovery.native(
        source.status, request.app.state.db_pool, agent_id, batch_id
    )


@router.get("/api/keyed/v1/agents/{agent_id}/uploads/{batch_id}/objects/{ordinal}")
async def object_file(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)],
    request: Request,
    batch_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
    ordinal: Annotated[int, Path(ge=0)],
) -> FileResponse:
    try:
        path, item = await request.app.state.upload_recovery.native(
            source.served_object, request.app.state.db_pool, agent_id, batch_id, ordinal
        )
    except UploadDeliveryConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    headers = render_safe_headers(item.name)
    # FileResponse safely encodes the original display name for attachment.
    headers.pop("Content-Disposition", None)
    return FileResponse(
        path,
        media_type=item.content_type,
        filename=item.filename,
        headers=headers,
    )
