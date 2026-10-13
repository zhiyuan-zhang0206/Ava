"""The HTTP route and the disconnect race around the service call."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from base.api_contracts.agent_contract import AgentNotFound
from base.lm.catalog import ModelCatalog
from gateway.agents.reply_suggestion.router import router, run_until_disconnect
from gateway.agents.reply_suggestion.schemas import ReplySuggestionResponse
from gateway.agents.reply_suggestion.tests.support import Harness


class StubService:
    def __init__(self, result: ReplySuggestionResponse | Exception) -> None:
        self.result = result
        self.agent_ids: list[int] = []

    async def suggest(self, agent_id: int) -> ReplySuggestionResponse:
        self.agent_ids.append(agent_id)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def client_for(service: StubService) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.reply_suggestions = service
    return TestClient(app)


def test_the_route_returns_the_services_suggestion() -> None:
    service = StubService(ReplySuggestionResponse(suggestion="Yes, go ahead."))

    response = client_for(service).post("/api/agents/7/reply-suggestion")

    assert response.status_code == 200
    assert response.json() == {"suggestion": "Yes, go ahead.", "reason": None}
    assert service.agent_ids == [7]


def test_the_route_reports_why_there_is_no_suggestion_as_a_200() -> None:
    service = StubService(ReplySuggestionResponse(reason="not_awaiting_reply"))

    response = client_for(service).post("/api/agents/7/reply-suggestion")

    assert response.status_code == 200
    assert response.json() == {"suggestion": None, "reason": "not_awaiting_reply"}


def test_a_non_integer_agent_id_is_rejected() -> None:
    response = client_for(StubService(ReplySuggestionResponse())).post(
        "/api/agents/abc/reply-suggestion"
    )

    assert response.status_code == 422


def test_the_route_does_not_swallow_an_unknown_agent() -> None:
    service = StubService(AgentNotFound("agent 404 does not exist"))

    with pytest.raises(AgentNotFound):
        client_for(service).post("/api/agents/404/reply-suggestion")


async def test_the_work_result_is_returned_while_the_client_stays() -> None:
    async def work() -> str:
        await asyncio.sleep(0.01)
        return "done"

    async def connected() -> bool:
        return False

    assert await run_until_disconnect(work(), connected) == "done"


async def test_the_work_exception_reaches_a_connected_client() -> None:
    async def work() -> str:
        raise ValueError("boom")

    async def connected() -> bool:
        return False

    with pytest.raises(ValueError, match="boom"):
        await run_until_disconnect(work(), connected)


async def test_a_disconnect_cancels_the_work() -> None:
    cancelled = asyncio.Event()

    async def work() -> str:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "never"

    async def gone() -> bool:
        return True

    assert await run_until_disconnect(work(), gone) is None
    assert cancelled.is_set()


async def test_a_disconnect_stops_a_draft_that_is_in_flight(model_catalog: ModelCatalog) -> None:
    harness = Harness(model_catalog)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_ainvoke(messages: Any) -> Any:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    harness.chat.ainvoke = slow_ainvoke  # pyright: ignore[reportAttributeAccessIssue]

    async def disconnects_once_drafting() -> bool:
        await started.wait()
        return True

    result = await run_until_disconnect(harness.service.suggest(7), disconnects_once_drafting)

    assert result is None
    assert cancelled.is_set()
