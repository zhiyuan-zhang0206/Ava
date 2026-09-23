"""Memory indexer daemon — `watchdog` Observer + Gemini embed + backend upsert.

After startup:
  1. Cold start: full scan `~/.ava/memory/**/*.md`, diff against the
     backend index (mtime + content_hash + provider fingerprint), embed
     missing / changed files, prune deleted entries.
  2. Start watchdog Observer to monitor fs events, push dirty paths to
     a queue.
  3. Main loop drains the queue every second (set dedup), batch
     embed + upsert / delete.

An incomplete reconcile retries on bounded backoff and never needs a restart.

Backed by `AVA_MEMORY_SEARCH_BACKEND` (default `numpy`). Switching
backends takes a restart; the cold-start scan rebuilds the new index.

Each file indexes as 0-or-1 description row (frontmatter `description`,
embedded on its own so short entity-bearing lines are not diluted by a
long body) + N body-chunk rows (~1800 chars each, ~200-char overlap,
paragraph-boundary aware). The backend's `search_topk` aggregates chunk
hits back to paths, so search callers see no difference.

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
import contextlib
import logging
import os
import queue
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import NoReturn

import numpy as np
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from services._pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.memory_indexer.backends.base import MemorySearchBackend, content_hash
from services.memory_indexer.backends.factory import get_backend
from services.memory_indexer.backends.probe import probe_backend

# Note-text chunking lives in chunking.py (split out 2026-09-20 when the
# hard-exit migration pushed this module against its line budget, task #4222);
# the private names stay re-exported here for the existing test surface.
from services.memory_indexer.chunking import (
    _chunk_body as _chunk_body,
)
from services.memory_indexer.chunking import (
    _file_rows as _file_rows,
)
from services.memory_indexer.chunking import (
    _split_note as _split_note,
)
from services.memory_indexer.embeddings import factory
from services.memory_indexer.embeddings.base import EmbeddingAPIError, EmbeddingProvider
from services.memory_indexer.embeddings.factory import get_provider
from shared.config import settings
from shared.daemon_health import Liveness, health_port, start_health_server, stop_health_server
from shared.daemon_shutdown import install_graceful_shutdown
from shared.log import init_gateway_process
from shared.paths import gateway_memory_dir
from shared.platform import CREATE_NO_WINDOW

_log = logging.getLogger("services.memory_indexer.daemon")

_MEMORY_ROOT = gateway_memory_dir()
_PIDFILE = settings.services.memory_indexer_pidfile
_LOOP_INTERVAL_S = 1.0
# Derive the ceiling from one provider batch's full retry budget: a single
# legitimate call can exceed 180s, and several shorter calls can compound.
# _process_paths beats before each provider/backend call, including commits
# and deletes, so calls cannot compound in one gap (default batch budget 606s).
# A false kill costs a rebuild; later true-wedge detection costs staleness
# only, since search keeps reading the existing index.
_LIVENESS_TIMEOUT_FLOOR_S = 180.0  # Historic ceiling; preserve other loop branches' slack.
# Covers executor scheduling, local processing, and loop resumption. Commit
# calls beat separately; NumPy's 300s upsert allowance fits the default 636s.
_LIVENESS_SAFETY_MARGIN_S = 30.0
# Startup and follow-up reconciles beat before and after file-granular chunks:
# a full rebuild can outlive the liveness ceiling. Chunks bound preparation and
# local work; _process_paths also beats before external calls within each chunk.
_RECONCILE_CHUNK_PATHS = 64
# How often the daemon fast-forwards the gateway checkout to origin/main —
# the refresh safety net (see module docstring). An hour bounds index
# staleness to ~1 consolidation cycle; the fetch is a no-op when main moved.
_CHECKOUT_REFRESH_INTERVAL_S = 3600.0
_BATCH_SIZE = 32
"""Gemini embed_content accepts multiple inputs per call; batching amortizes round-trips."""

_MD_SUFFIX = ".md"


def _liveness_timeout_s() -> float:
    return max(
        _LIVENESS_TIMEOUT_FLOOR_S,
        factory.worst_case_batch_seconds() + _LIVENESS_SAFETY_MARGIN_S,
    )


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
    """Whether a daemon is already running (via its pidfile).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_holds_daemon(_PIDFILE, "services.memory_indexer.daemon")


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


