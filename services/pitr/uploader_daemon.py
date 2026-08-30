"""Single-worker PITR GCS uploader daemon."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from services._pidfile import acquire_pidfile, remove_pidfile
from services.pitr.gcs_store import GCSObjectStore
from services.pitr.object_store import PermanentObjectStoreError, TransientObjectStoreError
from services.pitr.state import ArchiveHealth, health_state
from services.pitr.uploader import (
    AckCorruptionError,
    PitrUploader,
    RemoteCollisionError,
    WalSourceTooLargeError,
)
from shared import health_schema
from shared.config import settings
from shared.daemon_health import Liveness, start_health_server, stop_health_server
from shared.daemon_shutdown import install_graceful_shutdown
from shared.log import init_gateway_process
from shared.paths import ava_home

_log = logging.getLogger("services.pitr.uploader_daemon")


def build_uploader() -> PitrUploader:
    # Each read is a full settings.<domain>.<field> access: the consumer-guard
    # AST scan only recognizes that shape, and the gateway profile must carry
    # the physical_backup domain for this daemon to construct it at runtime.
    if not settings.physical_backup.pitr_enabled:
        raise RuntimeError("PITR uploader cannot start while AVA_PITR_ENABLED is off")
    key_path = settings.physical_backup.pitr_backup_key_file
    credentials = settings.physical_backup.pitr_gcs_credentials_file
    if key_path is None or credentials is None:
        raise RuntimeError("validated PITR secrets are missing")
    root = ava_home() / "physical-backup"
    return PitrUploader(
        spool=root / "spool",
        ack_dir=root / "ack",
        staging=root / "staging",
        prefix=settings.physical_backup.pitr_gcs_prefix,
        key=key_path.read_bytes(),
        key_id=settings.physical_backup.pitr_backup_key_id,
        store=GCSObjectStore(
            project=settings.physical_backup.pitr_gcs_project,
            bucket=settings.physical_backup.pitr_gcs_bucket,
            credentials_file=credentials,
        ),
    )


_CRITICAL_BACKOFF_S = 300.0
# One heartbeat cadence for every loop wait — intentional backoff and an
# in-flight upload alike: the loop never sits silently longer than this
# without proving it is alive.
_HEARTBEAT_S = 30.0
_ACK_SUFFIX = ".ack.json"


@dataclass
class _LoopErrors:
    """Accumulated upload failure counters, read by the health components.

    Carried through upload_loop so the health payload shows real error
    signals instead of a hardcoded zero (QA #4696 yellow 2)."""

    transient: int = 0
    critical: int = 0

    @property
    def total(self) -> int:
        return self.transient + self.critical


def _disk_components(
    uploader: PitrUploader, *, warn_bytes: int, hard_bytes: int
) -> list[dict[str, object]]:
    """Disk-footprint component — GATED readiness (QA #4696/405 ruling A).

    A combined spool+staging footprint past the hard bound means the daemon
    cannot do its job; that genuinely degrades readiness and the watchdog
    respawn (which re-reads config) is the right recovery, so this component
    keeps the default gate_readiness=True.
    """
    footprint = uploader.disk_footprint()
    total = footprint.total_bytes
    if total >= hard_bytes:
        status = health_schema.DOWN
        detail = "combined WAL spool and staging reached the hard disk bound"
    elif total >= warn_bytes:
        status = health_schema.DEGRADED
        detail = "combined WAL spool and staging reached the warning disk bound"
    else:
        status = health_schema.OK
        detail = None
    return [health_schema.component("pitr_disk", status, detail=detail)]


def _unacked_health(uploader: PitrUploader, upload_errors_total: int) -> ArchiveHealth:
    """Project local spool vs remote ACK through state.py's health model.

    QA #4681 block 3 / #4696: AVA_PITR_UNACKED_* are live health inputs, not
    dead configuration — the oldest un-ACKed spool entry's age is compared
    against the warn/critical thresholds (the spool byte bounds ride the disk
    component above). Low cardinality: no object names.
    """
    pending = uploader.pending()
    local_bytes = 0
    acked_segments = 0
    acked_bytes = 0
    oldest_unacked: float | None = None
    for entry in pending:
        try:
            info = entry.stat()
        except OSError:
            continue
        local_bytes += info.st_size
        ack_path = uploader._ack_dir / f"{entry.name}{_ACK_SUFFIX}"
        if ack_path.exists():
            acked_segments += 1
            try:
                acked_bytes += int(json.loads(ack_path.read_text()).get("source_size", 0))
            except (OSError, ValueError):
                acked_bytes += info.st_size  # unreadable ACK: count conservatively
            continue
        age = time.time() - info.st_mtime
        if oldest_unacked is None or age > oldest_unacked:
            oldest_unacked = age
    return health_state(
        local_segments=len(pending),
        local_bytes=local_bytes,
        remote_segments=acked_segments,
        remote_bytes=acked_bytes,
        oldest_unacked_seconds=oldest_unacked,
        last_remote_ack_lsn=None,
        upload_errors_total=upload_errors_total,
        archive_errors_total=0,
        quota_rejections_total=0,
        warn_bytes=settings.physical_backup.pitr_spool_warn_bytes,
        hard_bytes=settings.physical_backup.pitr_spool_hard_bytes,
        warn_seconds=settings.physical_backup.pitr_unacked_warn_seconds,
        critical_seconds=settings.physical_backup.pitr_unacked_critical_seconds,
    )


def _unacked_components(uploader: PitrUploader, errors: _LoopErrors) -> list[dict[str, object]]:
    """Unacked-age health component — NON-gating (QA #4696 block 2).

    An unacked age past the critical bound is a domain condition (GCS
    unreachable, operator action needed) that a restart cannot fix; gating
    readiness would make the watchdog kill+restart the daemon every 60s onto
    the same condition — the exact flap this PR exists to prevent. The
    component reports the degraded/critical state in the payload (visible to
    ops) but carries gate_readiness=False, so /healthz stays 200 while the
    loop is alive.
    """
    health = _unacked_health(uploader, upload_errors_total=errors.total)
    if health.level == "critical":
        status = health_schema.DEGRADED
        detail = f"critical — {health.detail}"
    elif health.level == "degraded":
        status = health_schema.DEGRADED
        detail = health.detail
    else:
        status = health_schema.OK
        detail = None
    oldest = (
        "none" if health.oldest_unacked_seconds is None else f"{health.oldest_unacked_seconds:.0f}s"
    )
    return [
        health_schema.component(
            "pitr-uploader",
            status,
            progress=(
                f"unacked={health.unacked_segments} "
                f"oldest_unacked={oldest} "
                f"acked={health.remote_acked_segments} "
                f"upload_errors={errors.total}"
            ),
            detail=detail,
            gate_readiness=False,
        )
    ]


async def _wait_with_heartbeat(
    stop: asyncio.Event,
    delay: float,
    liveness: Liveness | None,
    *,
    heartbeat_interval: float = _HEARTBEAT_S,
) -> None:
    """Wait interruptibly while reporting that intentional backoff is alive."""

    deadline = time.monotonic() + delay
    while not stop.is_set():
        if liveness is not None:
            liveness.beat()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=min(remaining, heartbeat_interval))


