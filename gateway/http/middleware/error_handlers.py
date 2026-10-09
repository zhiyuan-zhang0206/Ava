"""Exception-to-envelope adapters registered by the gateway application."""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from base.agents import AgentLaunchFailed, AvaAgentError
from base.agents.messages.chat_delivery import ChatInboundCommittedError
from gateway.http.auth.cors import cors_allowed_origins
from gateway.http.middleware.error_envelope import error_response

_log = logging.getLogger(__name__)


async def ava_agent_error_handler(request: Request, exc: AvaAgentError) -> JSONResponse:
    """Map an SDK-compatible agent error to its stable reason-bearing envelope."""
    return error_response(
        request,
        code=exc.reason.value,
        status=exc.http_status,
        detail=str(exc),
        retryable=False,
        reason=exc.reason,
        extensions=(
            {
                "agent_id": exc.agent_id,
                "state": exc.state,
                **(
                    {"retry_launch_path": exc.retry_launch_path}
                    if exc.retry_launch_path is not None
                    else {}
                ),
            }
            if isinstance(exc, AgentLaunchFailed)
            else None
        ),
    )


def cors_headers(request: Request) -> dict[str, str]:
    """Return CORS headers for an allowlisted request origin, else no headers.

    ServerErrorMiddleware — which answers unhandled exceptions — sits OUTSIDE
    every user middleware (Starlette/FastAPI build order), so its response
    never passes through CORSMiddleware and the catch-all handler below must
    add the headers itself. Mirrors CORSMiddleware's simple-response behavior
    for this gateway's exact-origin configuration (#187).
    """
    origin = request.headers.get("origin")
    if origin is None or origin not in cors_allowed_origins():
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Vary": "Origin",
        "Access-Control-Allow-Credentials": "true",
    }


async def request_validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Normalize FastAPI's structured 422 body without losing its field errors."""
    return error_response(
        request,
        code="validation_error",
        status=422,
        detail="Request validation failed",
        retryable=False,
        extensions={"errors": jsonable_encoder(exc.errors())},
    )


async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    """Map every router-raised HTTPException into the common error envelope."""
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return error_response(
        request,
        code=f"http_{exc.status_code}",
        status=exc.status_code,
        detail=detail,
        retryable=exc.status_code in {429, 503},
        headers=exc.headers,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return a typed 500 and preserve CORS after an unhandled route exception."""
    _log.error("unhandled gateway exception", exc_info=exc)
    return error_response(
        request,
        code="internal_error",
        status=500,
        detail=(
            "Chat inbound committed; post-commit work failed. Reconcile with the same logical key."
            if isinstance(exc, ChatInboundCommittedError)
            else "Internal Server Error"
        ),
        retryable=False,
        extensions=(
            {
                "committed": True,
                "inbound_id": exc.receipt.inbound_id,
                "idempotency_key": request.headers.get("Idempotency-Key"),
            }
            if isinstance(exc, ChatInboundCommittedError)
            else None
        ),
        headers=cors_headers(request),
    )
