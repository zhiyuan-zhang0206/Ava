"""Verified logical recovery snapshots owned by the backup pipeline.

The caller chooses when a snapshot is required and supplies its progress sink.
Creation retains the reentrant backup lock through encrypted-artifact restore
verification. PITR activation snapshots retain their operation-specific naming,
remote publication and retention behavior in services.backup.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path

import shared.pg_tools
from shared.platform import LockTimeoutError
from shared.proc import run_bounded

DUMP_TIMEOUT_S = 20 * 60
LOCK_HEARTBEAT_S = 60.0
_RESTORE_TIMEOUT_S = 60.0
_PG_RESTORE_TOC_ENTRY_RE = re.compile(r"^\d+;")


def verify_snapshot(artifact: Path) -> None:
    """Verify an encrypted snapshot while keeping all error paths redacted."""
    try:
        _verify_snapshot_artifact_inner(artifact)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"logical data snapshot {artifact} is not restorable: verification failed "
            f"({type(exc).__name__})"
        ) from None


def _require_nonempty(path: Path, artifact: Path, label: str) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        raise RuntimeError(
            f"logical data snapshot {artifact} is not restorable: cannot read {label}"
        ) from None
    if size <= 0:
        raise RuntimeError(f"logical data snapshot {artifact} is not restorable: {label} is empty")


def _verify_snapshot_artifact_inner(artifact: Path) -> None:
    """Decrypt and list a snapshot without exposing its connection URL."""
    _require_nonempty(artifact, artifact, "artifact")

    from services.backup import decrypt_artifact, gunzip_if_needed

    with tempfile.TemporaryDirectory(prefix="ava-snapshot-") as temporary_dir:
        temporary = Path(temporary_dir)
        dump = temporary / "snapshot.dump"
        try:
            decrypt_artifact(artifact, dump)
        except Exception as exc:
            raise RuntimeError(
                f"logical data snapshot {artifact} is not restorable: decrypt failed "
                f"({type(exc).__name__})"
            ) from None
        _require_nonempty(dump, artifact, "decrypted dump")
        try:
            # Fresh snapshots are raw custom dumps; a legacy artifact (written
            # before the 2026-08-27 double-gzip removal) still needs its gzip
            # layer removed before pg_restore can read it.
            gunzip_if_needed(dump, timeout_s=_RESTORE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"logical data snapshot {artifact} is not restorable: gunzip timed out after "
                f"{_RESTORE_TIMEOUT_S:.0f}s"
            ) from None
        except Exception as exc:
            raise RuntimeError(
                f"logical data snapshot {artifact} is not restorable: gunzip failed "
                f"({type(exc).__name__})"
            ) from None
        _require_nonempty(dump, artifact, "decompressed dump")
        try:
            listing = run_bounded(
                [str(shared.pg_tools.pg_tool("pg_restore")), "--list", str(dump)],
                timeout=_RESTORE_TIMEOUT_S,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"logical data snapshot {artifact} is not restorable: pg_restore --list "
                f"timed out after {_RESTORE_TIMEOUT_S:.0f}s"
            ) from None
        except Exception as exc:
            raise RuntimeError(
                f"logical data snapshot {artifact} is not restorable: pg_restore --list "
                f"failed ({type(exc).__name__})"
            ) from None
        if listing.returncode != 0:
            raise RuntimeError(
                f"logical data snapshot {artifact} is not restorable: pg_restore --list "
                f"exited {listing.returncode}"
            )
        if not any(_PG_RESTORE_TOC_ENTRY_RE.match(line) for line in listing.stdout.splitlines()):
            raise RuntimeError(
                f"logical data snapshot {artifact} is not restorable: pg_restore --list "
                "returned an empty table of contents"
            )


@contextmanager
def _narrated_backup_lock(
    *, timeout_s: float, heartbeat_s: float, progress: Callable[[str], None]
) -> Generator[None]:
    """Take the backup lock within `timeout_s`, narrating contested waits.

    A scheduled writer holding the lock would otherwise leave the snapshot
    silent for up to the whole budget — the 2026-09-14 silent-dump shape. The
    acquire leg is retried in `heartbeat_s` chunks, reporting each failed chunk
    through `progress`, so an operator can distinguish a contended lock from a
    stalled process. Expiry still raises `LockTimeoutError`, its message
    naming the total budget rather than the last chunk value.
    """
    from services.backup import backup_lock

    started = time.monotonic()
    deadline = started + timeout_s
    with ExitStack() as stack:
        while True:
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                raise LockTimeoutError(
                    f"could not take the backup lock within {timeout_s:.0f}s — another "
                    "process has held it for the whole wait; it is released when "
                    "that process exits"
                )
            try:
                stack.enter_context(backup_lock(timeout_s=min(heartbeat_s, remaining_s)))
                break
            except LockTimeoutError:
                progress(
                    f"waiting for the backup lock ({time.monotonic() - started:.0f}s "
                    "elapsed; another backup is writing)"
                )
        yield


def create_pre_update_snapshot(*, progress: Callable[[str], None]) -> Path:
    """Create and verify a local logical dump before a migration-bearing update."""
    from services.backup import run_backup

    progress(f"started (dump bounded at {DUMP_TIMEOUT_S / 60:.0f} min)")

    # Hold the same lock as the daily writer through the restore listing. A
    # verified dump must remain untouched until this function hands its path to
    # recovery; otherwise a scheduled writer can sweep its partial or replace
    # the same-second target between creation and verification. A contended
    # take is retried in narrated chunks — not one silent 20-minute wait.
    with _narrated_backup_lock(
        timeout_s=DUMP_TIMEOUT_S,
        heartbeat_s=LOCK_HEARTBEAT_S,
        progress=progress,
    ):
        try:
            dump_path = run_backup(
                timeout_s=DUMP_TIMEOUT_S,
                pre_update=True,
                publish=False,
                progress=progress,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                "could not create pre-update data snapshot: pg_dump timed out after "
                f"{DUMP_TIMEOUT_S:.0f}s"
            ) from None
        except Exception as exc:
            # pg_dump's argv includes the direct DB URL. Do not stringify or
            # chain its failure into a rollout log.
            raise RuntimeError(
                f"could not create pre-update data snapshot: pg_dump failed ({type(exc).__name__})"
            ) from None

        verify_snapshot(dump_path)
        # The verified path goes to the rollout output: without it, a snapshot
        # is invisible in the log (run_backup writes to its own daemon stream)
        # and an operator can mistake it for an unscheduled backup.
        progress(f"{dump_path} (verified)")
        return dump_path


def create_pre_activation_snapshot(
    *, operation_id: str, db_url: str, progress: Callable[[str], None]
) -> Path:
    """Create the mandatory logical recovery floor before first PITR activation.

    This snapshot is unconditional: WAL archiving is still off. The caller
    receives progress throughout the lock wait, dump and final verification.
    """
    from services.backup import run_backup

    progress(f"started (dump bounded at {DUMP_TIMEOUT_S / 60:.0f} min)")
    with _narrated_backup_lock(
        timeout_s=DUMP_TIMEOUT_S,
        heartbeat_s=LOCK_HEARTBEAT_S,
        progress=progress,
    ):
        try:
            dump_path = run_backup(
                timeout_s=DUMP_TIMEOUT_S,
                db_url=db_url,
                pitr_activation=operation_id,
                progress=progress,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                "could not create pre-activation data snapshot: pg_dump timed out after "
                f"{DUMP_TIMEOUT_S:.0f}s"
            ) from None
        except Exception as exc:
            raise RuntimeError(
                "could not create pre-activation data snapshot: "
                f"pg_dump failed ({type(exc).__name__})"
            ) from None
        verify_snapshot(dump_path)
        progress(f"{dump_path} (verified)")
        return dump_path
