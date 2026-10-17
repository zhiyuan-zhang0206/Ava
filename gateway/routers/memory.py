from __future__ import annotations

import asyncio
import subprocess
import threading
from collections import OrderedDict
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from base.api_contracts.agent_contract import IndexerUnavailable
from base.db import Database
from base.packages.docs.notes import Note, extract_md_links, parse_note, walk_notes
from gateway.agents.eval_guard import deny_isolated_result_read
from gateway.http.disconnect import run_until_disconnect
from gateway.http.model_catalog import require_model_catalog
from gateway.schemas.memory import (
    MemoryGraphEdge,
    MemoryGraphNode,
    MemoryGraphResponse,
    MemoryNoteResponse,
    MemoryRefreshResponse,
    MemorySearchRequest,
    MemorySearchResponse,
    MemorySearchResultItem,
)
from services.derived.memory_indexer import rerank
from services.derived.memory_indexer.backends.base import ChunkHit
from services.derived.memory_indexer.embeddings.base import EmbeddingProvider

router = APIRouter()


@dataclass(frozen=True)
class MemorySearchInputs:
    # meiva:ref gateway/routers/docs/memory.meiva.okf.md#bounded-search
    embedding_backend: Callable[[], str]
    backend: Callable[[], str]
    uri: Callable[[], str]
    embedding_timeout: Callable[[], float]
    api_key: Callable[[], str | None]
    acquire_timeout: Callable[[], float]
    deadline: Callable[[], float]
    rerank_model: Callable[[], str]
    rerank_candidates: Callable[[], int]
    dedup_candidates: Callable[[], int]


def build_search_gate(max_concurrency: int) -> asyncio.Semaphore:
    # meiva:ref gateway/routers/docs/memory.meiva.okf.md#bounded-search
    return asyncio.Semaphore(max_concurrency)


@asynccontextmanager
async def _bounded_semaphore(semaphore: asyncio.Semaphore, budget: float) -> AsyncGenerator[None]:
    # meiva:ref gateway/routers/docs/memory.meiva.okf.md#bounded-search
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=budget)
    except TimeoutError as exc:
        raise IndexerUnavailable(f"memory search concurrency gate busy after {budget:g}s") from exc
    try:
        yield
    finally:
        semaphore.release()


_RESERVED_NAMES = frozenset({"index.md", "log.md", "MEMORY.md"})


def _build_memory_graph(memory_root: Path) -> MemoryGraphResponse:
    # meiva:ref gateway/routers/docs/memory.meiva.okf.md#graph-projection
    if not memory_root.is_dir():
        return MemoryGraphResponse(nodes=[], edges=[], warnings=["memory_root not found"])

    warnings: list[str] = []
    nodes: list[MemoryGraphNode] = []
    reference_edges: list[MemoryGraphEdge] = []
    for md_file, note in walk_notes(memory_root, skip_names=_RESERVED_NAMES, warnings=warnings):
        nodes.append(_node_from_note(note))
        reference_edges.extend(
            MemoryGraphEdge(source=source, target=target, kind="reference")
            for source, target in extract_md_links(note.body, md_file.parent, memory_root, note.rel)
        )

    note_ids = {node.id for node in nodes}
    reference_edges = [e for e in reference_edges if e.target in note_ids]

    folder_nodes, containment_edges = _folder_structure(nodes, memory_root)

    return MemoryGraphResponse(
        nodes=[*folder_nodes, *nodes],
        edges=[*containment_edges, *reference_edges],
        warnings=warnings,
    )


def _folder_id_of(rel_id: str) -> str:
    parts = rel_id.split("/")
    if len(parts) == 1:
        return "/"
    return "/".join(parts[:-1]) + "/"


def _parent_folder_id(folder_id: str) -> str:
    parts = folder_id.rstrip("/").split("/")
    return "/" if len(parts) <= 1 else "/".join(parts[:-1]) + "/"


def _folder_structure(
    nodes: list[MemoryGraphNode], memory_root: Path
) -> tuple[list[MemoryGraphNode], list[MemoryGraphEdge]]:
    # meiva:ref gateway/routers/docs/memory.meiva.okf.md#graph-projection
    if not nodes:
        return [], []

    folders: set[str] = {"/"}
    for node in nodes:
        folder = _folder_id_of(node.id)
        while folder != "/":
            folders.add(folder)
            folder = _parent_folder_id(folder)

    folder_nodes = [
        MemoryGraphNode(
            id=folder,
            path=folder,
            title=(
                memory_root.name or "/" if folder == "/" else folder.rstrip("/").rsplit("/", 1)[-1]
            ),
            kind="folder",
            description=None,
            tags=[],
            primary_tag="",
            timestamp=None,
            meiva_agent=None,
            meiva_machine=None,
        )
        for folder in sorted(folders)
    ]

    edges = [
        MemoryGraphEdge(source=node.id, target=_folder_id_of(node.id), kind="containment")
        for node in nodes
    ]
    edges.extend(
        MemoryGraphEdge(source=folder, target=_parent_folder_id(folder), kind="containment")
        for folder in sorted(folders)
        if folder != "/"
    )
    return folder_nodes, edges


