"""Memory indexer daemon — `watchdog` Observer + Gemini embed + Milvus upsert.

After startup:
  1. Cold start: full scan `~/.ava/memory/**/*.md`, diff against milvus
     index, embed missing / changed files, prune deleted entries.
  2. Start watchdog Observer to monitor fs events, push dirty paths to
     a queue.
  3. Main loop drains the queue every second (set dedup), batch
     embed + upsert / delete.

Backed by the standalone milvus-lite server in the `services/milvus/`
session; URI `AVA_MILVUS_URI` (default `http://127.0.0.1:19530`).

Each file indexes as 0-or-1 description row (frontmatter `description`,
embedded on its own so short entity-bearing lines are not diluted by a
long body) + N body-chunk rows (~1800 chars each, ~200-char overlap,
paragraph-boundary aware). `index.search_topk` aggregates chunk hits back
to paths, so search callers see no difference.

API key comes from env `GEMINI_API_KEY`. `~/.ava/.env` is already the
single source of secrets.

Usage:
    .venv/bin/python -m services.memory_indexer.daemon

Kept alive by the watchdog via `services.memory_indexer.healthcheck`
(HTTP /healthz on :8105).

Refresh safety net: once an hour the daemon fast-forwards the gateway
checkout to origin/main itself. The intended path is the arbiter's
post-merge `ava memory refresh` (bundled into `ava memory arbiter merge`),
but when that step is skipped or fails, the checkout — and therefore the
search index — silently rots (the 2026-06-22 → 2026-08-01 staleness
incident: 6 weeks of merged notes never searchable). Fetch + fast-forward
is cheap; when HEAD moves, the fs observer below re-embeds the changed
files, and a pull failure is logged at ERROR and retried next cycle.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import re
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

from pymilvus import MilvusClient
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from services._pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.memory_indexer import embedder, index
from shared.config import settings
from shared.daemon_health import Liveness, health_port, start_health_server, stop_health_server
from shared.daemon_shutdown import install_graceful_shutdown
from shared.log import init_gateway_process
from shared.notes import parse_note
from shared.paths import gateway_memory_dir, legacy_pid_path
from shared.platform import CREATE_NO_WINDOW

_log = logging.getLogger("services.memory_indexer.daemon")

_MEMORY_ROOT = gateway_memory_dir()
_PIDFILE = settings.services.memory_indexer_pidfile
_LOOP_INTERVAL_S = 1.0
# Liveness staleness ceiling — generous because one iteration may embed a
# whole batch via a network round-trip; beating before and after the embed
# tolerates a slow-but-legit batch while a true wedge flips /healthz 503.
_LIVENESS_TIMEOUT_S = 180.0
# How often the daemon fast-forwards the gateway checkout to origin/main —
# the refresh safety net (see module docstring). An hour bounds index
# staleness to ~1 consolidation cycle; the fetch is a no-op when main moved.
_CHECKOUT_REFRESH_INTERVAL_S = 3600.0
_BATCH_SIZE = 32
"""Gemini embed_content accepts multiple inputs per call; batching amortizes round-trips."""

# Chunking: a long note's single embedding dilutes the entities mentioned in
# it (queries like "hand off to 402" missed notes whose body carried the id).
# The body is split at paragraph boundaries into blocks of ~1800 chars
# (~512 tokens), overlapping by ~200 chars (~64 tokens) so a query spanning a
# boundary still finds the note; the frontmatter description is embedded as
# its own row on top of that.
_CHUNK_MAX_CHARS = 1800
_CHUNK_OVERLAP_CHARS = 200

_MD_SUFFIX = ".md"


class _MarkdownEventHandler(FileSystemEventHandler):
    """Push *.md create / modify / move / delete paths to the dirty queue.

    Observer runs its own thread; callbacks must not block — only push,
    never process. The main loop dedup + batch. move is split into
    delete(src) + create(dest).
    """

    def __init__(self, dirty: queue.Queue[Path]) -> None:
        self._dirty = dirty

    def _push(self, path_str: str) -> None:
        p = Path(path_str)
        if p.suffix == _MD_SUFFIX:
            self._dirty.put(p)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._push(str(event.src_path))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._push(str(event.src_path))

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._push(str(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._push(str(event.src_path))
            dest = getattr(event, "dest_path", None)
            if dest:
                self._push(str(dest))


def _write_pidfile() -> None:
    if not acquire_pidfile(_PIDFILE, "services.memory_indexer.daemon"):
        _log.info("[memory_indexer] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)


def _remove_pidfile() -> None:
    remove_pidfile(_PIDFILE)


def _is_running() -> bool:
    """Whether a daemon is already running (via pidfile, new + legacy paths).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_holds_daemon(_PIDFILE, "services.memory_indexer.daemon") or pidfile_holds_daemon(
        legacy_pid_path("memory_indexer"), "services.memory_indexer.daemon"
    )