def _process_paths(
    backend: MemorySearchBackend, paths: set[Path], provider: EmbeddingProvider, liveness: Liveness
) -> None:
    """Process a batch of dirty paths: missing/foreign -> delete; else embed+upsert.

    A path is deleted when it is missing on disk OR when it lies outside the
    watched root. The second arm matters: rows indexed from an older era
    (e.g. the authoring checkout, before the gateway checkout split) carry
    paths that still exist on disk — so a mere existence check would keep
    them forever, and every search would show the note twice. The index is
    keyed by absolute path; only paths under the watched root belong in it.

    Embeds go through `provider`; rows are stamped with its fingerprint,
    and a row whose stored fingerprint differs from the configured
    provider's is re-embedded even when content is unchanged — a provider
    switch changes the semantic space, so old vectors must never be mixed
    with new ones (same dim is not the same space).

    A re-embedded file REPLACES its rows as one unit (issue #1946): after
    every row of the file is embedded, the new rows are upserted and the
    rows the current content no longer produces (a removed description, a
    shrunken body tail, an empty file) are deleted. If embedding fails part
    way, only the files whose rows are ALL embedded are committed — a
    partially-embedded file keeps its old rows intact, so its stored state
    stays consistent (all-old) and a follow-up reconcile retries it against
    the still-mismatching content hash.

    Embedding failures (`EmbeddingAPIError`) propagate after the
    provider's internal retries — the main loop records the failure and
    schedules a follow-up reconcile pass while the indexer stays available.

    Sync function — the caller uses ``asyncio.to_thread`` so the event
    loop is not blocked from serving the health probe (`/healthz`).
    Backends must be cross-thread safe (the milvus gRPC client is).
    """
    liveness.beat()
    root = _MEMORY_ROOT.resolve()
    to_delete: list[Path] = []
    to_embed: list[tuple[Path, float, str, str]] = []  # (path, mtime, hash, content)
    existing_meta = backend.all_meta()
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
        hash_ = content_hash(content)
        prev = existing_meta.get(str(p))
        if prev is not None and prev[1] == hash_ and prev[2] == provider.fingerprint:
            continue  # content unchanged AND same provider space; mtime touch only
        to_embed.append((p, mtime, hash_, content))

    for p in to_delete:
        liveness.beat()
        backend.delete(str(p))
        _log.info("[indexer] deleted %s", p)

    # Flatten each dirty file into its chunk rows (desc + body chunks), one
    # file at a time so per-file completeness stays visible. A file whose
    # content hash is unchanged is skipped before this point, so a re-embed
    # only happens when the file actually changed.
    file_rows: list[tuple[Path, list[tuple[float, str, str, int, str]]]] = []
    for path, mtime, hash_, content in sorted(to_embed, key=lambda t: str(t[0])):
        rows = [
            (mtime, hash_, kind, chunk_idx, text) for kind, chunk_idx, text in _file_rows(content)
        ]
        file_rows.append((path, rows))

    upsert_rows: list[tuple[str, float, str, np.ndarray, str, int]] = []
    total_by_file = {path: len(rows) for path, rows in file_rows}
    # A file that produces no rows (empty body, no description) is complete
    # by definition — committing it means deleting every row it used to have.
    embedded_files: list[Path] = [path for path, total in total_by_file.items() if total == 0]
    done_by_file: dict[Path, int] = {}
    flat_rows = [
        (path, mtime, hash_, kind, chunk_idx, text)
        for path, rows in file_rows
        for mtime, hash_, kind, chunk_idx, text in rows
    ]
    try:
        for i in range(0, len(flat_rows), _BATCH_SIZE):
            batch = flat_rows[i : i + _BATCH_SIZE]
            texts = [text for *_, text in batch]
            # One external call per gap; commit calls beat separately below.
            liveness.beat()
            vectors = provider.embed_batch(texts)
            for (path, mtime, hash_, kind, chunk_idx, _), vector in zip(
                batch, vectors, strict=True
            ):
                upsert_rows.append((str(path), mtime, hash_, vector, kind, chunk_idx))
                done = done_by_file.get(path, 0) + 1
                done_by_file[path] = done
                if done == total_by_file[path] and path not in embedded_files:
                    embedded_files.append(path)
    except EmbeddingAPIError:
        # Only files whose rows are ALL embedded commit; a partially-embedded
        # file keeps its old rows intact (consistent old state, re-embeds on
        # the next trigger against its still-mismatching hash).
        committed = _commit_files(backend, upsert_rows, embedded_files, file_rows, liveness)
        for path in committed:
            _log.info("[indexer] indexed %s", path)
        raise

    committed = _commit_files(backend, upsert_rows, embedded_files, file_rows, liveness)
    for path in committed:
        _log.info("[indexer] indexed %s", path)


