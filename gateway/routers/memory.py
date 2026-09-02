"""Memory endpoints — /api/memory/search + /api/memory/refresh + /api/memory/graph.

`search` (ava.memory.search SDK target) returns relative paths under
memory_root; the SDK caller prepends `ava.memory.PATH` to reconstruct the
absolute path (fs-neutral so gateway and agent-runner filesystems can
differ).

`refresh` fast-forwards the gateway memory checkout to the consolidated
pool (`origin/main`); the memory indexer then re-embeds the changed files.

`pool` serves the consolidated pool as a git bundle (real main ancestry) —
the bootstrap source a fresh agent-runner fetches when it has no memory
remote (memory_repo.bootstrap_from_gateway).

`graph` returns concept notes and cross-links as a graph for the frontend
OKF knowledge-graph page.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response

from gateway.routers._eval_guard import deny_isolated_result_read
from gateway.schemas import (
    MemoryGraphEdge,
    MemoryGraphNode,
    MemoryGraphResponse,
    MemoryNoteResponse,
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
# the backend's async client); the semaphore caps in-flight Gemini query
# embeds so a burst of searches cannot pile up unbounded on the shared key.
# Sized by `memory_search_max_concurrency` (env
# AVA_MEMORY_SEARCH_MAX_CONCURRENCY, default 20 — see shared/config/services.py):
# the historical hardcoded 2 predated the async-embed fix (2026-08-03, when a
# sync embed on the event loop froze the whole gateway for up to ~4.5 minutes
# and cost 13 restarts in 8h) and starved passive recall behind a queue
# during fleet-wake bursts; query embeds are short single calls on a paid
# Tier-2 key, so a burst fits well within the quota at 20.
#
# Every wait under the semaphore must be bounded — see `post_memory_search`'s
# deadline. Holding a permit across an unbounded await is what turned a
# stalled backend into a dead endpoint on 2026-08-03: both permits pinned,
# every later request parked forever in acquire, and each agent's passive
# recall wedged with it.


@lru_cache(maxsize=1)
def _search_semaphore() -> asyncio.Semaphore:
    """The query-embed concurrency gate, built once per process from the
    `memory_search_max_concurrency` setting — a knob, not a hardcoded
    constant, so a deployment can widen or narrow it without a code change
    (config panel or env; takes effect on gateway restart)."""
    return asyncio.Semaphore(settings.services.memory_search_max_concurrency)


@asynccontextmanager
async def _bounded_semaphore(semaphore: asyncio.Semaphore) -> AsyncGenerator[None]:
    """Acquire a search-gate permit with a short queue budget.

    The overall search deadline already covers the acquire, but a deep queue
    under that deadline is exactly the 2026-08-29 fleet-wake storm: every
    late request waits the full deadline and endpoint latency scales with
    queue length. Failing fast on the acquire — a permit not free within
    `memory_search_acquire_timeout_seconds` is congestion, not a backend the
    caller should wait out — answers 503 in ~1s, so passive recall's own
    deadline degrades in ~1s instead of ~5s and an explicit search learns
    immediately instead of queueing.
    """
    budget = settings.services.memory_search_acquire_timeout_seconds
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=budget)
    except TimeoutError as exc:
        raise IndexerUnavailable(f"memory search concurrency gate busy after {budget:g}s") from exc
    try:
        yield
    finally:
        semaphore.release()


# Files reserved by OKF spec — excluded from concept graph
_RESERVED_NAMES = frozenset({"index.md", "log.md", "MEMORY.md"})


def _build_memory_graph(memory_root: Path) -> MemoryGraphResponse:
    """Build a concept graph from the memory pool on the gateway filesystem.

    Scans recursively so notes in subdirectories (health/, ava/, etc.) are
    included. Node ids are relative paths sans extension (e.g.
    "health/user-health-overview"); links between notes are resolved relative
    to the source file's directory (standard Markdown link resolution).

    The graph's main structure is containment: one pseudo node per folder
    (always including the pool root) with an edge from every note to its
    folder, plus folder → parent-folder edges so the folder skeleton is a
    connected tree. Cross-references between notes ride on top as
    `reference` edges, which the frontend renders visually weaker.

    The pipeline is explicit — walk → parse → node → edge → filter — each
    stage a pure function over the previous stage's output; node ids are
    derived from the node set, not accumulated state.
    """
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

    # Drop reference edges whose target is not a known note.
    note_ids = {node.id for node in nodes}
    reference_edges = [e for e in reference_edges if e.target in note_ids]

    folder_nodes, containment_edges = _folder_structure(nodes, memory_root)

    return MemoryGraphResponse(
        nodes=[*folder_nodes, *nodes],
        edges=[*containment_edges, *reference_edges],
        warnings=warnings,
    )


def _folder_id_of(rel_id: str) -> str:
    """The folder pseudo-node id holding the note `rel_id`: its directory as
    a posix path with a trailing slash; the pool root is "/"."""
    parts = rel_id.split("/")
    if len(parts) == 1:
        return "/"
    return "/".join(parts[:-1]) + "/"


def _parent_folder_id(folder_id: str) -> str:
    """The parent folder pseudo-node id ("a/b/" → "a/"; "/" → "/")."""
    parts = folder_id.rstrip("/").split("/")
    return "/" if len(parts) <= 1 else "/".join(parts[:-1]) + "/"


def _folder_structure(
    nodes: list[MemoryGraphNode], memory_root: Path
) -> tuple[list[MemoryGraphNode], list[MemoryGraphEdge]]:
    """Folder pseudo nodes + containment edges — the graph's main structure.

    One pseudo node per folder that holds notes, closed under parent
    directories (every ancestor of a note's folder becomes a node, so
    folder → parent-folder edges always land on a node), plus the pool root
    itself so the skeleton is a single connected tree even when the pool has
    no root-level notes. An empty pool yields no nodes at all, so the
    frontend's empty state still triggers.
    """
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
            ava_agent=None,
            ava_machine=None,
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
    """Map a parsed note to a graph node — the only schema-touching stage."""
    tags = list(note.tags)
    return MemoryGraphNode(
        id=note.rel,
        path=note.rel + ".md",
        title=note.title,
        kind="note",
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


async def _backend_topk(
    query_vector: Any, k: int, deadline: float, dim: int, fingerprint: str
) -> list[str]:
    """The storage half of a search: ask the configured backend for top-k
    paths, every step handed an explicit deadline. `dim` + `fingerprint` are
    the embedding provider's vector space (see `embeddings.factory`) — the
    backend is constructed for it even though the read path never writes
    rows.

    pymilvus (the default backend) leaves awaits unbounded when given no
    timeout, and serializes connect/close on one process-global
    `AsyncConnectionManager` lock. So an unbounded call does not just stall
    its own request — it pins that lock and every later milvus call in the
    process queues behind it, including the `close()` in the backend's own
    `finally`. Passing the deadline down bounds pymilvus's retry loop and
    its channel wait; the caller's `asyncio.timeout` covers the rest (the
    `Connect` RPC inside `ensure_channel_ready` takes no deadline at all).
    """
    from services.memory_indexer.backends import factory

    return await factory.get_backend(dim=dim, fingerprint=fingerprint).search_topk_async(
        query_vector, k, timeout=deadline
    )


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

    Only the gateway hosts the search backend (numpy by default), so
    this endpoint always runs on the gateway; agent-runner SDK calls reach this
    handler via the gateway URL they were configured with (CF Tunnel /
    private network) and never enter via a local agent-runner gateway
    (agent-runners run no gateway process).

    Raises:
        IndexerUnavailable: embedder API / search backend unreachable, or the
            search exceeded its deadline (wire 503)
    """
    from services.memory_indexer.embeddings import factory as _embedding_factory

    provider = _embedding_factory.get_provider()

    # Both phases are native async I/O — httpx.AsyncClient for the embed,
    # the backend's async client for the search — so a slow backend
    # can never block the event loop (2026-08-03 freeze mechanism, see
    # `_search_semaphore` note). The semaphore still caps concurrency so
    # a burst of searches cannot pile up in-flight embedding requests.
    #
    # The deadline wraps the semaphore acquires as well as the two phases: a
    # request parked in acquire behind a stalled peer is just as stuck as one
    # parked in the backend, and only a bound that covers both guarantees the
    # permit comes back. Each acquire is separately bounded by
    # `memory_search_acquire_timeout_seconds` (`_bounded_semaphore`), which
    # is what turns a congested gate into a ~1s 503 instead of a wait until
    # the overall deadline. On expiry the CancelledError unwinds through
    # `async with`, whose release is synchronous — so the permit is returned
    # even though the cleanup awaits below never get to run.
    deadline = settings.services.memory_search_deadline_seconds
    try:
        async with asyncio.timeout(deadline):
            try:
                async with _bounded_semaphore(_search_semaphore()):
                    query_vector = await provider.embed_query_async(body.query)
            except IndexerUnavailable:
                # The gate was busy (`_bounded_semaphore`'s fast-fail) — a
                # modelled state, not a backend failure; do not re-wrap it.
                raise
            except Exception as exc:
                # Symmetric with the backend phase below, and with what this
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
                async with _bounded_semaphore(_search_semaphore()):
                    abs_paths = await _backend_topk(
                        query_vector, body.k, deadline, provider.dim, provider.fingerprint
                    )
            except IndexerUnavailable:
                raise
            except Exception as exc:
                raise IndexerUnavailable(f"memory search backend failed: {exc}") from exc
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


