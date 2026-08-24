"""Gateway-wide RFC 9457-style error-envelope contract tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator

import httpx
import httpx2
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from gateway import loki_events, loki_query_budget, prom_metrics
from gateway._cors import cors_allowed_origins
from gateway.app import (
    _ava_agent_error_handler,
    _cluster_auth_middleware,
    _cluster_pause_middleware,
    _http_exception_handler,
    _loki_query_budget_error_handler,
    _observability_read_unavailable_handler,
    _prom_query_budget_error_handler,
    _request_validation_error_handler,
    _unhandled_exception_handler,
)
from gateway.error_envelope import request_trace_middleware
from gateway.routers._backend_failure import raise_backend_unavailable
from gateway.schemas import ErrorEnvelope
from shared import config
from shared.agents import AgentNotFound, AvaAgentError, ErrorReason


def _assert_envelope(
    response: httpx.Response | httpx2.Response | Response,
    *,
    status: int,
    code: str,
    retryable: bool,
    reason: str | None = None,
    extensions: set[str] | None = None,
) -> None:
    """Assert the common response contract without coupling extension values."""
    extensions = extensions or set()
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    body = json.loads(bytes(response.body)) if isinstance(response, Response) else response.json()
    assert set(body) == {
        "type",
        "code",
        "status",
        "detail",
        "retryable",
        "trace_id",
        *extensions,
        *({"reason"} if reason is not None else set()),
    }
    assert body["type"] == "about:blank"
    assert body["code"] == code
    assert body["status"] == status
    assert isinstance(body["detail"], str)
    assert body["retryable"] is retryable
    assert isinstance(body["trace_id"], str) and body["trace_id"]
    if reason is not None:
        assert body["reason"] == reason


@pytest.fixture
def handler_client() -> Iterator[TestClient]:
    """An isolated app that dispatches every shared gateway error handler."""
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=".*",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.middleware("http")(request_trace_middleware)
    app.add_exception_handler(AvaAgentError, _ava_agent_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(
        loki_query_budget.LokiQueryBudgetError,
        _loki_query_budget_error_handler,  # type: ignore[arg-type]
    )
    app.add_exception_handler(
        loki_events.ObservabilityReadUnavailable,
        _observability_read_unavailable_handler,  # type: ignore[arg-type]
    )
    app.add_exception_handler(
        prom_metrics.PromQueryBudgetError,
        _prom_query_budget_error_handler,  # type: ignore[arg-type]
    )
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _request_validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled_exception_handler)

    @app.get("/agent")
    def agent_error() -> None:
        raise AgentNotFound("agent missing")

    @app.get("/loki")
    def loki_error() -> None:
        raise loki_query_budget.LokiQueryBudgetError("queue_full")

    @app.get("/observability")
    def observability_error() -> None:
        raise loki_events.ObservabilityReadUnavailable("observability is unavailable")

    @app.get("/prom")
    def prom_error() -> None:
        raise prom_metrics.PromQueryBudgetError("queue_full")

    @app.get("/http/{status}")
    def http_error(status: int) -> None:
        raise HTTPException(status_code=status, detail=f"HTTP {status}")

    @app.get("/backend")
    def backend_error() -> None:
        raise_backend_unavailable(httpx.ConnectError("offline"), backend="loki")

    @app.get("/validation")
    def validation_error(count: int) -> None:
        del count

    @app.get("/exception")
    def unhandled_error() -> None:
        raise RuntimeError("boom")

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.mark.parametrize(
    ("path", "status", "code", "retryable", "reason", "extensions"),
    [
        ("/agent", 404, "agent_not_found", False, "agent_not_found", set[str]()),
        ("/loki", 503, "loki_query_budget_unavailable", True, None, set[str]()),
        ("/observability", 503, "observability_read_unavailable", True, None, set[str]()),
        ("/prom", 503, "prom_query_budget_unavailable", True, None, set[str]()),
        ("/http/404", 404, "http_404", False, None, set[str]()),
        ("/backend", 503, "http_503", True, None, set[str]()),
        ("/validation?count=not-an-int", 422, "validation_error", False, None, {"errors"}),
        ("/exception", 500, "internal_error", False, None, set[str]()),
    ],
)
def test_global_handlers_emit_typed_envelopes(
    handler_client: TestClient,
    path: str,
    status: int,
    code: str,
    retryable: bool,
    reason: str | None,
    extensions: set[str],
) -> None:
    """Every handler keeps its status while exposing one typed response shape."""
    origin = cors_allowed_origins()[0]
    response = handler_client.get(path, headers={"Origin": origin})
    _assert_envelope(
        response,
        status=status,
        code=code,
        retryable=retryable,
        reason=reason,
        extensions=extensions,
    )
    if path == "/backend":
        assert response.headers["retry-after"] == "1"
    if path == "/validation?count=not-an-int":
        assert isinstance(response.json()["errors"], list)
    if path == "/exception":
        assert response.headers["access-control-allow-origin"] == origin


def test_active_otel_trace_id_wins_over_request_fallback(
    handler_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Envelope correlation uses the active OTel trace when one exists."""
    from gateway import error_envelope

    monkeypatch.setattr(error_envelope.telemetry, "_capture_trace_ids", lambda: ("a" * 32, None))
    response = handler_client.get("/agent")
    assert response.json()["trace_id"] == "a" * 32


def _request() -> Request:
    """Build the smallest request shape accepted by the direct middleware calls."""
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/agents",
            "raw_path": b"/api/agents",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("testserver", 80),
            "state": {},
        }
    )


def test_auth_middleware_uses_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unauthorized short-circuits remain 401 but expose the shared contract."""
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", "test-secret")

    async def call_next(_request: Request) -> Response:
        raise AssertionError("authentication middleware must short-circuit")

    response = asyncio.run(_cluster_auth_middleware(_request(), call_next))
    _assert_envelope(response, status=401, code="authentication_required", retryable=False)


def test_cluster_pause_middleware_uses_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pause short-circuits remain retriable 503 responses with Retry-After."""
    import gateway.app as gateway_app

    async def paused(_request: Request) -> bool:
        return True

    async def call_next(_request: Request) -> Response:
        raise AssertionError("pause middleware must short-circuit")

    monkeypatch.setattr(gateway_app, "_cluster_is_paused", paused)
    response = asyncio.run(_cluster_pause_middleware(_request(), call_next))
    _assert_envelope(response, status=503, code="cluster_updating", retryable=True)
    assert response.headers["retry-after"] == "30"


def test_error_envelope_schema_round_trips_and_omits_unset_reason() -> None:
    """The schema preserves the six common fields and opt-in SDK reason field."""
    envelope = ErrorEnvelope(
        code="validation_error",
        status=422,
        detail="Request validation failed",
        retryable=False,
        trace_id="f" * 32,
    )
    dumped = envelope.model_dump(mode="json", exclude_none=True)
    assert "reason" not in dumped
    assert ErrorEnvelope.model_validate(dumped) == envelope

    agent_envelope = envelope.model_copy(
        update={"code": "agent_not_found", "reason": ErrorReason.AGENT_NOT_FOUND}
    )
    assert agent_envelope.model_dump(mode="json", exclude_none=True)["reason"] == "agent_not_found"
