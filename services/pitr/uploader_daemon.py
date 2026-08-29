"""Single-worker PITR GCS uploader daemon."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from services._pidfile import acquire_pidfile, remove_pidfile
from services.pitr.gcs_store import GCSObjectStore
from services.pitr.object_store import PermanentObjectStoreError, TransientObjectStoreError
from services.pitr.uploader import PitrUploader, RemoteCollisionError
from shared.config import settings
from shared.daemon_health import Liveness, start_health_server, stop_health_server
from shared.daemon_shutdown import install_graceful_shutdown
from shared.log import init_gateway_process
from shared.paths import ava_home

_log = logging.getLogger("services.pitr.uploader_daemon")


def build_uploader() -> PitrUploader:
    config = settings.physical_backup
    if not config.pitr_enabled:
        raise RuntimeError("PITR uploader cannot start while AVA_PITR_ENABLED is off")
    key_path = config.pitr_backup_key_file
    credentials = config.pitr_gcs_credentials_file
    if key_path is None or credentials is None:
        raise RuntimeError("validated PITR secrets are missing")
    root = ava_home() / "physical-backup"
    return PitrUploader(
        spool=root / "spool",
        ack_dir=root / "ack",
        staging=root / "staging",
        prefix=config.pitr_gcs_prefix,
        key=key_path.read_bytes(),
        key_id=config.pitr_backup_key_id,
        store=GCSObjectStore(
            project=config.pitr_gcs_project,
            bucket=config.pitr_gcs_bucket,
            credentials_file=credentials,
        ),
    )


async def upload_loop(
    uploader: PitrUploader, *, stop: asyncio.Event, liveness: Liveness | None = None
) -> None:
    """Attempt one object per iteration; SDK and outer retries never nest."""
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
            except (PermanentObjectStoreError, RemoteCollisionError):
                _log.exception("PITR upload entered a critical operator-action state")
                raise
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)


async def run() -> None:
    pidfile = settings.services.pitr_uploader_pidfile
    if not acquire_pidfile(pidfile, "services.pitr.uploader_daemon"):
        return
    uploader = build_uploader()
    stop = asyncio.Event()
    liveness = Liveness(timeout_s=120)
    server = await start_health_server("pitr_uploader", liveness=liveness)
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
