"""Shared construction of typed gateway error responses."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import uuid4

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from gateway.schemas.errors import ErrorEnvelope
from shared import telemetry
from shared.agents import ErrorReason


def _trace_id(request: Request) -> str:
    """Return the active OTel trace id or this request's generated correlation id."""
    trace_id, _span_id = telemetry._capture_trace_ids()
    if trace_id is not None:
        return trace_id
    trace_id = getattr(request.state, "trace_id", None)
    if isinstance(trace_id, str):
        return trace_id
    trace_id = uuid4().hex
    request.state.trace_id = trace_id
    return trace_id


async def request_trace_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Bind a fallback correlation id before any gateway middleware can reject a request."""
    request.state.trace_id = _trace_id(request)
    return await call_next(request)


def error_response(
    request: Request,
    *,
    code: str,
    status: int,
    detail: str,
    retryable: bool,
    reason: ErrorReason | None = None,
    extensions: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Create the one JSON error shape used by all gateway API error paths."""
    content = ErrorEnvelope(
        code=code,
        status=status,
        detail=detail,
        retryable=retryable,
        trace_id=_trace_id(request),
        reason=reason,
    ).model_dump(mode="json", exclude_none=True)
    if extensions:
        overlap = set(content) & set(extensions)
        if overlap:
            raise ValueError(f"error envelope extensions duplicate fields: {sorted(overlap)}")
        content.update(extensions)
    return JSONResponse(
        status_code=status,
        content=content,
        headers=headers,
        media_type="application/problem+json",
    )