def _node_from_note(note: Note) -> MemoryGraphNode:
    tags = list(note.tags)
    return MemoryGraphNode(
        id=note.rel,
        path=note.rel + ".md",
        title=note.title,
        kind="note",
        description=note.description,
        tags=tags,
        primary_tag=primary_tag(tags),
        timestamp=note.timestamp,
        meiva_agent=note.meiva_agent,
        meiva_machine=note.meiva_machine,
    )


def primary_tag(tags: list[str]) -> str:
    # meiva:ref gateway/routers/docs/memory.meiva.okf.md#graph-projection
    domain = [t for t in tags if not t.startswith("type/")]
    if domain:
        return domain[0]
    return tags[0] if tags else ""


async def _backend_topk(
    database: Database,
    query_vector: Any,
    k: int,
    deadline: float,
    dim: int,
    fingerprint: str,
    inputs: MemorySearchInputs,
) -> list[ChunkHit]:
    # meiva:ref gateway/routers/docs/memory.meiva.okf.md#bounded-search
    from services.derived.memory_indexer.backends import factory

    return await factory.get_backend(
        database, dim=dim, fingerprint=fingerprint, name=inputs.backend(), uri_reader=inputs.uri
    ).search_topk_async(query_vector, k, timeout=deadline)


@router.post(
    "/api/memory/search",
    response_model=MemorySearchResponse,
    dependencies=[Depends(deny_isolated_result_read)],
    description=(
        "Search the memory pool; return the `k` notes most relevant to the query,\n"
        "each with the chunk that matched and a relevance score.\n"
        "\n"
        "Two stages. Vector search finds the best chunk of the closest\n"
        "`memory_rerank_candidates` notes (`memory_dedup_candidates` for the `dedup`\n"
        "purpose); a decision model then judges each chunk\n"
        "against the query, and the response is ordered by the probability that the\n"
        "chunk is relevant (`score`). No text is generated on this path.\n"
        "\n"
        "One search issues at most that many decision requests, whatever `k` is: `k` above the\n"
        "candidate count returns the candidates. Notes in `exclude` are dropped from the vector\n"
        "candidates before ranking and take no slot. The ranking stops, and its unanswered requests\n"
        "are cancelled, when the client disconnects or the deadline passes (the gateway's own, or\n"
        "the request's `deadline_seconds` when that is shorter).\n"
        "\n"
        "Paths are relative to memory pool root (settings.services.memory_root, e.g.\n"
        "`~/.meiva/memory/`); the SDK caller prepends `meiva.memory.PATH` to\n"
        "reconstruct the absolute path. fs-neutral so gateway fs (e.g.\n"
        "/Users/x) and agent-runner fs (/home/y) being different still works.\n"
        "\n"
        "Only the gateway hosts the search backend (numpy by default), so\n"
        "this endpoint always runs on the gateway; agent-runner SDK calls reach this\n"
        "handler via the gateway URL they were configured with (CF Tunnel /\n"
        "private network) and never enter via a local agent-runner gateway\n"
        "(agent-runners run no gateway process).\n"
        "\n"
        "Raises:\n"
        "    IndexerUnavailable: embedder API / search backend unreachable, the\n"
        "        decision model failed, or the search exceeded its deadline (wire 503)"
    ),
)
async def post_memory_search(request: Request, body: MemorySearchRequest) -> MemorySearchResponse:
    # meiva:ref gateway/routers/docs/memory.meiva.okf.md#bounded-search
    from services.derived.memory_indexer.embeddings import factory as _embedding_factory

    inputs: MemorySearchInputs = request.app.state.memory_search_inputs
    provider = _embedding_factory.get_provider(
        inputs.embedding_backend(),
        catalog=require_model_catalog(request),
        timeout_reader=inputs.embedding_timeout,
        api_key_reader=inputs.api_key,
    )

    deadline = inputs.deadline()
    if body.deadline_seconds is not None:
        deadline = min(deadline, body.deadline_seconds)
    try:
        async with asyncio.timeout(deadline):
            ranked = await run_until_disconnect(
                _search_and_rank(request, body, provider, inputs, deadline),
                request.is_disconnected,
            )
    except TimeoutError as exc:
        raise IndexerUnavailable(f"memory search exceeded its {deadline:g}s deadline") from exc
    if ranked is None:
        return MemorySearchResponse(results=[])
    return MemorySearchResponse(
        results=[
            MemorySearchResultItem(
                path=item.path,
                description=item.description,
                tags=item.tags,
                score=item.score,
                chunk=item.chunk,
            )
            for item in ranked
        ]
    )


