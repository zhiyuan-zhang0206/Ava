"""Memory endpoints — /api/memory/search + /api/memory/refresh + /api/memory/graph.

`search` (ava.memory.search SDK target) returns relative paths under
memory_root; the SDK caller prepends `ava.memory.PATH` to reconstruct the
absolute path (fs-neutral so gateway and agent-runner filesystems can
differ).

`refresh` fast-forwards the gateway memory checkout to the consolidated
pool (`origin/main`); the memory indexer then re-embeds the changed files.

`graph` returns concept notes and cross-links as a graph for the frontend
OKF knowledge-graph page.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from gateway.routers._eval_guard import deny_isolated_result_read
from gateway.schemas import (
    MemoryGraphEdge,
    MemoryGraphNode,
    MemoryGraphResponse,
    MemoryRefreshResponse,
    MemorySearchRequest,
    MemorySearchResponse,
    MemorySearchResultItem,
)
from shared.agents import IndexerUnavailable
from shared.config import settings
from shared.notes import Note, extract_md_links, parse_note, walk_notes
from shared.paths import gateway_memory_dir

router = APIRouter()

# Both phases of the search handler are native async I/O (httpx.AsyncClient,
# pymilvus AsyncMilvusClient); the semaphore caps in-flight Gemini requests so
# a burst of searches cannot pile up. Before this, the handler ran a sync
# embed call on the event loop and a slow Gemini API froze the whole gateway
# for up to ~4.5 minutes (60s httpx timeout x 4 attempts including sleep
# backoff) — 13 gateway restarts in 8h on 2026-08-03, 3 of the 5 examined
# freezes ended with a gemini-embedding POST as the last MainThread log line.
#
# A capped semaphore only helps while permits come back. Every wait under it
# must therefore be bounded — see `post_memory_search`'s deadline. Holding a
# permit across an unbounded await is what turned a stalled backend into a
# dead endpoint later the same day: two permits pinned, every later request
# parked forever in acquire, and each agent's passive recall wedged with it.
_MEMORY_SEARCH_SEMAPHORE = asyncio.Semaphore(2)


# Files reserved by OKF spec — excluded from concept graph
_RESERVED_NAMES = frozenset({"index.md", "log.md", "MEMORY.md"})


def _build_memory_graph(memory_root: Path) -> MemoryGraphResponse:
    """Build a concept graph from the memory pool on the gateway filesystem.

    Scans recursively so notes in subdirectories (health/, ava/, etc.) are
    included. Node ids are relative paths sans extension (e.g.
    "health/user-health-overview"); links between notes are resolved relative
    to the source file's directory (standard Markdown link resolution).

    The pipeline is explicit — walk → parse → node → edge → filter — each
    stage a pure function over the previous stage's output; node ids are
    derived from the node set, not accumulated state.
    """
    if not memory_root.is_dir():
        return MemoryGraphResponse(nodes=[], edges=[], warnings=["memory_root not found"])

    warnings: list[str] = []
    nodes: list[MemoryGraphNode] = []
    edges: list[MemoryGraphEdge] = []
    for md_file, note in walk_notes(memory_root, skip_names=_RESERVED_NAMES, warnings=warnings):
        nodes.append(_node_from_note(note))
        edges.extend(
            MemoryGraphEdge(source=source, target=target)
            for source, target in extract_md_links(note.body, md_file.parent, memory_root, note.rel)
        )

    # Drop edges whose target is not a known node.
    node_ids = {node.id for node in nodes}
    return MemoryGraphResponse(
        nodes=nodes,
        edges=[e for e in edges if e.target in node_ids],
        warnings=warnings,
    )


def _node_from_note(note: Note) -> MemoryGraphNode:
    """Map a parsed note to a graph node — the only schema-touching stage."""
    tags = list(note.tags)
    return MemoryGraphNode(
        id=note.rel,
        path=note.rel + ".md",
        title=note.title,
        description=note.description,
        tags=tags,
        primary_tag=_primary_tag(tags),
        timestamp=note.timestamp,
        ava_agent=note.ava_agent,
        ava_machine=note.ava_machine,
    )


def _primary_tag(tags: list[str]) -> str:
    """The tag the graph view groups nodes by: the first *domain* tag.

    `type/<x>` says how a note is meant to be used, not what it is about, and
    every note carries one — grouping on it would collapse the graph into six
    buckets and hide the domain structure the view exists to show. Falls back to
    the type tag only when a note has nothing else, so a node is never unlabeled
    when it does carry a tag.
    """
    domain = [t for t in tags if not t.startswith("type/")]
    if domain:
        return domain[0]
    return tags[0] if tags else ""


def _extract_meta(path: Path) -> tuple[str, list[str]]:
    """The `description` and `tags` from a markdown file's YAML frontmatter.

    Returns `("", [])` when the file is unreadable, has no frontmatter, or has
    neither field — a note that says nothing about itself is surfaced as such
    rather than guessed at from its title or body.

    Both fields ride the one read the search response already pays for. They are
    what a caller sees of a hit without opening it: the description says what the
    note holds, and the `type/<x>` tag says how it is meant to be used — which is
    what lets a filter be more careful with a user-profile note than with a
    procedure.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "", []
    note = parse_note(text, path.name)
    if note is None:
        return "", []
    description = note.description.strip() if note.description else ""
    return description, list(note.tags)


