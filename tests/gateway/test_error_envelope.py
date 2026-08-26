"""Gateway-wide RFC 9457-style error-envelope contract tests."""

from __future__ import annotations

import asyncio
import json
import logging
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

import gateway._auth401_log as auth401_log
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


def _request(
    *,
    method: str = "GET",
    path: str = "/api/agents",
    headers: list[tuple[bytes, bytes]] | None = None,
    client: tuple[str, int] | None = ("127.0.0.1", 1),
) -> Request:
    """Build the smallest request shape accepted by the direct middleware calls."""
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers or [],
            "client": client,
            "server": ("testserver", 80),
            "state": {},
        }
    )


@pytest.fixture(autouse=True)
def _clear_auth401_throttle_state() -> Iterator[None]:
    """Keep process-local auth-401 throttle state from coupling tests."""
    auth401_log._auth401_last_warn.clear()
    auth401_log._auth401_suppressed.clear()
    yield
    auth401_log._auth401_last_warn.clear()
    auth401_log._auth401_suppressed.clear()


def _enable_cluster_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", "test-secret")


def _unauthorized_auth_response(request: Request) -> Response:
    async def call_next(_request: Request) -> Response:
        raise AssertionError("authentication middleware must short-circuit")

    return asyncio.run(_cluster_auth_middleware(request, call_next))


def _auth401_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == "gateway._auth401_log" and record.getMessage().startswith("auth 401:")
    ]


def test_auth_middleware_uses_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unauthorized short-circuits remain 401 but expose the shared contract."""
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", "test-secret")

    async def call_next(_request: Request) -> Response:
        raise AssertionError("authentication middleware must short-circuit")

    response = asyncio.run(_cluster_auth_middleware(_request(), call_next))
    _assert_envelope(response, status=401, code="authentication_required", retryable=False)


def test_auth_middleware_requires_cluster_auth_for_alert_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The webhook exemption must not expose the alerts read API."""
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", "test-secret")

    async def call_next(_request: Request) -> Response:
        raise AssertionError("unauthenticated alert reads must short-circuit")

    response = asyncio.run(_cluster_auth_middleware(_request(path="/api/alerts"), call_next))
    _assert_envelope(response, status=401, code="authentication_required", retryable=False)


def test_auth_middleware_leaves_alert_webhook_auth_to_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the POST alert webhook reaches its router without cluster auth."""
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", "test-secret")

    async def call_next(_request: Request) -> Response:
        return Response(status_code=204)

    response = asyncio.run(
        _cluster_auth_middleware(
            _request(method="POST", path="/api/alerts"),
            call_next,
        )
    )
    assert response.status_code == 204


def test_auth_middleware_accepts_bearer_for_alert_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authenticated alert reads continue through ordinary cluster auth."""
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", "test-secret")

    async def call_next(_request: Request) -> Response:
        return Response(status_code=204)

    response = asyncio.run(
        _cluster_auth_middleware(
            _request(
                path="/api/alerts",
                headers=[(b"authorization", b"Bearer test-secret")],
            ),
            call_next,
        )
    )
    assert response.status_code == 204


@pytest.mark.parametrize(
    "path",
    [
        "/api/alerts/stream",
        "/api/system/all",
        "/api/agents/1/events/stream",
        "/api/system",
        "/api/agents/1/system",
    ],
)
def test_auth_middleware_logs_sse_poll_401_at_debug(
    path: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Stale EventSource reconnects stay forensic-only without changing the response."""
    _enable_cluster_auth(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="gateway._auth401_log")

    response = _unauthorized_auth_response(
        _request(path=path, headers=[(b"user-agent", b"stale-browser")])
    )
    _assert_envelope(response, status=401, code="authentication_required", retryable=False)
    records = _auth401_records(caplog)
    assert [record.levelno for record in records] == [logging.DEBUG]
    assert f"path={path}" in records[0].getMessage()
    assert "client=127.0.0.1" in records[0].getMessage()
    assert "ua=stale-browser" in records[0].getMessage()


def test_auth_middleware_warns_on_first_non_stream_401(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A new client/path source remains visible once at WARNING."""
    _enable_cluster_auth(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="gateway._auth401_log")

    response = _unauthorized_auth_response(_request(headers=[(b"user-agent", b"curl/8.1")]))
    _assert_envelope(response, status=401, code="authentication_required", retryable=False)
    records = _auth401_records(caplog)
    assert [record.levelno for record in records] == [logging.WARNING]
    assert "path=/api/agents" in records[0].getMessage()
    assert "client=127.0.0.1" in records[0].getMessage()
    assert "ua=curl/8.1" in records[0].getMessage()


def test_auth_middleware_suppresses_immediate_non_stream_401_repeat(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A repeated client/path key is downgraded and counted during cooldown."""
    _enable_cluster_auth(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="gateway._auth401_log")
    request = _request()

    _unauthorized_auth_response(request)
    caplog.clear()
    response = _unauthorized_auth_response(request)

    _assert_envelope(response, status=401, code="authentication_required", retryable=False)
    records = _auth401_records(caplog)
    assert [record.levelno for record in records] == [logging.DEBUG]
    assert "suppressed" in records[0].getMessage()
    assert auth401_log._auth401_suppressed[("127.0.0.1", "/api/agents")] == 1


def test_auth_middleware_warns_after_cooldown_with_suppressed_count(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The next warning reports repeats hidden during the elapsed cooldown."""
    _enable_cluster_auth(monkeypatch)
    now = [100.0]
    monkeypatch.setattr(auth401_log.time, "monotonic", lambda: now[0])
    caplog.set_level(logging.DEBUG, logger="gateway._auth401_log")
    request = _request()

    _unauthorized_auth_response(request)
    _unauthorized_auth_response(request)
    now[0] = 401.0
    caplog.clear()
    response = _unauthorized_auth_response(request)

    _assert_envelope(response, status=401, code="authentication_required", retryable=False)
    records = _auth401_records(caplog)
    assert [record.levelno for record in records] == [logging.WARNING]
    assert "suppressed 1 repeats in the last 300s" in records[0].getMessage()


def test_auth_middleware_prunes_idle_401_throttle_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client/path key idle for two windows stops consuming throttle state."""
    _enable_cluster_auth(monkeypatch)
    now = [100.0]
    monkeypatch.setattr(auth401_log.time, "monotonic", lambda: now[0])
    stale_key = ("127.0.0.1", "/api/agents")

    _unauthorized_auth_response(_request())
    _unauthorized_auth_response(_request())
    now[0] = 701.0
    response = _unauthorized_auth_response(_request(path="/api/alerts"))

    _assert_envelope(response, status=401, code="authentication_required", retryable=False)
    assert stale_key not in auth401_log._auth401_last_warn
    assert stale_key not in auth401_log._auth401_suppressed


def test_auth_middleware_throttles_non_stream_401s_per_client_and_path(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A warning for one client/path key does not hide either neighboring key."""
    _enable_cluster_auth(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="gateway._auth401_log")
    _unauthorized_auth_response(_request())
    caplog.clear()

    responses = [
        _unauthorized_auth_response(_request(client=("127.0.0.2", 1))),
        _unauthorized_auth_response(_request(path="/api/alerts")),
    ]

    for response in responses:
        _assert_envelope(
            response,
            status=401,
            code="authentication_required",
            retryable=False,
        )
    records = _auth401_records(caplog)
    assert [record.levelno for record in records] == [logging.WARNING, logging.WARNING]
    messages = [record.getMessage() for record in records]
    assert any("path=/api/agents client=127.0.0.2" in message for message in messages)
    assert any("path=/api/alerts client=127.0.0.1" in message for message in messages)


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