async def _upload_off_loop(
    uploader: PitrUploader,
    source: Path,
    *,
    executor: ThreadPoolExecutor,
    liveness: Liveness | None,
) -> None:
    """Run one blocking upload on the daemon's worker pool, heartbeating meanwhile.

    ``upload_one`` is synchronous end-to-end — digest, encrypt, the GCS
    network call (whose SDK retries can hold for the full 120 s RetryError
    budget), the fsync'd ACK. Running it inline froze the event loop for
    the whole call: /healthz stopped answering and `ava start` readiness
    burned its full 180 s while every other service was already up (P0,
    rollout f22f5eb1). On the pool the loop stays free to answer health
    probes; the awaited future re-raises the upload's exceptions unchanged,
    so the loop's transient/critical classification and the ACK chain are
    untouched. The liveness lane is beaten on the heartbeat cadence while
    the upload runs, so a legitimate slow upload never reads as a wedged
    loop.
    """
    loop = asyncio.get_running_loop()
    upload = loop.run_in_executor(executor, uploader.upload_one, source)
    while True:
        if liveness is not None:
            liveness.beat()
        done, _pending = await asyncio.wait({upload}, timeout=_HEARTBEAT_S)
        if upload in done:
            break
    upload.result()  # re-raise any upload exception; the ACK is its success signal


