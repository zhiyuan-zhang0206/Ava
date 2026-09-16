"""Verify pre-update / pre-activation snapshot artifacts.

`_verify_snapshot_artifact` decrypts a managed dump and proves it carries a
non-empty `pg_restore` table of contents before anything is stopped — a dump
that cannot be listed is not recovery evidence. The snapshot entry points in
`_update_git` run it while the backup lock is still held; `_pitr_activation`
re-verifies an existing artifact.

Split out of `cli.commands._update_git` (task #3442) to keep that module inside
the per-file line budget: the verification bound and the TOC rule below belong
to this stage alone.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

import shared.pg_tools
from shared.proc import run_bounded

_PRE_UPDATE_RESTORE_TIMEOUT_S = 60.0
_PG_RESTORE_TOC_ENTRY_RE = re.compile(r"^\d+;")


def _verify_snapshot_artifact(artifact: Path) -> None:
    """Verify an encrypted snapshot while keeping all error paths redacted."""
    try:
        _verify_snapshot_artifact_inner(artifact)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"pre-update data snapshot {artifact} is not restorable: verification failed "
            f"({type(exc).__name__})"
        ) from None


def _verify_snapshot_artifact_inner(artifact: Path) -> None:
    """Decrypt and list a snapshot without exposing its connection URL."""
    try:
        artifact_size = artifact.stat().st_size
    except OSError:
        raise RuntimeError(
            f"pre-update data snapshot {artifact} is not restorable: cannot read artifact"
        ) from None
    if artifact_size <= 0:
        raise RuntimeError(
            f"pre-update data snapshot {artifact} is not restorable: artifact is empty"
        )

    from services.backup import decrypt_artifact, gunzip_if_needed

    with tempfile.TemporaryDirectory(prefix="ava-pre-update-") as temporary_dir:
        temporary = Path(temporary_dir)
        dump = temporary / "snapshot.dump"
        try:
            decrypt_artifact(artifact, dump)
        except Exception as exc:
            raise RuntimeError(
                f"pre-update data snapshot {artifact} is not restorable: decrypt failed "
                f"({type(exc).__name__})"
            ) from None
        try:
            dump_size = dump.stat().st_size
        except OSError:
            raise RuntimeError(
                f"pre-update data snapshot {artifact} is not restorable: cannot read decrypted dump"
            ) from None
        if dump_size <= 0:
            raise RuntimeError(
                f"pre-update data snapshot {artifact} is not restorable: decrypted dump is empty"
            )
        try:
            # Fresh snapshots are raw custom dumps; a legacy artifact (written
            # before the 2026-08-27 double-gzip removal) still needs its gzip
            # layer removed before pg_restore can read it.
            gunzip_if_needed(dump, timeout_s=_PRE_UPDATE_RESTORE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"pre-update data snapshot {artifact} is not restorable: gunzip timed out after "
                f"{_PRE_UPDATE_RESTORE_TIMEOUT_S:.0f}s"
            ) from None
        except Exception as exc:
            raise RuntimeError(
                f"pre-update data snapshot {artifact} is not restorable: gunzip failed "
                f"({type(exc).__name__})"
            ) from None
        try:
            dump_size = dump.stat().st_size
        except OSError:
            raise RuntimeError(
                f"pre-update data snapshot {artifact} is not restorable: cannot read decompressed dump"
            ) from None
        if dump_size <= 0:
            raise RuntimeError(
                f"pre-update data snapshot {artifact} is not restorable: decompressed dump is empty"
            )
        try:
            listing = run_bounded(
                [str(shared.pg_tools.pg_tool("pg_restore")), "--list", str(dump)],
                timeout=_PRE_UPDATE_RESTORE_TIMEOUT_S,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"pre-update data snapshot {artifact} is not restorable: pg_restore --list "
                f"timed out after {_PRE_UPDATE_RESTORE_TIMEOUT_S:.0f}s"
            ) from None
        except Exception as exc:
            raise RuntimeError(
                f"pre-update data snapshot {artifact} is not restorable: pg_restore --list "
                f"failed ({type(exc).__name__})"
            ) from None
        if listing.returncode != 0:
            raise RuntimeError(
                f"pre-update data snapshot {artifact} is not restorable: pg_restore --list "
                f"exited {listing.returncode}"
            )
        if not any(_PG_RESTORE_TOC_ENTRY_RE.match(line) for line in listing.stdout.splitlines()):
            raise RuntimeError(
                f"pre-update data snapshot {artifact} is not restorable: pg_restore --list "
                "returned an empty table of contents"
            )
