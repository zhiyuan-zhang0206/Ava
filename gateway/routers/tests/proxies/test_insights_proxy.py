"""The gateway's insights proxy: what it forwards, what it refuses, how an outage reads."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from base.agents.history.timeline_inputs import TimelineReadInputs
from base.clock import Clock, ClockConfig
from base.db import Database
from base.lm.catalog import ModelCatalog
from gateway.agents import eval_guard
from gateway.routers import insights as proxy
from services.derived.insights.app import build_app
from services.derived.insights.config import InsightsConfig

_PROXIED = (
    "/api/agents/{agent_id}/run-timeline",
    "/api/agents/{agent_id}/run-timeline/messages",
    "/api/agents/{agent_id}/run-timeline/context",
    "/api/insights/run-timeline/links",
    "/api/insights/run-timeline/link-content",
)


def _upstream() -> FastAPI:
    """A stand-in service that echoes what it was asked."""
    app = FastAPI()

    @app.get("/{path:path}")
    async def echo(request: Request) -> JSONResponse:
        if request.url.path.endswith("/missing"):
            return JSONResponse({"detail": "gone"}, status_code=404, headers={"etag": "abc"})
        return JSONResponse(
            {
                "path": request.url.path,
                "query": request.url.query,
                "headers": {k.lower(): v for k, v in request.headers.items()},
            }
        )

    return app


def _gateway(transport: httpx.AsyncBaseTransport) -> FastAPI:
    app = FastAPI()
    app.include_router(proxy.router)
    app.state.insights_client = httpx.AsyncClient(transport=transport, base_url="http://insights")
    app.state.db_pool = object()
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(_gateway(httpx.ASGITransport(app=_upstream()))) as c:
        yield c


def test_a_typed_route_forwards_its_path_and_query_untouched(client: TestClient) -> None:
    body = client.get(
        "/api/agents/7/run-timeline?from=2026-10-08T00:00:00%2B08:00&to=2026-10-09T00:00:00Z"
    ).json()
    assert body["path"] == "/api/agents/7/run-timeline"
    assert body["query"] == "from=2026-10-08T00:00:00%2B08:00&to=2026-10-09T00:00:00Z"
    body = client.get("/api/agents/7/run-timeline/context?at=12").json()
    assert (body["path"], body["query"]) == ("/api/agents/7/run-timeline/context", "at=12")


def test_the_browsers_credentials_are_not_forwarded(client: TestClient) -> None:
    sent = client.get(
        "/api/agents/7/run-timeline/context?at=1",
        headers={"cookie": "session=secret", "authorization": "Bearer secret", "accept": "x/y"},
    ).json()["headers"]
    assert "cookie" not in sent
    assert "authorization" not in sent
    assert sent["accept"] == "x/y"


def test_the_gateway_rejects_what_its_declaration_rejects(client: TestClient) -> None:
    assert client.get("/api/agents/7/run-timeline/context").status_code == 422
    assert client.get("/api/agents/x/run-timeline").status_code == 422


def test_an_untyped_insights_path_is_forwarded_and_the_service_status_passes_through(
    client: TestClient,
) -> None:
    ok = client.get("/api/insights/cluster/usage?window=7d").json()
    assert (ok["path"], ok["query"]) == ("/api/insights/cluster/usage", "window=7d")
    gone = client.get("/api/insights/missing")
    assert gone.status_code == 404
    assert gone.json() == {"detail": "gone"}
    assert gone.headers["etag"] == "abc"


def test_a_path_that_could_address_another_route_is_refused(client: TestClient) -> None:
    assert client.get("/api/insights/a/%2E%2E/b").status_code == 400
    assert client.get("/api/insights/a%5Cb").status_code == 400


def test_an_eval_isolated_caller_is_refused_before_the_service_is_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(_transport_request: httpx.Request) -> httpx.Response:
        raise AssertionError("the service must not be reached")

    def isolated_caller(_pool: object, _agent: int) -> bool:
        return True

    monkeypatch.setattr(eval_guard, "caller_eval_isolation", isolated_caller)
    with TestClient(_gateway(httpx.MockTransport(refuse))) as isolated:
        for path in (
            "/api/agents/7/run-timeline/context?at=1&caller=agent:5",
            "/api/insights/x?caller=agent:5",
        ):
            assert isolated.get(path).status_code == 403


@pytest.mark.parametrize(
    ("failure", "status"),
    [(httpx.ConnectError("no socket"), 502), (httpx.ReadTimeout("slow"), 504)],
)
def test_an_unavailable_service_is_a_gateway_error_not_a_crash(
    failure: httpx.HTTPError, status: int
) -> None:
    def fail(_request: httpx.Request) -> httpx.Response:
        raise failure

    with TestClient(_gateway(httpx.MockTransport(fail))) as down:
        assert down.get("/api/agents/7/run-timeline").status_code == status


def test_the_gateway_declares_exactly_what_the_service_serves(
    *, model_catalog: ModelCatalog
) -> None:
    """Same query parameters and response schema per proxied route, so the OpenAPI contract
    (and the frontend types generated from it) cannot drift from the service."""
    service = build_app(
        cast(Database, object()),
        cast(ConnectionPool[Any], object()),
        InsightsConfig(run_timeline_message_text_max=1),
        catalog=model_catalog,
        default_model_reader=lambda: "deepseek-v4-flash-vision-exp",
        timeline_inputs=TimelineReadInputs(
            lambda: Clock(ClockConfig("UTC", "UTC", False)), lambda: False
        ),
    ).openapi()
    gateway = _gateway(httpx.MockTransport(lambda _r: httpx.Response(200))).openapi()

    def contract(spec: dict[str, Any], path: str) -> tuple[Any, Any]:
        get = spec["paths"][path]["get"]
        params = sorted(
            (p["name"], p["in"], p.get("required", False), str(p["schema"]))
            for p in get["parameters"]
        )
        return params, get["responses"]["200"]["content"]["application/json"]["schema"]

    for path in _PROXIED:
        assert contract(gateway, path) == contract(service, path), path
    assert {p for p in service["paths"] if "run-timeline" in p} == set(_PROXIED)