def _scan_disk(root: Path) -> dict[Path, float]:
    """Recursive list `*.md` under `root`. {abs_path: mtime}. Symlinks not followed."""
    if not root.exists():
        return {}
    result: dict[Path, float] = {}
    for p in root.rglob(f"*{_MD_SUFFIX}"):
        if p.is_file() and not p.is_symlink():
            try:
                result[p.resolve()] = p.stat().st_mtime
            except OSError:
                continue
    return result


def _split_note(content: str) -> tuple[str | None, str]:
    """Split one markdown file into (description, body).

    description is the frontmatter `description` (None when the file has no
    frontmatter or an empty one); body is the text after the frontmatter.
    Reuses the shared note parser so the desc vector and the description the
    search endpoint surfaces always agree.
    """
    note = parse_note(content, "memory.md")
    if note is None:
        return None, content
    description = note.description.strip() if note.description else None
    return description or None, note.body


def _chunk_body(
    body: str,
    *,
    max_chars: int = _CHUNK_MAX_CHARS,
    overlap_chars: int = _CHUNK_OVERLAP_CHARS,
) -> list[str]:
    """Split body text into overlapping chunks, preferring paragraph boundaries.

    Paragraphs (blank-line separated) pack greedily into chunks of at most
    `max_chars`; when the next paragraph would overflow, the chunk closes and
    the next one re-opens with the trailing paragraphs that fit in
    `overlap_chars`, so a query spanning a boundary still finds the note. A
    single paragraph longer than `max_chars` is hard-split by character with
    the same overlap. Returns [] for an empty body.
    """
    body = body.strip()
    if not body:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n[ \t]*\n", body)]
    paragraphs = [p for p in paragraphs if p]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def _flush() -> None:
        """Close the current chunk; carry its trailing paragraphs (up to
        `overlap_chars`) into the next chunk."""
        nonlocal current, current_len
        chunks.append("\n\n".join(current))
        tail: list[str] = []
        tail_len = 0
        for para in reversed(current):
            if tail_len + len(para) + (2 if tail_len else 0) > overlap_chars:
                break
            tail.insert(0, para)
            tail_len += len(para) + (2 if tail_len > 0 else 0)
        current = tail
        current_len = tail_len

    for para in paragraphs:
        if len(para) > max_chars:
            if current:
                chunks.append("\n\n".join(current))
                current, current_len = [], 0
            start = 0
            while start < len(para):
                end = min(start + max_chars, len(para))
                chunks.append(para[start:end])
                if end == len(para):
                    break
                start = end - overlap_chars
            continue
        if current and current_len + 2 + len(para) > max_chars:
            _flush()
        current.append(para)
        current_len += len(para) + (2 if current_len else 0)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _file_rows(content: str) -> list[tuple[str, int, str]]:
    """The chunk rows of one file: (kind, chunk_idx, text).

    The frontmatter `description` becomes one KIND_DESC row when present; the
    body splits into KIND_BODY chunks. A file with neither produces no rows
    (it is not searchable content; the cold-start reconcile tolerates that).
    """
    description, body = _split_note(content)
    rows: list[tuple[str, int, str]] = []
    if description:
        rows.append((index.KIND_DESC, 0, description))
    for i, chunk in enumerate(_chunk_body(body)):
        rows.append((index.KIND_BODY, i, chunk))
    return rows