async def upload_loop(
    uploader: PitrUploader,
    *,
    stop: asyncio.Event,
    liveness: Liveness | None = None,
    errors: _LoopErrors | None = None,
    executor: ThreadPoolExecutor,
) -> None:
    """Attempt one object per iteration; SDK and outer retries never nest.

    Each attempt runs on the daemon's worker pool (``executor``): upload_one
    is blocking IO and must never hold the event loop (see
    ``_upload_off_loop`` — it froze /healthz for the whole GCS call).

    Critical failures (auth/config rejection, an immutable-object collision,
    permission-class IO errors) never crash the daemon: a raised exception
    would make the watchdog respawn the process on the same condition, a
    restart flap instead of a backoff (baseline 7 — critical conditions do
    not busy-loop). They are logged and retried on the long critical cadence,
    so a config fix or a manual collision resolution recovers in place.
    """
    failures = 0
    errors = errors if errors is not None else _LoopErrors()
    while not stop.is_set():
        if liveness is not None:
            liveness.beat()
        pending = uploader.pending()
        if not pending:
            failures = 0
            delay = 1.0
        else:
            try:
                await _upload_off_loop(uploader, pending[0], executor=executor, liveness=liveness)
                failures = 0
                delay = 0.0
            except TransientObjectStoreError:
                failures += 1
                errors.transient += 1
                delay = min(60.0, float(2 ** min(failures, 6)))
                _log.warning("PITR upload temporarily failed; retrying after bounded backoff")
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
                    # Permission / read-only-filesystem class: an operator
                    # fix, not a transient — same semantics as the permanent
                    # store errors, so it backs off on the critical cadence
                    # (QA #4696 yellow 1) instead of silently retrying.
                    errors.critical += 1
                    _log.error(
                        "PITR upload permission failure (errno=%s, %s); "
                        "operator action needed, backing off critically",
                        exc.errno,
                        exc,
                    )
                    delay = _CRITICAL_BACKOFF_S
                else:
                    # Local IO failure (disk full ENOSPC, fsync EIO, ...)
                    # writing the staging file or ACK: transient by nature —
                    # the operator can free space — so back off and retry.
                    # Escaping the loop would crash the daemon and make the
                    # watchdog respawn it onto the same full disk, a restart
                    # flap instead of a backoff (QA #4681 block 1; baseline 7).
                    failures += 1
                    errors.transient += 1
                    delay = min(60.0, float(2 ** min(failures, 6)))
                    _log.warning(
                        "PITR upload local IO failure (%s); retrying after bounded backoff", exc
                    )
            except (
                PermanentObjectStoreError,
                AckCorruptionError,
                RemoteCollisionError,
                WalSourceTooLargeError,
            ) as exc:
                errors.critical += 1
                _log.error("PITR upload critical (operator action needed): %s", exc)
                delay = _CRITICAL_BACKOFF_S
        await _wait_with_heartbeat(stop, delay, liveness)


async def run() -> None:
    pidfile = settings.services.pitr_uploader_pidfile
    if not acquire_pidfile(pidfile, "services.pitr.uploader_daemon"):
        return
    uploader = build_uploader()
    stop = asyncio.Event()
    liveness = Liveness(timeout_s=120)
    errors = _LoopErrors()
    # The upload's own worker pool, not asyncio's default executor.
    # Default-executor threads are joined when the loop closes, and the
    # interpreter's atexit joins this pool's workers with no bound at all —
    # one wedged GCS call (the exact 2026-08-30 outage shape) would hold a
    # SIGTERM'd daemon for minutes past the supervisor's 15 s graceful
    # window. Owning the pool makes the threads nameable and lets the
    # shutdown path drop it without waiting (see main()/_hard_exit; the
    # same shape services/agent_ops adopted after its 2026-08-12 wedge).
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pitr-upload")

    def disk_health() -> dict[str, object]:
        footprint = uploader.disk_footprint()
        return {
            "disk_footprint": {
                **asdict(footprint),
                "total_bytes": footprint.total_bytes,
            }
        }

    def components() -> list[dict[str, object]]:
        return _disk_components(
            uploader,
            warn_bytes=settings.physical_backup.pitr_spool_warn_bytes,
            hard_bytes=settings.physical_backup.pitr_spool_hard_bytes,
        ) + _unacked_components(uploader, errors)

    server = await start_health_server(
        "pitr_uploader",
        liveness=liveness,
        components=components,
        extra=disk_health,
    )
    try:
        await upload_loop(uploader, stop=stop, liveness=liveness, errors=errors, executor=executor)
    finally:
        # Sync cleanup first: on the SIGTERM path the pending await below can
        # be cut short by cancellation, and the pidfile must be gone regardless.
        executor.shutdown(wait=False)
        remove_pidfile(pidfile)
        await stop_health_server(server)


def _hard_exit(code: int) -> int:
    """End the process now, skipping interpreter teardown. Never returns.

    Teardown is precisely what hangs: the interpreter's atexit handler joins
    every non-daemon worker in the upload pool with no bound (the ops daemon
    measured the same shape after its 2026-08-12 wedge). One wedged GCS
    upload would therefore hold a daemon that has already finished every
    piece of cleanup it owns — run()'s finally layers run first (pool drop,
    pidfile, health server), and what is skipped after them is bookkeeping
    for an interpreter about to stop existing.

    Logs are flushed first: they are the one thing a skipped teardown would
    lose, and this log is where the next stall has to be legible.
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
    init_gateway_process(name="pitr-uploader")
    install_graceful_shutdown("pitr-uploader")
    code = 0
    # `asyncio.Runner`, not `asyncio.run`: `run` closes in a `finally` that
    # awaits `shutdown_default_executor` (bounded at 300 s, still far past
    # the supervisor's 15 s graceful window), and the interpreter's atexit
    # joins the upload pool's workers with no bound. The runner is therefore
    # never closed: after run()'s own cleanup, _hard_exit skips the teardown
    # that would hang. Same shape as services/agent_ops/daemon.py.
    runner = asyncio.Runner()
    try:
        runner.run(run())
    except KeyboardInterrupt:
        _log.info("[pitr-uploader] interrupted, shutting down")
    except Exception:
        _log.exception("[pitr-uploader] daemon crashed — uncaught exception escaped run()")
        code = 1
    _hard_exit(code)


if __name__ == "__main__":
    main()
