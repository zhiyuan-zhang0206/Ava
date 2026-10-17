from __future__ import annotations

from collections.abc import Collection
from typing import NamedTuple

from base.agents.context import MeivaContext
from base.api_contracts.memory_search import MemorySearchPurpose

from . import transport

MEMORY_SEARCH_MAX_RETRIES = 2


MEMORY_SEARCH_TIMEOUT_MARGIN_S = 3.0


# meiva:ref base/agents/context/gateway_client/docs/memory-search.meiva.okf.md#default-timeout
def memory_search_timeout(context: MeivaContext) -> transport.TransportTimeout:

    import httpx

    budget = (
        context.clients.get(transport.GatewayTransportInputs).memory_deadline_reader()
        + MEMORY_SEARCH_TIMEOUT_MARGIN_S
    )
    return httpx.Timeout(budget)


# meiva:ref base/agents/context/gateway_client/docs/memory-search.meiva.okf.md#wire-results
class MemorySearchResult(NamedTuple):
    path: str
    description: str
    tags: tuple[str, ...]
    score: float
    chunk: str


# meiva:ref base/agents/context/gateway_client/docs/memory-search.meiva.okf.md#request-budget
def memory_search(
    query: str,
    k: int,
    *,
    purpose: MemorySearchPurpose,
    exclude: Collection[str] = (),
    timeout: float | None = None,
    attempts: int = MEMORY_SEARCH_MAX_RETRIES,
    context: MeivaContext,
) -> list[MemorySearchResult]:

    import httpx

    body: dict[str, object] = {"query": query, "k": k, "purpose": purpose}
    if exclude:
        body["exclude"] = sorted(exclude)
    if timeout is not None:
        body["deadline_seconds"] = timeout
    resp = transport.post(
        "/api/memory/search",
        body,
        timeout=(
            httpx.Timeout(timeout + MEMORY_SEARCH_TIMEOUT_MARGIN_S)
            if timeout is not None
            else memory_search_timeout(context)
        ),
        max_retries=attempts,
        context=context,
    )
    transport.raise_from_response(resp)
    return [
        MemorySearchResult(
            path=r["path"],
            description=r["description"],
            tags=tuple(r["tags"]),
            score=float(r["score"]),
            chunk=r["chunk"],
        )
        for r in resp.json()["results"]
    ]
