"""Argument validation of the memory plugin's entry points (`search`)."""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest

from meiva.gateway_client import use_client
from meiva_builtins.plugins.meiva_memory import sdk as memory_plugin


@pytest.fixture
def gateway_requests() -> Iterator[list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/memory/search"
        return httpx.Response(200, json={"results": []})

    with (
        httpx.Client(
            base_url="http://gateway.test", transport=httpx.MockTransport(respond)
        ) as client,
        use_client(client),
    ):
        yield requests


class TestMemoryEntries:
    def test_search_query_unwraps(self, gateway_requests: list[httpx.Request]) -> None:
        memory_plugin.search_unwrapped(("query",))  # pyright: ignore[reportArgumentType]
        [request] = gateway_requests
        assert json.loads(request.content) == {"query": "query", "k": 5, "purpose": "search"}

    def test_search_query_multi_element_type_errors(
        self, gateway_requests: list[httpx.Request]
    ) -> None:
        with pytest.raises(TypeError, match="query must be a string"):
            memory_plugin.search_unwrapped(("a", "b"))  # pyright: ignore[reportArgumentType]
        assert gateway_requests == []
