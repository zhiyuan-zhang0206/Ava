"""Memory indexer daemon — `watchdog` Observer + Gemini embed + backend upsert.

Cold-start reconciliation embeds missing or changed Markdown rows and prunes
removed entries. A watchdog Observer feeds a deduplicated dirty queue. An
incomplete reconcile is logged; no follow-up pass is scheduled. Rows contain a
description and body chunks, aggregated back to paths by search. Backend changes
require restart. API key and provider configuration belong to the process
configuration owner.

Run: python -m services.derived.memory_indexer.daemon
Health: HTTP /healthz on :8105.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import signal
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import numpy as np
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentity, MachineRoles
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.daemon.health import Liveness, start_health_server, stop_health_server
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.deploy.maintenance import admission
from base.host.exit_codes import hard_exit as _hard_exit
from base.log import prepare_process_logging
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.packages.plugin_providers import build_model_catalog
from base.paths import ava_home, gateway_memory_dir
from base.telemetry.delivery.receipts import DrainStatus
from base.telemetry.pipeline_build import build_pipeline
from base.telemetry.process_logging import init_gateway_process
from services.derived.memory_indexer import config as indexer_config
from services.derived.memory_indexer.backends.base import MemorySearchBackend, content_hash
from services.derived.memory_indexer.backends.factory import get_backend
from services.derived.memory_indexer.backends.probe import probe_backend
from services.derived.memory_indexer.chunking import _file_rows
from services.derived.memory_indexer.config import MemoryIndexerInputs
from services.derived.memory_indexer.embeddings.base import EmbeddingAPIError, EmbeddingProvider
from services.derived.memory_indexer.embeddings.factory import get_provider
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile

_log = logging.getLogger("services.derived.memory_indexer.daemon")


def memory_root(*, roles: MachineRoles) -> Path:
    """The consolidated memory checkout this daemon indexes, resolved on use."""
    return gateway_memory_dir(roles=roles)


def _endpoint(config: ConfigBoot, *, runtime: Path) -> ServiceEndpoint:
    runtime.mkdir(parents=True, exist_ok=True)
    return ServiceEndpoints.from_boot(config, runtime=runtime).of("memory_indexer")


def _pidfile(config: ConfigBoot, *, runtime: Path) -> Path:
    return _endpoint(config, runtime=runtime).pidfile


LOOP_INTERVAL_S = 1.0
# Startup reconciles beat before and after file-granular chunks:
# a full rebuild can outlive the liveness ceiling. Chunks bound preparation and
# local work; process_paths also beats before external calls within each chunk.
RECONCILE_CHUNK_PATHS = 64
BATCH_SIZE = 32
"""Gemini embed_content accepts multiple inputs per call; batching amortizes round-trips."""

_MD_SUFFIX = ".md"


class MarkdownEventHandler(FileSystemEventHandler):
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


def write_pidfile(config: ConfigBoot, *, runtime: Path) -> None:
    if not acquire_pidfile(
        _pidfile(config, runtime=runtime), "services.derived.memory_indexer.daemon"
    ):
        _log.info(
            "[memory_indexer] daemon already running (pidfile=%s), exiting",
            _pidfile(config, runtime=runtime),
        )
        sys.exit(1)


def _remove_pidfile(config: ConfigBoot, *, runtime: Path) -> None:
    remove_pidfile(_pidfile(config, runtime=runtime))


def _is_running(config: ConfigBoot, *, runtime: Path) -> bool:
    """Whether a daemon is already running (via its pidfile).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_holds_daemon(
        _pidfile(config, runtime=runtime), "services.derived.memory_indexer.daemon"
    )


def scan_disk(root: Path) -> dict[Path, float]:
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


@dataclass
class _EmbedProgress:
    """Rows embedded so far and the files whose rows are all embedded."""

    embedded_files: list[Path]
    upsert_rows: list[tuple[str, float, str, np.ndarray, str, int]] = field(
        default_factory=list[tuple[str, float, str, np.ndarray, str, int]]
    )
    done_by_file: dict[Path, int] = field(default_factory=dict[Path, int])


def _classify_dirty(
    backend: MemorySearchBackend, paths: set[Path], provider: EmbeddingProvider, root: Path
) -> tuple[list[Path], list[tuple[Path, float, str, str]]]:
    """Split dirty paths into `(to delete, to embed as (path, mtime, hash, content))`."""
    to_delete: list[Path] = []
    to_embed: list[tuple[Path, float, str, str]] = []
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
    return to_delete, to_embed


