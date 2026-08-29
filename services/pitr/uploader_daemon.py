"""Single-worker PITR GCS uploader daemon."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import asdict

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
from shared.health_schema import DEGRADED, OK, component
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
_BACKOFF_HEARTBEAT_S = 30.0
_ACK_SUFFIX = ".ack.json"


def _disk_components(
    uploader: PitrUploader, *, warn_bytes: int, hard_bytes: int
) -> list[dict[str, object]]:
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

    QA #4681 block 3: AVA_PITR_UNACKED_* are live health inputs, not dead
    configuration — the oldest un-ACKed spool entry's age is compared against
    the warn/critical thresholds (the spool byte bounds ride the disk
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


def _unacked_components(uploader: PitrUploader) -> list[dict[str, object]]:
    """/healthz projection: unacked-age health through state.py's model.

    A critical domain state (unacked age past the critical bound) reads as
    degraded on the wire — never DOWN — so the watchdog does not restart-flap
    on a condition that a restart cannot fix (baseline 7).
    """
    health = _unacked_health(uploader, upload_errors_total=0)
    if health.level == "critical":
        status = DEGRADED
        detail = f"critical — {health.detail}"
    elif health.level == "degraded":
        status = DEGRADED
        detail = health.detail
    else:
        status = OK
        detail = None
    oldest = (
        "none" if health.oldest_unacked_seconds is None else f"{health.oldest_unacked_seconds:.0f}s"
    )
    return [
        component(
            "pitr-uploader",
            status,
            progress=(
                f"unacked={health.unacked_segments} "
                f"oldest_unacked={oldest} "
                f"acked={health.remote_acked_segments}"
            ),
            detail=detail,
        )
    ]


async def _wait_with_heartbeat(
    stop: asyncio.Event,
    delay: float,
    liveness: Liveness | None,
    *,
    heartbeat_interval: float = _BACKOFF_HEARTBEAT_S,
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


async def upload_loop(
    uploader: PitrUploader, *, stop: asyncio.Event, liveness: Liveness | None = None
) -> None:
    """Attempt one object per iteration; SDK and outer retries never nest.

    Critical failures (auth/config rejection, an immutable-object collision)
    never crash the daemon: a raised exception would make the watchdog respawn
    the process on the same collision, a restart flap instead of a backoff
    (baseline 7 — critical conditions do not busy-loop). They are logged and
    retried on the long critical cadence, so a config fix or a manual
    collision resolution recovers in place.
    """
    failures = 0
    while not stop.is_set():
        if liveness is not None:
            liveness.beat()
        pending = uploader.pending()
        if not pending:
            failures = 0
            delay = 1.0
        else:
            try:
                uploader.upload_one(pending[0])
                failures = 0
                delay = 0.0
            except TransientObjectStoreError:
                failures += 1
                delay = min(60.0, float(2 ** min(failures, 6)))
                _log.warning("PITR upload temporarily failed; retrying after bounded backoff")
            except OSError as exc:
                # Local IO failure (disk full ENOSPC, fsync EIO, ...) writing
                # the staging file or ACK: transient by nature — the operator
                # can free space — so back off and retry. Escaping the loop
                # would crash the daemon and make the watchdog respawn it onto
                # the same full disk, a restart flap instead of a backoff
                # (QA #4681 block 1; baseline 7 — no busy-loop).
                failures += 1
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
        ) + _unacked_components(uploader)

    server = await start_health_server(
        "pitr_uploader",
        liveness=liveness,
        components=components,
        extra=disk_health,
    )
    try:
        await upload_loop(uploader, stop=stop, liveness=liveness)
    finally:
        await stop_health_server(server)
        remove_pidfile(pidfile)


def main() -> None:
    init_gateway_process(name="pitr-uploader")
    install_graceful_shutdown("pitr-uploader")
    asyncio.run(run())


if __name__ == "__main__":
    main()