async def _milvus_topk(query_vector: Any, k: int, deadline: float) -> list[str]:
    """The milvus half of a search: connect, top-k, close — every step handed
    an explicit deadline.

    pymilvus's async client leaves awaits unbounded when given no timeout, and
    serializes connect/close on one process-global `AsyncConnectionManager`
    lock. So an unbounded call does not just stall its own request — it pins
    that lock and every later milvus call in the process queues behind it,
    including the `close()` in this function's own `finally`. Passing the
    deadline down bounds pymilvus's retry loop and its channel wait; the
    caller's `asyncio.timeout` covers the rest (the `Connect` RPC inside
    `ensure_channel_ready` takes no deadline at all).
    """
    from services.memory_indexer import index

    client = await index.connect_async(timeout=deadline)
    try:
        return await index.search_topk_async(client, query_vector, k, timeout=deadline)
    finally:
        with suppress(Exception):
            await client.close()


@router.post(
    "/api/memory/search",
    response_model=MemorySearchResponse,
    dependencies=[Depends(deny_isolated_result_read)],
)
async def post_memory_search(body: MemorySearchRequest) -> MemorySearchResponse:
    """Semantic search the memory pool; return **relative** paths of the
    top-k most-relevant markdown files.

    Paths are relative to memory pool root (settings.services.memory_root, e.g.
    `~/.ava/memory/`); the SDK caller prepends `ava.memory.PATH` to
    reconstruct the absolute path. fs-neutral so gateway fs (e.g.
    /Users/x) and agent-runner fs (/home/y) being different still works.

    Only the gateway hosts the milvus instance, so this endpoint
    always runs on the gateway; agent-runner SDK calls reach this
    handler via the gateway URL they were configured with (CF Tunnel /
    private network) and never enter via a local agent-runner gateway
    (agent-runners run no gateway process).

    Raises:
        IndexerUnavailable: embedder API / milvus backend unreachable, or the
            search exceeded its deadline (wire 503)
    """
    from services.memory_indexer import embedder

    # Both phases are native async I/O — httpx.AsyncClient for the Gemini
    # embed, pymilvus's AsyncMilvusClient for the search — so a slow backend
    # can never block the event loop (2026-08-03 freeze mechanism, see
    # _MEMORY_SEARCH_SEMAPHORE note). The semaphore still caps concurrency so
    # a burst of searches cannot pile up in-flight Gemini requests.
    #
    # The deadline wraps the semaphore acquires as well as the two phases: a
    # request parked in acquire behind a stalled peer is just as stuck as one
    # parked in the backend, and only a bound that covers both guarantees the
    # permit comes back. On expiry the CancelledError unwinds through
    # `async with`, whose release is synchronous — so the permit is returned
    # even though the cleanup awaits below never get to run.
    deadline = settings.services.memory_search_deadline_seconds
    try:
        async with asyncio.timeout(deadline):
            try:
                async with _MEMORY_SEARCH_SEMAPHORE:
                    query_vector = await embedder.embed_query_async(body.query)
            except Exception as exc:
                # Symmetric with the milvus phase below, and with what this
                # endpoint documents raising. Catching only EmbeddingAPIError
                # left every other embed failure to escape as a bare 500 whose
                # body carries no wire `reason` -- which the SDK cannot map back
                # to IndexerUnavailable, so callers saw an unmodelled error
                # instead of the outage this is. On 2026-08-07 the gateway was
                # running out of a deleted worktree's venv and the embed call's
                # httpx client raised FileNotFoundError building its SSL context
                # (missing certifi cacert); the 500 that produced killed agent
                # 405. Either backend failing means the same thing to a caller:
                # the index cannot answer.
                raise IndexerUnavailable(f"embed query failed: {exc}") from exc

            try:
                async with _MEMORY_SEARCH_SEMAPHORE:
                    abs_paths = await _milvus_topk(query_vector, body.k, deadline)
            except Exception as exc:
                raise IndexerUnavailable(f"milvus search failed: {exc}") from exc
    except TimeoutError as exc:
        raise IndexerUnavailable(f"memory search exceeded its {deadline:g}s deadline") from exc

    memory_root = gateway_memory_dir().resolve()
    rel_paths: list[str] = []
    results: list[MemorySearchResultItem] = []
    for p in abs_paths:
        abs_p = Path(p).resolve()
        try:
            rel = str(abs_p.relative_to(memory_root))
        except ValueError:
            rel = p
        rel_paths.append(rel)
        description, tags = _extract_meta(abs_p)
        results.append(MemorySearchResultItem(path=rel, description=description, tags=tags))
    return MemorySearchResponse(paths=rel_paths, results=results)


@router.post("/api/memory/refresh", response_model=MemoryRefreshResponse)
async def post_memory_refresh() -> MemoryRefreshResponse:
    """Fast-forward the gateway memory checkout to the consolidated pool.

    The gateway indexes `main`; once the pool's per-machine branches are
    merged into `main`, calling this pulls those changes into the local checkout
    (fast-forward only). The memory indexer then re-embeds the changed files on
    its own. Returns the HEAD sha after the pull.

    This runs only on the gateway (the gateway + memory checkout live
    there); a runner reaches it via its configured gateway URL.
    """
    from shared import memory_repo

    head = memory_repo.pull_main()
    return MemoryRefreshResponse(head=head)


@router.get("/api/memory/graph", response_model=MemoryGraphResponse)
def get_memory_graph() -> MemoryGraphResponse:
    """Return concept notes and cross-links from the gateway memory bundle."""
    return _build_memory_graph(gateway_memory_dir())