def _commit_files(
    backend: MemorySearchBackend,
    upsert_rows: list[tuple[str, float, str, np.ndarray, str, int]],
    embedded_files: list[Path],
    file_rows: list[tuple[Path, list[tuple[float, str, str, int, str]]]],
    liveness: Liveness,
) -> list[Path]:
    """Commit the fully-embedded files: delete their obsolete tail rows first,
    then upsert the new rows. Returns the committed paths (input order).

    Delete-first is deliberate: if the upsert then fails, the file's
    surviving rows still carry the OLD content hash, so `all_meta` keeps
    disagreeing with the file and the next fs event / reconcile
    re-embeds it. An upsert-first failure would leave the tail while the new
    rows' mtime/hash make the file look current — the reconcile-blind state
    issue #1946 is about.
    """
    if not embedded_files:
        return []
    embedded = set(embedded_files)
    committed_rows = [row for row in upsert_rows if Path(row[0]) in embedded]
    limits_by_file = {path: _kind_limits(rows) for path, rows in file_rows if path in embedded}
    liveness.beat()
    backend.delete_stale_rows([(str(path), limits_by_file[path]) for path in embedded_files])
    liveness.beat()
    backend.upsert_many(committed_rows)
    return list(embedded_files)


def _kind_limits(rows: list[tuple[float, str, str, int, str]]) -> dict[str, int]:
    """kind -> how many rows the current file produces of that kind (the row
    counts are contiguous from 0, so the count doubles as the keep limit)."""
    limits: dict[str, int] = {}
    for row in rows:
        kind = row[2]
        limits[kind] = limits.get(kind, 0) + 1
    return limits