def _candidate_count(inputs: MemorySearchInputs, body: MemorySearchRequest) -> int:
    # meiva:ref gateway/routers/docs/memory.meiva.okf.md#bounded-search
    return inputs.dedup_candidates() if body.purpose == "dedup" else inputs.rerank_candidates()


async def _search_and_rank(
    request: Request,
    body: MemorySearchRequest,
    provider: EmbeddingProvider,
    inputs: MemorySearchInputs,
    deadline: float,
) -> list[rerank.RankedChunk]:
    limit = _candidate_count(inputs, body)
    exclude = frozenset(body.exclude)
    hits = await _vector_candidates(request, body, provider, inputs, deadline, limit + len(exclude))
    return await rerank.rerank(
        body.query,
        hits,
        purpose=body.purpose,
        model=inputs.rerank_model(),
        catalog=require_model_catalog(request),
        k=body.k,
        limit=limit,
        exclude=exclude,
        memory_root=request.app.state.gateway_config.memory_root,
    )


async def _vector_candidates(
    request: Request,
    body: MemorySearchRequest,
    provider: EmbeddingProvider,
    inputs: MemorySearchInputs,
    deadline: float,
    count: int,
) -> list[ChunkHit]:
    # meiva:ref gateway/routers/docs/memory.meiva.okf.md#bounded-search
    from services.derived.memory_indexer.embeddings.base import EmbeddingAPIError

    gate = request.app.state.memory_search_gate
    try:
        async with _bounded_semaphore(gate, inputs.acquire_timeout()):
            query_vector = await provider.embed_query_async(body.query)
    except IndexerUnavailable:
        raise
    except EmbeddingAPIError as exc:
        raise IndexerUnavailable(f"embed query failed: {exc}") from exc

    try:
        async with _bounded_semaphore(gate, inputs.acquire_timeout()):
            return await _backend_topk(
                request.app.state.db,
                query_vector,
                count,
                deadline,
                provider.dim,
                provider.fingerprint,
                inputs,
            )
    except IndexerUnavailable:
        raise
    except Exception as exc:
        raise IndexerUnavailable(f"memory search backend failed: {exc}") from exc


@router.post(
    "/api/memory/refresh",
    response_model=MemoryRefreshResponse,
    description=(
        "Fast-forward the gateway memory checkout to the consolidated pool.\n"
        "\n"
        "The gateway indexes `main`; once the pool's per-machine branches are\n"
        "merged into `main`, calling this pulls those changes into the local checkout\n"
        "(fast-forward only). The memory indexer re-embeds the changed files on its\n"
        "next patrol (15 minutes by default). Returns the HEAD sha after the pull.\n"
        "\n"
        "This runs only on the gateway (the gateway + memory checkout live\n"
        "there); a runner reaches it via its configured gateway URL.\n"
        "\n"
        "`pull_main` shells out to git (network-bound fetch against the memory\n"
        "remote): it must run OFF the event loop, or a slow/dead remote blocks the\n"
        "whole gateway until the watchdog kills it (P2 #2102: the 2026-09-10\n"
        "04:06-04:35 watchdog kill loop). `asyncio.to_thread` — the same hop the\n"
        "memory indexer uses for its own pull."
    ),
)
async def post_memory_refresh(request: Request) -> MemoryRefreshResponse:
    # meiva:ref gateway/routers/docs/memory.meiva.okf.md#refresh-owner
    from base.deploy.git import memory_repo

    config = request.app.state.gateway_process.config.view.general
    head = await asyncio.to_thread(
        memory_repo.pull_main,
        root=request.app.state.gateway_config.memory_root,
        keep_local=config.memory_keep_local,
        branch=config.track_branch,
    )
    return MemoryRefreshResponse(head=head)


def _pool_revision(root: Path) -> str | None:
    # meiva:ref gateway/routers/docs/memory.meiva.okf.md#graph-revision
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


class MemoryGraphCache:
    # meiva:ref gateway/routers/docs/memory.meiva.okf.md#graph-revision
    def __init__(self, *, max_entries: int = 4) -> None:
        self._max_entries = max_entries
        self._graphs: OrderedDict[tuple[str, str], MemoryGraphResponse] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, root: Path, revision: str) -> MemoryGraphResponse:
        key = (str(root), revision)
        with self._lock:
            cached = self._graphs.get(key)
            if cached is not None:
                self._graphs.move_to_end(key)
                return cached
            graph = _build_memory_graph(root)
            self._graphs[key] = graph
            while len(self._graphs) > self._max_entries:
                self._graphs.popitem(last=False)
            return graph