def _process_paths(client: MilvusClient, paths: set[Path]) -> None:
    """Process a batch of dirty paths: missing/foreign -> delete; else embed+upsert.

    A path is deleted when it is missing on disk OR when it lies outside the
    watched root. The second arm matters: rows indexed from an older era
    (e.g. the authoring checkout, before the gateway checkout split) carry
    paths that still exist on disk — so a mere existence check would keep
    them forever, and every search would show the note twice. The index is
    keyed by absolute path; only paths under the watched root belong in it.

    Embedding failures (`EmbeddingAPIError`) propagate after the
    embedder's internal retries — the caller (main loop) catches + logs
    + continues (the next fs event re-triggers, indexer stays available).

    Sync function — the caller uses ``asyncio.to_thread`` so the event
    loop is not blocked from serving the health probe (`/healthz`).
    The pymilvus client uses gRPC internally and is cross-thread safe.
    """
    root = _MEMORY_ROOT.resolve()
    to_delete: list[Path] = []
    to_embed: list[tuple[Path, float, str, str]] = []  # (path, mtime, hash, content)
    existing_meta = index.all_meta(client)
    for p in paths:
        if not p.exists() or not p.is_file() or not p.is_relative_to(root):
            to_delete.append(p)
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            _log.warning("[indexer] skip %s: %r", p, exc)
            continue
        mtime = p.stat().st_mtime
        hash_ = index.content_hash(content)
        prev = existing_meta.get(str(p))
        if prev is not None and prev[1] == hash_:
            continue  # content unchanged, mtime touch only
        to_embed.append((p, mtime, hash_, content))

    for p in to_delete:
        index.delete(client, str(p))
        _log.info("[indexer] deleted %s", p)

    # Flatten each dirty file into its chunk rows (desc + body chunks);
    # embedding and upserting happen per row. A file whose content hash is
    # unchanged is skipped before this point, so a re-embed only happens when
    # the file actually changed.
    rows: list[
        tuple[Path, float, str, str, int, str]
    ] = []  # (path, mtime, hash, kind, chunk_idx, text)
    for path, mtime, hash_, content in to_embed:
        for kind, chunk_idx, text in _file_rows(content):
            rows.append((path, mtime, hash_, kind, chunk_idx, text))

    for i in range(0, len(rows), _BATCH_SIZE):
        batch = rows[i : i + _BATCH_SIZE]
        texts = [text for *_, text in batch]
        vectors = embedder.embed_documents(texts)
        last_path: str | None = None
        for (path, mtime, hash_, kind, chunk_idx, _), vector in zip(batch, vectors, strict=True):
            index.upsert(client, str(path), mtime, hash_, vector, kind=kind, chunk_idx=chunk_idx)
            if str(path) != last_path:
                _log.info("[indexer] indexed %s", path)
                last_path = str(path)


def _cold_start_reconcile(client: MilvusClient) -> None:
    """Diff disk vs index db; fill in gaps. Runs once on daemon start (in a thread executor).

    Rows whose path is outside the watched root are pruned even though the
    files still exist on disk (see `_process_paths`): they are leftovers
    from an era when the indexer embedded a different checkout, and they
    surface as stale duplicates in search results.
    """
    disk = _scan_disk(_MEMORY_ROOT)
    indexed = index.all_meta(client)
    indexed_paths = {Path(p) for p in indexed}
    disk_paths = set(disk.keys())
    root = _MEMORY_ROOT.resolve()

    dirty: set[Path] = set()
    # Changes / additions — mark dirty when mtime differs;
    # _process_paths further filters by hash internally.
    for path, mtime in disk.items():
        prev = indexed.get(str(path))
        if prev is None or prev[0] != mtime:
            dirty.add(path)
    # Deletions: indexed rows that vanished from disk, or that live outside
    # the watched root (stale-checkout leftovers) — _process_paths deletes
    # both classes (missing on disk / not under root).
    for path in indexed_paths - disk_paths:
        dirty.add(path)
    for path in indexed_paths:
        if not path.is_relative_to(root):
            dirty.add(path)

    if dirty:
        _log.info("[indexer] cold-start reconcile: %d dirty paths", len(dirty))
        try:
            _process_paths(client, dirty)
        except embedder.EmbeddingAPIError as exc:
            _log.error(
                "[indexer] cold-start embed failed: %r — daemon continues; watchdog re-triggers later",
                exc,
            )


def _refresh_gateway_checkout() -> None:
    """Fast-forward the gateway checkout to origin/main — hourly safety net.

    The intended path for new merged notes to reach the index is the
    arbiter's post-merge `ava memory refresh` (bundled into `ava memory
    arbiter merge`). This catches the case where that step is skipped or
    fails: fetch + ff-only merge is cheap, and when HEAD moves the fs
    observer fires events that re-embed the changed files. A failure is
    logged at ERROR and retried next cycle — a stale index can no longer
    rot silently. Keep-local mode: `pull_main` is a no-op.
    """
    from shared.memory_repo import gateway_memory_dir, pull_main

    cwd = gateway_memory_dir()
    try:
        before = subprocess.check_output(  # noqa: S603 — argv is a static literal
            ["git", "-C", str(cwd), "rev-parse", "HEAD"],
            text=True,
            creationflags=CREATE_NO_WINDOW,
        ).strip()
        head = pull_main()
    except Exception as exc:
        _log.error(
            "[indexer] gateway checkout refresh failed: %r — the search index may be "
            "stale; run `ava memory refresh`. Will retry next cycle.",
            exc,
        )
        return
    if head != before:
        _log.info(
            "[indexer] gateway checkout fast-forwarded %s → %s (post-merge refresh "
            "was missed — new notes are now searchable)",
            before[:8],
            head[:8],
        )