def _embed_batches(
    provider: EmbeddingProvider,
    flat_rows: list[tuple[Path, float, str, str, int, str]],
    total_by_file: dict[Path, int],
    progress: _EmbedProgress,
    liveness: Liveness,
) -> None:
    """Embed `flat_rows` in batches, recording each embedded row (and each completed file)."""
    for i in range(0, len(flat_rows), BATCH_SIZE):
        batch = flat_rows[i : i + BATCH_SIZE]
        texts = [text for *_, text in batch]
        # One external call per gap; commit calls beat separately below.
        liveness.beat()
        vectors = provider.embed_batch(texts)
        for (path, mtime, hash_, kind, chunk_idx, _), vector in zip(batch, vectors, strict=True):
            progress.upsert_rows.append((str(path), mtime, hash_, vector, kind, chunk_idx))
            done = progress.done_by_file.get(path, 0) + 1
            progress.done_by_file[path] = done
            if done == total_by_file[path] and path not in progress.embedded_files:
                progress.embedded_files.append(path)


def process_paths(
    backend: MemorySearchBackend,
    paths: set[Path],
    provider: EmbeddingProvider,
    liveness: Liveness,
    *,
    machine: MachineIdentity,
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
    Backends must be cross-thread safe (the HTTP client and the pgvector pool are).
    """
    liveness.beat()
    root = memory_root(roles=machine.role()).resolve()
    to_delete, to_embed = _classify_dirty(backend, paths, provider, root)

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

    total_by_file = {path: len(rows) for path, rows in file_rows}
    progress = _EmbedProgress(
        # A file that produces no rows (empty body, no description) is complete
        # by definition — committing it means deleting every row it used to have.
        embedded_files=[path for path, total in total_by_file.items() if total == 0]
    )
    flat_rows = [
        (path, mtime, hash_, kind, chunk_idx, text)
        for path, rows in file_rows
        for mtime, hash_, kind, chunk_idx, text in rows
    ]
    try:
        _embed_batches(provider, flat_rows, total_by_file, progress, liveness)
    except EmbeddingAPIError:
        # Only files whose rows are ALL embedded commit; a partially-embedded
        # file keeps its old rows intact (consistent old state, re-embeds on
        # the next trigger against its still-mismatching hash).
        committed = _commit_files(
            backend, progress.upsert_rows, progress.embedded_files, file_rows, liveness
        )
        for path in committed:
            _log.info("[indexer] indexed %s", path)
        raise

    committed = _commit_files(
        backend, progress.upsert_rows, progress.embedded_files, file_rows, liveness
    )
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


def reconcile(
    backend: MemorySearchBackend,
    provider: EmbeddingProvider,
    liveness: Liveness,
    *,
    machine: MachineIdentity,
) -> bool:
    """Diff disk vs index db once at cold start.

    Runs in a thread executor. Returns True when the dirty set finishes, False
    when an embed failure truncates the pass; no follow-up pass is scheduled.

    Rows whose path is outside the watched root are pruned even though the
    files still exist on disk (see `process_paths`): they are leftovers
    from an era when the indexer embedded a different checkout, and they
    surface as stale duplicates in search results.

    A row whose stored provider fingerprint differs from the configured
    provider's is dirty even at the same mtime — the index was built in
    another semantic space (a provider switch), so every row must be
    re-embedded.

    The dirty set is worked in file-granular chunks (`RECONCILE_CHUNK_PATHS`),
    beating `liveness` before and after each chunk. `process_paths` also beats
    before each embed batch so a long rebuild stays alive. Chunk boundaries
    never split a file; its rows commit as one unit (issue #1946). An
    `EmbeddingAPIError` is logged and the remaining chunks are skipped.
    """
    disk = scan_disk(memory_root(roles=machine.role()))
    indexed = backend.all_meta()
    indexed_paths = {Path(p) for p in indexed}
    disk_paths = set(disk.keys())
    root = memory_root(roles=machine.role()).resolve()

    dirty: set[Path] = set()
    # Changes / additions — mark dirty when mtime differs;
    # process_paths further filters by hash internally.
    for path, mtime in disk.items():
        prev = indexed.get(str(path))
        if prev is None or prev[0] != mtime or prev[2] != provider.fingerprint:
            dirty.add(path)
    # Deletions: indexed rows that vanished from disk, or that live outside
    # the watched root (stale-checkout leftovers) — process_paths deletes
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
        # across chunks (`process_paths` commits a file's rows as one unit).
        ordered = sorted(dirty, key=str)
        try:
            for i in range(0, len(ordered), RECONCILE_CHUNK_PATHS):
                chunk = set(ordered[i : i + RECONCILE_CHUNK_PATHS])
                liveness.beat()  # per chunk: a rebuild outlives the liveness ceiling
                process_paths(backend, chunk, provider, liveness, machine=machine)
                liveness.beat()  # chunk committed -> the rebuild is progressing
        except EmbeddingAPIError as exc:
            _log.error(
                "[indexer] reconcile embed failed: %r — index remains incomplete; explicit rebuild required",
                exc,
            )
            return False
    return True


async def drain_loop(
    backend: MemorySearchBackend,
    dirty_queue: queue.Queue[Path],
    liveness: Liveness,
    provider: EmbeddingProvider,
    *,
    machine: MachineIdentity,
) -> None:
    """Main loop: every LOOP_INTERVAL_S drain queue, dedup, batch process.

    `process_paths` blocks (network embed + backend write calls); use
    ``asyncio.to_thread`` so the event loop can still serve the health
    probe during embedding. Failed paths are not requeued; a later file event or
    explicit rebuild may attempt them again.
    """
    _log.info("[indexer] daemon loop started, pid=%s", os.getpid())
    while True:
        liveness.beat()
        await asyncio.sleep(LOOP_INTERVAL_S)
        if admission.quiesced():
            continue  # the pgvector backend borrows the pool; dirty paths wait in the queue
        batch: set[Path] = set()
        # Drain the queue until empty — queue.Empty is the loop terminator.
        with suppress(queue.Empty):
            while True:
                batch.add(dirty_queue.get_nowait().resolve())
        if not batch:
            continue
        try:
            await asyncio.to_thread(
                process_paths, backend, batch, provider, liveness, machine=machine
            )
            liveness.beat()  # embed batch returned -> loop is making progress
        except EmbeddingAPIError as exc:
            # Name the blast radius: which/how many paths lost this round.
            _log.error(
                "[indexer] embed failed for a batch of %d path(s) (%s): %r — "
                "explicit rebuild or a later file change is required",
                len(batch),
                ", ".join(str(p) for p in sorted(batch)[:5]) + ("..." if len(batch) > 5 else ""),
                exc,
            )


async def connect_backend_with_retry(
    database: Database,
    provider: EmbeddingProvider,
    deadline_s: float = 30.0,
    *,
    name: str,
    uri_reader: Callable[[], str],
    probe_message: str | None = None,
) -> MemorySearchBackend:
    """Connect to the configured backend at daemon startup — `ava start`
    spawns the storage service (the memory-search session) and the
    memory_indexer session in order; server initialization takes a few
    seconds, and within that race window connect may hit connection
    refused. This function retries every 2s up to deadline to give the
    backend time to come up. The backend is constructed for `provider`'s
    vector space (dim + fingerprint) — see `backends.factory`.

    Runtime backend calls (`process_paths` etc.) do **not** use this
    retry — those are the healthcheck's responsibility (if the backend
    dies, the healthcheck restarts it; if memory_indexer itself crashes
    and exits, the healthcheck spawns a fresh process that goes through
    this retry).
    """
    backend = get_backend(
        database,
        dim=provider.dim,
        fingerprint=provider.fingerprint,
        name=name,
        uri_reader=uri_reader,
    )
    start = time.monotonic()
    last_exc: Exception | None = None
    while time.monotonic() - start < deadline_s:
        try:
            await asyncio.to_thread(backend.connect)
            return backend
        except (ConnectionError, TimeoutError, httpx.ConnectError, httpx.ConnectTimeout) as exc:
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


def _close_backend(backend: MemorySearchBackend) -> None:
    """Close the backend at shutdown; a failure is logged so the remaining teardown still runs."""
    try:
        backend.close()
    except Exception:
        _log.warning("[indexer] backend close failed during shutdown", exc_info=True)


async def run(
    *,
    config: ConfigBoot,
    runtime: Path,
    database: Callable[[], Database],
    image: LoadedCommit,
    machine: MachineIdentity,
) -> None:
    """Write pidfile -> start healthz server -> cold-start -> drain loop.

    Both before cold-start — cold-start may take tens of seconds
    embedding many files; pidfile / healthz being invisible would let
    watchdog misjudge death and spawn races (PR #254 fixed this).
    Publish the pidfile before binding healthz so identity-aware probes can verify it.
    """
    inputs = MemoryIndexerInputs.from_boot(config)
    if _is_running(config, runtime=runtime):
        _log.info(
            "[indexer] daemon already running (pidfile=%s), exiting",
            _pidfile(config, runtime=runtime),
        )
        sys.exit(1)

    write_pidfile(config, runtime=runtime)
    _log.info("[indexer] pidfile written: %s", _pidfile(config, runtime=runtime))

    # Fail fast before deriving liveness or binding healthz: an unknown
    # AVA_EMBEDDING_BACKEND must produce the clean configuration FATAL.
    try:
        catalog = build_model_catalog()
        provider = get_provider(
            inputs.embedding_name(),
            catalog=catalog,
            timeout_reader=inputs.embed_timeout,
            api_key_reader=inputs.api_key,
        )
    except ValueError as exc:
        _log.critical("[indexer] embedding provider config invalid: %s", exc)
        sys.stderr.write(f"[memory_indexer] FATAL: {exc}\n")
        sys.exit(1)

    liveness = Liveness(
        indexer_config.liveness_timeout_seconds(
            inputs.embedding_name(), timeout_seconds=inputs.embed_timeout()
        )
    )
    endpoint = _endpoint(config, runtime=runtime)
    health = await start_health_server(
        "memory_indexer",
        endpoint.health_port,
        liveness=liveness,
        image=image,
    )
    _log.info("[indexer] healthz listening on :%s", endpoint.health_port)

    root = memory_root(roles=machine.role())
    root.mkdir(parents=True, exist_ok=True)
    # Preflight the selected backend BEFORE the retry loop: a backend that can
    # never work (fatal) fails fast with the actionable fix instead of a 30s
    # retry storm; a merely-unreachable one rides into the retry loop with its
    # message attached to the terminal error (CTO ruling 2026-08-30 direction ②).
    db = database()
    preflight = probe_backend(inputs.backend_name(), db, uri_reader=inputs.search_uri)
    if preflight.fatal:
        _log.critical(
            "[indexer] %s backend preflight FAILED: %s",
            inputs.backend_name(),
            preflight.message,
        )
        sys.stderr.write(f"[memory_indexer] FATAL: {preflight.message}\n")
        sys.exit(1)
    if preflight.message:
        _log.warning(
            "[indexer] %s backend preflight: %s",
            inputs.backend_name(),
            preflight.message,
        )
    backend = await connect_backend_with_retry(
        db,
        provider,
        name=inputs.backend_name(),
        uri_reader=inputs.search_uri,
        probe_message=preflight.message,
    )
    _log.info("[indexer] connected to %s backend", backend.name)
    dirty_queue: queue.Queue[Path] = queue.Queue()
    handler = MarkdownEventHandler(dirty_queue)
    observer = Observer()
    observer.schedule(handler, str(root), recursive=True)
    observer.start()
    _log.info("[indexer] watching %s", root)

    try:
        if not await asyncio.to_thread(reconcile, backend, provider, liveness, machine=machine):
            _log.warning("[indexer] startup index incomplete; explicit rebuild required")
        await drain_loop(backend, dirty_queue, liveness, provider, machine=machine)
    finally:
        observer.stop()
        observer.join(timeout=5.0)
        _close_backend(backend)
        await stop_health_server(health)
        _remove_pidfile(config, runtime=runtime)
        _log.info("[indexer] daemon stopped")


def main() -> None:
    """Entry point: log init + install the graceful-stop signal + run asyncio.

    Does not call `assert_schema_current` — memory_indexer does not
    read the main DB (only via the search backend); there is no main-DB
    schema-drift surface.
    """
    config = ConfigBoot()
    config.boot()
    machine = machine_from_boot(config)
    process_home = ava_home().resolve()
    image = LoadedCommit.capture()
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="memory_indexer")

    def database() -> Database:
        return Database(
            db_config_from_boot(config), gate=gate, direct_url=ClusterDirectUrl(machine.host)
        )

    pipeline = build_pipeline(database=database, machine=machine, home=process_home)
    code = 0
    try:
        init_gateway_process(
            name="memory_indexer",
            producer=lambda: pipeline,
            machine_reader=machine.name,
            image=image,
        )
        install_graceful_shutdown("memory_indexer")
        # Keep executor joins outside the stop path: drain tasks explicitly,
        # then hard-exit without closing Runner and its default executor.
        runner = asyncio.Runner()
        try:
            runner.run(
                run(
                    config=config,
                    runtime=process_home / "run",
                    database=database,
                    image=image,
                    machine=machine,
                )
            )
        except KeyboardInterrupt:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a retry must not abort the bounded exit
            _log.info("[indexer] received interrupt, shutting down")
            # The signal path skips Runner's own cancellation, so drain the loop's
            # tasks explicitly: run()'s finally still stops the observer, closes
            # the backend and removes the pidfile. The executor is deliberately
            # NOT drained.
            failures = cancel_and_drain(runner)
            if failures:
                _log.error("[indexer] async shutdown failed: %r", failures)
                code = 1
        except Exception:
            _log.exception("[indexer] daemon crashed — uncaught exception escaped run()")
            code = 1
        finally:
            _remove_pidfile(config, runtime=process_home / "run")
    finally:
        try:
            drain = pipeline.stop(timeout=2)
            if drain.status is DrainStatus.UNFINISHED:
                _log.warning("event pipeline stop unfinished: %s", drain)
        except Exception:
            _log.exception("event pipeline stop failed")
            code = 1
    _hard_exit(code)


if __name__ == "__main__":
    prepare_process_logging()
    main()