@router.get(
    "/api/memory/graph",
    response_model=MemoryGraphResponse,
    description=("Return concept notes and cross-links from the gateway memory bundle."),
)
def get_memory_graph(request: Request) -> MemoryGraphResponse:
    # meiva:ref gateway/routers/docs/memory.meiva.okf.md#graph-revision
    root = request.app.state.gateway_config.memory_root
    revision = _pool_revision(root)
    if revision is None:
        return _build_memory_graph(root)
    cache: MemoryGraphCache = request.app.state.memory_graph_cache
    return cache.get(root, revision)


@router.get(
    "/api/memory/note",
    response_model=MemoryNoteResponse,
    description=(
        "Return one parsed memory note by its **relative** path (sans frontmatter).\n"
        "\n"
        "`path` is a memory-pool-relative markdown path as the graph carries it\n"
        "(e.g. `health/user-health-overview.md`). Resolved inside the memory root\n"
        "only — traversal (`..`, absolute paths) is rejected with 404 rather than\n"
        "leaking filesystem structure. A file that is not a note (no/invalid\n"
        "frontmatter) is not a note either: 404. The response carries the parsed\n"
        "fields plus the markdown body with the YAML frontmatter already stripped."
    ),
)
def get_memory_note(request: Request, path: str) -> MemoryNoteResponse:
    # meiva:ref gateway/routers/docs/memory.meiva.okf.md#note-containment
    root = request.app.state.gateway_config.memory_root.resolve()
    rel = Path(path)
    if rel.is_absolute() or rel.suffix != ".md":
        raise HTTPException(status_code=404, detail="memory note not found")

    try:
        candidate = (root / rel).resolve()
        candidate.relative_to(root)
        if not candidate.is_file():
            raise FileNotFoundError
        text = candidate.read_text(encoding="utf-8")
    except (ValueError, OSError, UnicodeDecodeError):
        raise HTTPException(status_code=404, detail="memory note not found") from None
    note = parse_note(text, rel.with_suffix("").as_posix())
    if note is None:
        raise HTTPException(status_code=404, detail="memory note not found")
    return MemoryNoteResponse(
        path=note.rel + ".md",
        title=note.title,
        description=note.description,
        tags=list(note.tags),
        timestamp=note.timestamp,
        meiva_agent=note.meiva_agent,
        meiva_machine=note.meiva_machine,
        body=note.body,
    )


@router.get(
    "/api/memory/pool",
    response_class=Response,
    description=(
        "Download the consolidated memory pool as a git bundle.\n"
        "\n"
        "Serves the request-owned memory checkout on `main` as a\n"
        "`git bundle` of HEAD (full real ancestry, so a bootstrapped machine branch\n"
        "is a true descendant of `main` and converges cleanly when a memory remote\n"
        "is configured later). A fresh agent-runner whose memory remote is not\n"
        "configured (a headless remote runner, no GitHub credentials) fetches this over its\n"
        "gateway URL and clones it as its initial pool, so the shared index and\n"
        "notes reach its agents without GitHub. Untracked machine-local paths\n"
        "(`.cache`, `.githooks`, …) never ride a bundle — git only carries the\n"
        "tracked tree. Sits behind the normal Bearer/session middleware like every\n"
        "/api route. `X-Pool-Head` carries the checkout's HEAD sha so the receiver\n"
        "can verify what it cloned is the advertised snapshot."
    ),
)
def get_memory_pool(request: Request) -> Response:
    root = request.app.state.gateway_config.memory_root
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="gateway memory pool not initialized")
    try:
        head, data = _build_pool_bundle(root)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HTTPException(status_code=500, detail=f"memory pool bundle failed: {e}") from e
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "X-Pool-Head": head,
            "Content-Disposition": 'attachment; filename="memory-pool.bundle"',
        },
    )


def _build_pool_bundle(root: Path) -> tuple[str, bytes]:
    import tempfile

    from base.host.proc import run_bounded

    with tempfile.TemporaryDirectory(prefix="memory-pool-bundle-") as tmp:
        bundle_path = Path(tmp) / "pool.bundle"
        result = run_bounded(
            ["git", "-C", str(root), "bundle", "create", str(bundle_path), "HEAD"],
            timeout=60.0,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"git bundle create failed: {(result.stderr or '').strip()[:200]}",
            )
        head = run_bounded(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            timeout=30.0,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return head, bundle_path.read_bytes()