async def _drain_loop(
    client: MilvusClient, dirty_queue: queue.Queue[Path], liveness: Liveness
) -> None:
    """Main loop: every _LOOP_INTERVAL_S drain queue, dedup, batch process.

    `_process_paths` blocks (network embed + milvus gRPC); use
    ``asyncio.to_thread`` so the event loop can still serve the health
    probe during embedding. Every `_CHECKOUT_REFRESH_INTERVAL_S` the loop
    also runs the gateway-checkout refresh safety net.
    """
    _log.info("[indexer] daemon loop started, pid=%s", os.getpid())
    next_checkout_refresh = time.monotonic() + _CHECKOUT_REFRESH_INTERVAL_S
    while True:
        liveness.beat()
        await asyncio.sleep(_LOOP_INTERVAL_S)
        now = time.monotonic()
        if now >= next_checkout_refresh:
            next_checkout_refresh = now + _CHECKOUT_REFRESH_INTERVAL_S
            await asyncio.to_thread(_refresh_gateway_checkout)
        batch: set[Path] = set()
        # Drain the queue until empty — queue.Empty is the loop terminator.
        with suppress(queue.Empty):
            while True:
                batch.add(dirty_queue.get_nowait().resolve())
        if not batch:
            continue
        try:
            await asyncio.to_thread(_process_paths, client, batch)
            liveness.beat()  # embed batch returned -> loop is making progress
        except embedder.EmbeddingAPIError as exc:
            # Name the blast radius: which/how many paths lost this round.
            _log.error(
                "[indexer] embed failed for a batch of %d path(s) (%s): %r — "
                "subsequent fs modify events will retry",
                len(batch),
                ", ".join(str(p) for p in sorted(batch)[:5]) + ("..." if len(batch) > 5 else ""),
                exc,
            )


async def _connect_milvus_with_retry(deadline_s: float = 30.0) -> MilvusClient:
    """Connect to milvus at daemon startup — `ava start` spawns the
    milvus session and the memory_indexer session in order; milvus-lite
    server initialization takes ~1-3s, and within that race window
    `index.connect()` may hit connection refused. This function retries
    every 2s up to deadline to give milvus time to come up.

    Runtime milvus calls (`_process_paths` etc.) do **not** use this
    retry — those are watchdog's responsibility (if milvus dies, the
    healthcheck restarts it; if memory_indexer itself crashes and
    exits, the healthcheck spawns a fresh process that goes through
    this retry).
    """
    start = time.time()
    last_exc: Exception | None = None
    while time.time() - start < deadline_s:
        try:
            return await asyncio.to_thread(index.connect)
        except Exception as exc:
            last_exc = exc
            _log.info(
                "[indexer] milvus not ready (%s: %s), retry in 2s...", type(exc).__name__, exc
            )
            await asyncio.sleep(2.0)
    raise RuntimeError(f"milvus unreachable after {deadline_s}s: {last_exc}") from last_exc


async def run() -> None:
    """Write pidfile -> start healthz server -> cold-start -> drain loop.

    Both before cold-start — cold-start may take tens of seconds
    embedding many files; pidfile / healthz being invisible would let
    watchdog misjudge death and spawn races (PR #254 fixed this).
    Pidfile before the healthz bind — see services/restarter/daemon.py:run().
    """
    if _is_running():
        _log.info("[indexer] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)

    _write_pidfile()
    _log.info("[indexer] pidfile written: %s", _PIDFILE)

    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    health = await start_health_server("memory_indexer", liveness=liveness)
    _log.info("[indexer] healthz listening on :%s", health_port("memory_indexer"))

    _MEMORY_ROOT.mkdir(parents=True, exist_ok=True)
    client = await _connect_milvus_with_retry()
    _log.info("[indexer] connected to milvus %s", index.server_uri())
    dirty_queue: queue.Queue[Path] = queue.Queue()
    handler = _MarkdownEventHandler(dirty_queue)
    observer = Observer()
    observer.schedule(handler, str(_MEMORY_ROOT), recursive=True)
    observer.start()
    _log.info("[indexer] watching %s", _MEMORY_ROOT)

    try:
        await asyncio.to_thread(_cold_start_reconcile, client)
        await _drain_loop(client, dirty_queue, liveness)
    finally:
        observer.stop()
        observer.join(timeout=5.0)
        with suppress(Exception):
            client.close()
        await stop_health_server(health)
        _remove_pidfile()
        _log.info("[indexer] daemon stopped")


def main() -> None:
    """Entry point: log init + install the graceful-stop signal + run asyncio.

    Does not call `assert_schema_current` — memory_indexer does not
    read the main DB (only via milvus gRPC); there is no main-DB
    schema-drift surface.
    """
    init_gateway_process(name="memory_indexer")
    install_graceful_shutdown("indexer")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        _log.info("[indexer] received interrupt, shutting down")
    except Exception:
        _log.exception("[indexer] daemon crashed — uncaught exception escaped run()")
        raise
    finally:
        _remove_pidfile()


if __name__ == "__main__":
    main()
