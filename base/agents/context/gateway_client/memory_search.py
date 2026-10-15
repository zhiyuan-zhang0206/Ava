"""Explicit-context gateway memory search and its client-side result contract."""

from __future__ import annotations

from collections.abc import Collection
from typing import NamedTuple

from base.agents.context import AvaContext
from base.api_contracts.memory_search import MemorySearchPurpose

from . import transport

# ── memory search: its own timeout and retry budget ──
# `/api/memory/search` stands apart from the other routes: the gateway gives
# it a server-side deadline (`memory_search_deadline_seconds`, default 15s)
# and answers a *modelled* 503 (reason indexer_unavailable) when it expires —
# or in ~1s when the query-embed gate is congested (task #2003/E). So the
# default SDK budget (20s x 3 attempts + backoff) spends agent time re-sending
# a request whose failure the gateway already answered; on the 2026-08-29
# fleet-wake storm that was the ~27s overhang measured behind search (#2003/A).
# Two attempts (one retry) still ride out a transient blip (the 2026-08-07
# indexer-500 class, task #960) without stacking the default 3; on a
# persistent outage the caller decides how long it is worth.
MEMORY_SEARCH_MAX_RETRIES = 2
# Per-attempt HTTP timeout must stay ABOVE the gateway's own search deadline:
# the server's wire 503 is the informative failure (IndexerUnavailable), and a
# client ReadTimeout that beats it converts the error into a generic
# GatewayUnavailable — the exact "caller reads out first" case the deadline
# setting's description warns about.
MEMORY_SEARCH_TIMEOUT_MARGIN_S = 3.0


def memory_search_timeout(context: AvaContext) -> transport.TransportTimeout:
    """Per-attempt HTTP timeout for one memory search: the gateway's own
    search deadline plus the margin, so the server's deadline fires first."""
    import httpx

    budget = (
        context.clients.get(transport.GatewayTransportInputs).memory_deadline_reader()
        + MEMORY_SEARCH_TIMEOUT_MARGIN_S
    )
    return httpx.Timeout(budget)


class MemorySearchResult(NamedTuple):
    """One memory-search hit as the SDK boundary sees it.

    The single client-side shape for a search result, so every consumer
    (`ava.memory.search`, passive recall) reads the same fields instead of
    re-parsing the wire dict — a new field added here flows to all of them.

    path: memory-pool-relative path of the note.
    description: the note's frontmatter `description`, extracted server-side;
        empty string when the note has none (never synthesized from title/body).
    tags: the note's frontmatter `tags`, including its `type/<x>`; empty when it
        has none. What lets a caller weigh a hit by the kind of note it is, not
        just by how it scored.
    score: the ranking model's probability (0..1) that `chunk` is relevant to
        the query.
    chunk: the text of the note's best-matching chunk, or its description line.
    """

    path: str
    description: str
    tags: tuple[str, ...]
    score: float
    chunk: str


def memory_search(
    query: str,
    k: int,
    *,
    purpose: MemorySearchPurpose,
    exclude: Collection[str] = (),
    timeout: float | None = None,
    attempts: int = MEMORY_SEARCH_MAX_RETRIES,
    context: AvaContext,
) -> list[MemorySearchResult]:
    """POST /api/memory/search → list of `MemorySearchResult`, most relevant first.

    The gateway embeds the query, retrieves candidate chunks from the memory-search
    service, and has a decision model score each chunk's relevance (see
    `MemorySearchResult`). Returns relative paths with frontmatter
    descriptions extracted server-side, so callers do not need to
    re-read every file to get a summary.

    Semantically a read (idempotent): a transient backend 5xx (the
    2026-08-07 memory-indexer 500 class) is retried once with backoff +
    jitter before surfacing, so a blip self-heals instead of reaching the
    caller — and a persistent failure (the gateway's own deadline 503, a
    congested gate) surfaces as `IndexerUnavailable` without stacking
    attempts behind it.

    `purpose` says why the query was issued ("recall": passive recall, the query is the recent
    conversation; "search": an agent's own search; "dedup": the duplicate check on a note just
    written, the query is that note), which selects the question the gateway's ranking model
    answers.

    `exclude` lists memory-pool-relative note paths the gateway leaves out before ranking: they
    cost no ranking request and take no candidate slot. At most `k` results come back, and never
    more than the notes the gateway ranks per search.

    `timeout` is how long one attempt may take (seconds). It travels in the request, so the gateway
    stops ranking when it passes: a call whose caller has stopped waiting must not keep spending
    ranking requests. The HTTP timeout is `timeout` plus a 3s margin, so the gateway's answer
    (`IndexerUnavailable`) arrives before the client reads out and reports `GatewayUnavailable`.
    The default is no request deadline, and the gateway's own search deadline plus the margin
    (18s at default settings) as the HTTP timeout.

    `attempts` is how many times a transient failure is tried in all (default 2: one retry). A
    caller that gives up on its own deadline passes 1: a retry would start a second search after
    the caller has stopped waiting for the first.
    """
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
