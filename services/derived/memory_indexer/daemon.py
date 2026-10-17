from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import numpy as np

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
from base.paths import gateway_memory_dir, meiva_home
from base.telemetry.delivery.receipts import DrainStatus
from base.telemetry.pipeline_build import build_pipeline
from base.telemetry.process_logging import init_gateway_process
from services.derived.memory_indexer import config as indexer_config
from services.derived.memory_indexer.backends.base import MemorySearchBackend, content_hash
from services.derived.memory_indexer.backends.factory import get_backend
from services.derived.memory_indexer.backends.probe import probe_backend
from services.derived.memory_indexer.chunking import _file_rows
from services.derived.memory_indexer.config import MemoryIndexerConfig, MemoryIndexerInputs
from services.derived.memory_indexer.embeddings.base import EmbeddingAPIError, EmbeddingProvider
from services.derived.memory_indexer.embeddings.factory import get_provider
from services.derived.memory_indexer.patrol_status import (
    PatrolStatus,
    emit_patrol_event,
    retry_delay_s,
)
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


_MD_SUFFIX = ".md"


def is_indexable(path: Path, *, config: MemoryIndexerConfig) -> bool:
    return path.suffix == _MD_SUFFIX and path.name not in config.excluded_basenames


def scan_disk(root: Path, *, config: MemoryIndexerConfig) -> dict[Path, str]:
    if not root.exists():
        return {}
    result: dict[Path, str] = {}
    for p in root.rglob(f"*{_MD_SUFFIX}"):
        if not p.is_file() or p.is_symlink() or not is_indexable(p, config=config):
            continue
        try:
            result[p.resolve()] = content_hash(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            _log.warning("[indexer] skip %s: %r", p, exc)
    return result


def _is_stale(
    record: tuple[float, str, str] | None, hash_: str, provider: EmbeddingProvider
) -> bool:
    return record is None or record[1] != hash_ or record[2] != provider.fingerprint


def dirty_paths(
    disk: dict[Path, str], indexed: dict[str, tuple[float, str, str]], provider: EmbeddingProvider
) -> set[Path]:
    dirty = {
        path for path, hash_ in disk.items() if _is_stale(indexed.get(str(path)), hash_, provider)
    }
    dirty.update(Path(path) for path in set(indexed) - {str(path) for path in disk})
    return dirty


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


@dataclass
class _EmbedProgress:
    """Rows embedded so far and the files whose rows are all embedded."""

    embedded_files: list[Path]
    upsert_rows: list[tuple[str, float, str, np.ndarray, str, int]] = field(
        default_factory=list[tuple[str, float, str, np.ndarray, str, int]]
    )
    done_by_file: dict[Path, int] = field(default_factory=dict[Path, int])


def _classify_dirty(
    backend: MemorySearchBackend,
    paths: set[Path],
    provider: EmbeddingProvider,
    root: Path,
    *,
    config: MemoryIndexerConfig,
) -> tuple[list[Path], list[tuple[Path, float, str, str]]]:
    to_delete: list[Path] = []
    to_embed: list[tuple[Path, float, str, str]] = []
    existing_meta = backend.all_meta()
    for p in paths:
        if (
            not is_indexable(p, config=config)
            or not p.exists()
            or not p.is_file()
            or not p.is_relative_to(root)
        ):
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
    *,
    config: MemoryIndexerConfig,
) -> None:
    """Embed `flat_rows` in batches, recording each embedded row (and each completed file)."""
    for i in range(0, len(flat_rows), config.batch_size):
        batch = flat_rows[i : i + config.batch_size]
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
    config: MemoryIndexerConfig,
) -> None:
    liveness.beat()
    root = memory_root(roles=machine.role()).resolve()
    to_delete, to_embed = _classify_dirty(backend, paths, provider, root, config=config)

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
        _embed_batches(provider, flat_rows, total_by_file, progress, liveness, config=config)
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
    config: MemoryIndexerConfig,
    status: PatrolStatus,
    root: Path,
) -> bool:
    started = time.monotonic()
    disk = scan_disk(root, config=config)
    scanned_at = time.monotonic()
    indexed = backend.all_meta()
    if not disk and indexed:
        status.begin(len(indexed))
        _log.error(
            "[indexer] patrol scanned no file under %s while the index holds %d path(s); "
            "refusing to prune and retrying — a vanished root must not empty the index",
            root,
            len(indexed),
        )
        return False
    dirty = dirty_paths(disk, indexed, provider)
    compared_at = time.monotonic()
    status.begin(len(dirty))

    complete = True
    if dirty:
        _log.info("[indexer] patrol: %d dirty path(s)", len(dirty))
        # Sorted for a deterministic chunk order; boundaries fall between
        # paths, and a path is one whole file, so no file is ever split
        # across chunks (`process_paths` commits a file's rows as one unit).
        ordered = sorted(dirty, key=str)
        try:
            for i in range(0, len(ordered), config.reconcile_chunk_paths):
                chunk = set(ordered[i : i + config.reconcile_chunk_paths])
                liveness.beat()  # per chunk: a rebuild outlives the liveness ceiling
                process_paths(backend, chunk, provider, liveness, machine=machine, config=config)
                status.settled(len(chunk))
                liveness.beat()  # chunk committed -> the rebuild is progressing
        except EmbeddingAPIError as exc:
            complete = False
            _log.error(
                "[indexer] patrol embed failed: %r — %d path(s) still behind; the next pass retries",
                exc,
                status.pending,
            )

    finished = time.monotonic()
    _log.info(
        "[indexer] patrol pass: %d file(s) scanned, %d stale; %.2fs scan / %.2fs compare / "
        "%.2fs embed (%.2fs total)",
        len(disk),
        len(dirty),
        scanned_at - started,
        compared_at - scanned_at,
        finished - compared_at,
        finished - started,
    )
    if finished - started > config.patrol_interval_s:
        _log.warning(
            "[indexer] patrol pass took %.0fs — longer than the %.0fs patrol interval",
            finished - started,
            config.patrol_interval_s,
        )
    return complete


