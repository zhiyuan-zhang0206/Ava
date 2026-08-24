"""Exception-to-envelope adapters registered by the gateway application."""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from gateway import loki_events, loki_query_budget, prom_metrics
from gateway._cors import cors_allowed_origins
from gateway.error_envelope import error_response
from shared.agents import AvaAgentError

_log = logging.getLogger(__name__)


async def _ava_agent_error_handler(request: Request, exc: AvaAgentError) -> JSONResponse:
    """Map an SDK-compatible agent error to its stable reason-bearing envelope."""
    return error_response(
        request,
        code=exc.reason.value,
        status=exc.http_status,
        detail=str(exc),
        retryable=False,
        reason=exc.reason,
    )


def _cors_headers(request: Request) -> dict[str, str]:
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


async def _loki_query_budget_error_handler(
    request: Request,
    exc: loki_query_budget.LokiQueryBudgetError,
) -> JSONResponse:
    """Map local Loki admission saturation to one retriable wire contract."""
    return error_response(
        request,
        code="loki_query_budget_unavailable",
        status=503,
        detail=f"Loki query budget unavailable ({exc.reason}); retry",
        retryable=True,
        headers={"Retry-After": "1"},
    )


async def _observability_read_unavailable_handler(
    request: Request,
    exc: loki_events.ObservabilityReadUnavailable,
) -> JSONResponse:
    """Expose a non-LGTM gateway's deliberate read isolation as a clean 503."""
    return error_response(
        request,
        code="observability_read_unavailable",
        status=503,
        detail=str(exc),
        retryable=True,
    )


async def _prom_query_budget_error_handler(
    request: Request,
    exc: prom_metrics.PromQueryBudgetError,
) -> JSONResponse:
    """Map local Prometheus admission saturation to one retriable contract."""
    return error_response(
        request,
        code="prom_query_budget_unavailable",
        status=503,
        detail=f"Prometheus query budget unavailable ({exc.reason}); retry",
        retryable=True,
        headers={"Retry-After": "1"},
    )


async def _request_validation_error_handler(
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


async def _http_exception_handler(
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


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return a typed 500 and preserve CORS after an unhandled route exception."""
    _log.error("unhandled gateway exception", exc_info=exc)
    return error_response(
        request,
        code="internal_error",
        status=500,
        detail="Internal Server Error",
        retryable=False,
        headers=_cors_headers(request),
    )