@router.get("/api/memory/note", response_model=MemoryNoteResponse)
def get_memory_note(path: str) -> MemoryNoteResponse:
    """Return one parsed memory note by its **relative** path (sans frontmatter).

    `path` is a memory-pool-relative markdown path as the graph carries it
    (e.g. `health/user-health-overview.md`). Resolved inside the memory root
    only — traversal (`..`, absolute paths) is rejected with 404 rather than
    leaking filesystem structure. A file that is not a note (no/invalid
    frontmatter) is not a note either: 404. The response carries the parsed
    fields plus the markdown body with the YAML frontmatter already stripped.
    """
    root = gateway_memory_dir().resolve()
    rel = Path(path)
    if rel.is_absolute() or rel.suffix != ".md":
        raise HTTPException(status_code=404, detail="memory note not found")
    # Everything below is one failure surface: a path that cannot be resolved
    # (null bytes in lstat raise ValueError on POSIX), escapes the root, is
    # missing, or is unreadable is uniformly "not a note" — 404, never 500
    # (QA #1169 F3: `ok%00.md` used to escape as an uncaught ValueError).
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
        ava_agent=note.ava_agent,
        ava_machine=note.ava_machine,
        body=note.body,
    )


@router.get("/api/memory/pool", response_class=Response)
def get_memory_pool() -> Response:
    """Download the consolidated memory pool as a git bundle.

    Serves `gateway_memory_dir()` — the consolidated checkout on `main` — as a
    `git bundle` of HEAD (full real ancestry, so a bootstrapped machine branch
    is a true descendant of `main` and converges cleanly when a memory remote
    is configured later). A fresh agent-runner whose memory remote is not
    configured (headless enroll, no GitHub credentials) fetches this over its
    gateway URL and clones it as its initial pool, so the shared index and
    notes reach its agents without GitHub. Untracked machine-local paths
    (`.cache`, `.githooks`, …) never ride a bundle — git only carries the
    tracked tree. Sits behind the normal Bearer/session middleware like every
    /api route. `X-Pool-Head` carries the checkout's HEAD sha so the receiver
    can verify what it cloned is the advertised snapshot.
    """
    root = gateway_memory_dir()
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
    """git-bundle HEAD of the consolidated checkout. Raises HTTPException on
    git failure — outside any try here, so the route's except only wraps the
    transport/OS layer."""
    import tempfile

    from shared.proc import run_bounded

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