class _ReconcileRetrySchedule:
    """Bounded-backoff schedule for follow-up reconcile passes.

    The daemon keeps at most one follow-up pass scheduled. Each incomplete
    signal takes the next rung (base_s * 2 ** (n - 1), capped at cap_s): a
    reconcile pass that could not finish the dirty set, or a drain-batch embed
    failure that finds no pending schedule (ensure_scheduled counts it).
    A completed pass resets the ladder; drain failures never move a pending deadline.
    """

    def __init__(
        self, *, base_s: float, cap_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._base_s = base_s
        self._cap_s = cap_s
        self._clock = clock
        self._delay: float | None = None
        self._next_at: float | None = None

    @property
    def pending(self) -> bool:
        return self._next_at is not None

    def due(self) -> bool:
        return self._next_at is not None and self._clock() >= self._next_at

    def record_incomplete(self) -> float:
        # Doubling the capped delay implements the formula without an exponent
        # that overflows after a long quota outage.
        self._delay = min(self._base_s if self._delay is None else self._delay * 2, self._cap_s)
        self._next_at = self._clock() + self._delay
        return self._delay

    def record_pass_complete(self) -> None:
        self._delay = None
        self._next_at = None

    def ensure_scheduled(self) -> float | None:
        """Count an incomplete signal if idle; never move a pending deadline."""
        return None if self.pending else self.record_incomplete()

    def retry_in_s(self) -> float | None:
        return None if self._next_at is None else max(0.0, self._next_at - self._clock())


def _reconcile_health(retry: _ReconcileRetrySchedule) -> dict[str, object]:
    """Reconcile retry state for /healthz — informational, never gates 503."""
    retry_in = retry.retry_in_s()
    return {
        "reconcile_pending": retry.pending,
        "reconcile_retry_in_s": None if retry_in is None else round(retry_in, 1),
    }


def _reconcile(
    backend: MemorySearchBackend, provider: EmbeddingProvider, liveness: Liveness
) -> bool:
    """Diff disk vs index db; fill gaps at cold start and on scheduled follow-up passes.

    Runs in a thread executor. Returns True when the dirty set finishes, False
    when an embed failure truncates the pass so the main loop can schedule a retry.

    Rows whose path is outside the watched root are pruned even though the
    files still exist on disk (see `_process_paths`): they are leftovers
    from an era when the indexer embedded a different checkout, and they
    surface as stale duplicates in search results.

    A row whose stored provider fingerprint differs from the configured
    provider's is dirty even at the same mtime — the index was built in
    another semantic space (a provider switch), so every row must be
    re-embedded.

    The dirty set is worked in file-granular chunks (`_RECONCILE_CHUNK_PATHS`),
    beating `liveness` before and after each chunk. `_process_paths` also beats
    before each embed batch so a long rebuild stays alive. Chunk boundaries
    never split a file; its rows commit as one unit (issue #1946). An
    `EmbeddingAPIError` is logged and the remaining chunks are skipped.
    """
    disk = _scan_disk(_MEMORY_ROOT)
    indexed = backend.all_meta()
    indexed_paths = {Path(p) for p in indexed}
    disk_paths = set(disk.keys())
    root = _MEMORY_ROOT.resolve()

    dirty: set[Path] = set()
    # Changes / additions — mark dirty when mtime differs;
    # _process_paths further filters by hash internally.
    for path, mtime in disk.items():
        prev = indexed.get(str(path))
        if prev is None or prev[0] != mtime or prev[2] != provider.fingerprint:
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
        _log.info("[indexer] reconcile: %d dirty paths", len(dirty))
        # Sorted for a deterministic chunk order; boundaries fall between
        # paths, and a path is one whole file, so no file is ever split
        # across chunks (`_process_paths` commits a file's rows as one unit).
        ordered = sorted(dirty, key=str)
        try:
            for i in range(0, len(ordered), _RECONCILE_CHUNK_PATHS):
                chunk = set(ordered[i : i + _RECONCILE_CHUNK_PATHS])
                liveness.beat()  # per chunk: a rebuild outlives the liveness ceiling
                _process_paths(backend, chunk, provider, liveness)
                liveness.beat()  # chunk committed -> the rebuild is progressing
        except EmbeddingAPIError as exc:
            _log.error(
                "[indexer] reconcile embed failed: %r — daemon continues; a follow-up pass will retry",
                exc,
            )
            return False
    return True


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
    backend: MemorySearchBackend,
    dirty_queue: queue.Queue[Path],
    liveness: Liveness,
    provider: EmbeddingProvider,
    retry: _ReconcileRetrySchedule,
) -> None:
    """Main loop: every _LOOP_INTERVAL_S drain queue, dedup, batch process.

    `_process_paths` blocks (network embed + backend write calls); use
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
        # Waiting is safe: every loop tick keeps beating liveness until due.
        if retry.due():
            # Cancellation stays prompt; an in-flight to_thread worker finishes
            # in the background and is deliberately abandoned at exit (the
            # hard exit skips the executor join — see main()/_hard_exit).
            complete = await asyncio.to_thread(_reconcile, backend, provider, liveness)
            liveness.beat()  # the pass's tail must not stack with the next operation
            if complete:
                retry.record_pass_complete()
                _log.info("[indexer] follow-up reconcile complete: gap closed; retries cleared")
            else:
                delay = retry.record_incomplete()
                _log.warning("[indexer] reconcile incomplete; retry scheduled in %.1fs", delay)
        batch: set[Path] = set()
        # Drain the queue until empty — queue.Empty is the loop terminator.
        with suppress(queue.Empty):
            while True:
                batch.add(dirty_queue.get_nowait().resolve())
        if not batch:
            continue
        try:
            await asyncio.to_thread(_process_paths, backend, batch, provider, liveness)
            liveness.beat()  # embed batch returned -> loop is making progress
        except EmbeddingAPIError as exc:
            # Name the blast radius: which/how many paths lost this round.
            _log.error(
                "[indexer] embed failed for a batch of %d path(s) (%s): %r — "
                "a follow-up reconcile pass will retry",
                len(batch),
                ", ".join(str(p) for p in sorted(batch)[:5]) + ("..." if len(batch) > 5 else ""),
                exc,
            )
            # Failed paths stay dirty on disk; reconcile re-derives them from
            # the disk-vs-index diff, even if no further fs events arrive.
            delay = retry.ensure_scheduled()
            if delay is not None:
                _log.warning("[indexer] follow-up reconcile scheduled in %.1fs", delay)


async def _connect_backend_with_retry(
    provider: EmbeddingProvider,
    deadline_s: float = 30.0,
    *,
    probe_message: str | None = None,
) -> MemorySearchBackend:
    """Connect to the configured backend at daemon startup — `ava start`
    spawns the storage service (e.g. the milvus session) and the
    memory_indexer session in order; server initialization takes a few
    seconds, and within that race window connect may hit connection
    refused. This function retries every 2s up to deadline to give the
    backend time to come up. The backend is constructed for `provider`'s
    vector space (dim + fingerprint) — see `backends.factory`.

    Runtime backend calls (`_process_paths` etc.) do **not** use this
    retry — those are the healthcheck's responsibility (if the backend
    dies, the healthcheck restarts it; if memory_indexer itself crashes
    and exits, the healthcheck spawns a fresh process that goes through
    this retry).
    """
    backend = get_backend(dim=provider.dim, fingerprint=provider.fingerprint)
    start = time.time()
    last_exc: Exception | None = None
    while time.time() - start < deadline_s:
        try:
            await asyncio.to_thread(backend.connect)
            return backend
        except Exception as exc:
            last_exc = exc
            _log.info(
                "[indexer] %s backend not ready (%s: %s), retry in 2s...",
                backend.name,
                type(exc).__name__,
                exc,
            )
            await asyncio.sleep(2.0)
    suffix = f" — {probe_message}" if probe_message else ""
    raise RuntimeError(
        f"{backend.name} backend unreachable after {deadline_s}s: {last_exc}{suffix}"
    ) from last_exc


async def run() -> None:
    """Write pidfile -> start healthz server -> cold-start -> drain loop.

    Both before cold-start — cold-start may take tens of seconds
    embedding many files; pidfile / healthz being invisible would let
    watchdog misjudge death and spawn races (PR #254 fixed this).
    Publish the pidfile before binding healthz so identity-aware probes can verify it.
    """
    if _is_running():
        _log.info("[indexer] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)

    _write_pidfile()
    _log.info("[indexer] pidfile written: %s", _PIDFILE)

    # Fail fast before deriving liveness or binding healthz: an unknown
    # AVA_EMBEDDING_BACKEND must produce the clean configuration FATAL.
    try:
        provider = get_provider()
    except ValueError as exc:
        _log.critical("[indexer] embedding provider config invalid: %s", exc)
        sys.stderr.write(f"[memory_indexer] FATAL: {exc}\n")
        sys.exit(1)

    liveness = Liveness(_liveness_timeout_s())
    retry = _ReconcileRetrySchedule(
        base_s=settings.services.memory_indexer_reconcile_retry_backoff_seconds,
        cap_s=settings.services.memory_indexer_reconcile_retry_backoff_cap_seconds,
    )
    health = await start_health_server(
        "memory_indexer", liveness=liveness, extra=lambda: _reconcile_health(retry)
    )
    _log.info("[indexer] healthz listening on :%s", health_port("memory_indexer"))

    _MEMORY_ROOT.mkdir(parents=True, exist_ok=True)
    # Preflight the selected backend BEFORE the retry loop: a backend that can
    # never work (fatal) fails fast with the actionable fix instead of a 30s
    # retry storm; a merely-unreachable one rides into the retry loop with its
    # message attached to the terminal error (CTO ruling 2026-08-30 direction ②).
    preflight = probe_backend(settings.services.memory_search_backend)
    if preflight.fatal:
        _log.critical(
            "[indexer] %s backend preflight FAILED: %s",
            settings.services.memory_search_backend,
            preflight.message,
        )
        sys.stderr.write(f"[memory_indexer] FATAL: {preflight.message}\n")
        sys.exit(1)
    if preflight.message:
        _log.warning(
            "[indexer] %s backend preflight: %s",
            settings.services.memory_search_backend,
            preflight.message,
        )
    backend = await _connect_backend_with_retry(provider, probe_message=preflight.message)
    _log.info("[indexer] connected to %s backend", backend.name)
    dirty_queue: queue.Queue[Path] = queue.Queue()
    handler = _MarkdownEventHandler(dirty_queue)
    observer = Observer()
    observer.schedule(handler, str(_MEMORY_ROOT), recursive=True)
    observer.start()
    _log.info("[indexer] watching %s", _MEMORY_ROOT)

    try:
        if not await asyncio.to_thread(_reconcile, backend, provider, liveness):
            delay = retry.record_incomplete()
            _log.warning("[indexer] startup reconcile incomplete; retry scheduled in %.1fs", delay)
        await _drain_loop(backend, dirty_queue, liveness, provider, retry=retry)
    finally:
        observer.stop()
        observer.join(timeout=5.0)
        with suppress(Exception):
            backend.close()
        await stop_health_server(health)
        _remove_pidfile()
        _log.info("[indexer] daemon stopped")


def _hard_exit(code: int) -> NoReturn:
    """End the process now, skipping interpreter teardown. Never returns.

    Teardown is precisely what hangs: the reconcile pass and every batch embed
    run on the default executor (``asyncio.to_thread``), and their runs are
    routinely multi-minute (a full rebuild beats the liveness ceiling by
    design). ``asyncio.Runner.close`` joins that executor behind CPython's
    ``THREAD_JOIN_TIMEOUT`` cap (300 s) — the stop flow's entire budget
    (`PAUSE_TIMEOUT_SECONDS`) — and a pass still in flight at SIGTERM then
    keeps interpreter teardown waiting with no bound at all (measured: a
    ``shutdown(wait=False)`` worker is still joined at exit, task #3940).
    Nothing after ``run()`` needs that pass: failed paths stay dirty on disk
    and the next process's reconcile re-derives them. Logs are flushed first:
    they are the one thing a skipped teardown would lose. Same shape as
    services/agent_ops/daemon.py and services/pitr/uploader_daemon.py.
    """
    with contextlib.suppress(Exception):
        from loguru import logger as _loguru

        _loguru.remove()  # closes (and so flushes) every sink
    with contextlib.suppress(Exception):
        logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()
    os._exit(code)


def main() -> None:
    """Entry point: log init + install the graceful-stop signal + run asyncio.

    Does not call `assert_schema_current` — memory_indexer does not
    read the main DB (only via the search backend); there is no main-DB
    schema-drift surface.
    """
    init_gateway_process(name="memory_indexer")
    install_graceful_shutdown("indexer")
    code = 0
    # `asyncio.Runner`, not `asyncio.run`: `run` closes in a `finally` that
    # awaits `shutdown_default_executor`, joining the default executor's
    # workers — a reconcile pass or embed batch among them — and a stop
    # signal must never wait on those (see `_hard_exit`). The runner is
    # therefore never closed: after the explicit drain below, teardown is
    # skipped by the hard exit.
    runner = asyncio.Runner()
    try:
        runner.run(run())
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a retry must not abort the bounded exit
        _log.info("[indexer] received interrupt, shutting down")
        # The signal path skips Runner's own cancellation, so drain the loop's
        # tasks explicitly: run()'s finally still stops the observer, closes
        # the backend and removes the pidfile. The executor is deliberately
        # NOT drained.
        loop = runner.get_loop()
        tasks = asyncio.all_tasks(loop)
        for task in tasks:
            task.cancel()
        results = loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            _log.error("[indexer] async shutdown failed: %r", failures)
            code = 1
    except Exception:
        _log.exception("[indexer] daemon crashed — uncaught exception escaped run()")
        code = 1
    finally:
        _remove_pidfile()
    _hard_exit(code)


if __name__ == "__main__":
    main()