async def _patrol_once(
    backend: MemorySearchBackend,
    provider: EmbeddingProvider,
    liveness: Liveness,
    *,
    machine: MachineIdentity,
    config: MemoryIndexerConfig,
    status: PatrolStatus,
    root: Path,
) -> bool:
    try:
        return await asyncio.to_thread(
            reconcile,
            backend,
            provider,
            liveness,
            machine=machine,
            config=config,
            status=status,
            root=root,
        )
    except Exception:
        _log.exception("[indexer] patrol pass failed")
        return False


async def patrol_loop(
    backend: MemorySearchBackend,
    provider: EmbeddingProvider,
    liveness: Liveness,
    *,
    machine: MachineIdentity,
    config: MemoryIndexerConfig,
    status: PatrolStatus,
    clock: Callable[[], float],
    wait: Callable[[float], Awaitable[None]],
    root: Path,
) -> None:
    _log.info(
        "[indexer] patrol every %ss (retry %ss doubling, limit %s)",
        config.patrol_interval_s,
        config.retry_backoff_s,
        config.retry_limit,
    )
    next_patrol = clock()
    while True:
        liveness.beat()
        await wait(config.loop_interval_s)
        if admission.quiesced():
            continue
        if clock() < next_patrol:
            continue
        complete = await _patrol_once(
            backend,
            provider,
            liveness,
            machine=machine,
            config=config,
            status=status,
            root=root,
        )
        liveness.beat()
        if complete:
            status.succeeded()
            next_patrol = clock() + config.patrol_interval_s
        else:
            status.failed()
            if status.over_limit:
                _log.error(
                    "[indexer] patrol over its retry limit (%d consecutive failures); "
                    "retrying at the normal cadence",
                    status.consecutive_failures,
                )
                next_patrol = clock() + config.patrol_interval_s
            else:
                delay = retry_delay_s(config, status.consecutive_failures)
                _log.warning("[indexer] patrol incomplete; retry in %.0fs", delay)
                next_patrol = clock() + delay
        emit_patrol_event(status)


async def connect_backend_with_retry(
    database: Database,
    provider: EmbeddingProvider,
    deadline_s: float = 30.0,
    *,
    name: str,
    uri_reader: Callable[[], str],
    probe_message: str | None = None,
) -> MemorySearchBackend:
    """Connect to the configured backend at daemon startup — `meiva start`
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
    tuning: MemoryIndexerConfig,
) -> None:
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
    # MEIVA_EMBEDDING_BACKEND must produce the clean configuration FATAL.
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
    status = PatrolStatus(retry_limit=tuning.retry_limit)
    endpoint = _endpoint(config, runtime=runtime)
    health = await start_health_server(
        "memory_indexer",
        endpoint.health_port,
        liveness=liveness,
        extra=status.health_fields,
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

    try:
        await patrol_loop(
            backend,
            provider,
            liveness,
            machine=machine,
            config=tuning,
            status=status,
            clock=time.monotonic,
            wait=asyncio.sleep,
            root=root,
        )
    finally:
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
    process_home = meiva_home().resolve()
    image = LoadedCommit.capture()
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="memory_indexer")

    def database() -> Database:
        return Database(
            db_config_from_boot(config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host),
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
                    tuning=MemoryIndexerConfig(),
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
