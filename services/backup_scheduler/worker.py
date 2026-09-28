"""Fixed logical-backup operations in one launch-owned trusted process group.

Daily dumps and the weekly logical restore drill are separate operation kinds
with separate control roots, so a failed drill never stops the dumps. Each
quarantines into its own `$AVA_HOME/backups/quarantine/<kind>/`, which keeps
only complete encrypted artifacts: plaintext dump output never survives a
closed worker.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal

from services.pitr.logical_dump_names import DUMP_NAME_RE
from services.pitr.operation_custody import OperationKind, OperationWorker, publish_result
from services.pitr.worker_process import CompletedOperation, run_operation, worker_request
from shared.paths import ava_home
from shared.process_env import inherited_process_env

Job = Literal["dump", "restore"]


def _sanitize_dump(work: Path, _worker: OperationWorker | None) -> None:
    """Keep only complete encrypted artifacts; drop plaintext and partial pipeline files."""
    staged = work / "artifact"
    if not staged.is_dir():
        return
    for item in staged.iterdir():
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        elif not _complete_artifact(item):
            item.unlink()


def _complete_artifact(path: Path) -> bool:
    """A renamed `.dump.enc` exists only after encryption completed."""
    return (
        path.is_file()
        and not path.is_symlink()
        and path.name.endswith(".dump.enc")
        and DUMP_NAME_RE.fullmatch(path.name) is not None
    )


def _sanitize_restore_drill(work: Path, _worker: OperationWorker | None) -> None:
    """Remove the drill's decrypted dump and its restored throwaway cluster.

    The decrypted dump lives only in the private scratch. A worker killed
    before its own teardown leaves the restored database in its throwaway
    cluster; after proven closure its owner lock is released, so the
    throwaway sweep reaps it now instead of at the next throwaway start.
    """
    from shared.pg_tools import sweep_orphaned_throwaway_clusters

    scratch = work / "scratch"
    if scratch.is_dir() and not scratch.is_symlink():
        shutil.rmtree(scratch)
    sweep_orphaned_throwaway_clusters()


def dump_kind() -> OperationKind:
    root = ava_home() / "backups"
    return OperationKind(
        "logical-dump",
        root / "operations" / "dump",
        root / "quarantine" / "logical-dump",
        _sanitize_dump,
    )


def restore_drill_kind() -> OperationKind:
    root = ava_home() / "backups"
    return OperationKind(
        "logical-restore-drill",
        root / "operations" / "restore-drill",
        root / "quarantine" / "logical-restore-drill",
        _sanitize_restore_drill,
    )


def _sha256(path: Path) -> str:
    with path.open("rb") as artifact:
        return hashlib.file_digest(artifact, "sha256").hexdigest()


async def run_job(kind: Job, *, now: datetime | None = None) -> None:
    """Accept a worker result only after its entire inherited group closed."""
    completed = await run_operation(
        "services.backup_scheduler.worker",
        {"kind": kind, "now": now.isoformat() if now is not None else None},
        kind=dump_kind() if kind == "dump" else restore_drill_kind(),
        env=inherited_process_env(),
    )
    if kind == "dump":
        # Hashing and linking a multi-GiB artifact is bounded local I/O; the
        # scheduler's health server shares this loop.
        await completed.commit(lambda: _commit_dump(completed))
    else:
        await completed.commit(lambda: _accept_restore(completed))


def _commit_dump(completed: CompletedOperation) -> Path:
    result = completed.result
    if set(result) != {"artifact", "sha256"}:
        raise RuntimeError("logical backup returned an invalid result")
    name, digest = result["artifact"], result["sha256"]
    if not isinstance(name, str) or Path(name).name != name or not isinstance(digest, str):
        raise RuntimeError("logical backup result escaped its controls")
    return commit_scheduled_backup(completed.work / "artifact" / name, digest)


def _accept_restore(completed: CompletedOperation) -> None:
    if completed.result != {"restored": True}:
        raise RuntimeError("logical restore returned an invalid result")


def commit_scheduled_backup(staged: Path, digest: str) -> Path:
    """Publish only the exact completed worker artifact; never overwrite one.

    The caller invokes this only after the worker's group closed with a zero
    exit. A backup directory on another filesystem gets a verified private copy
    published the same exclusive way. Any refusal leaves the staged artifact in
    the operation's controls for quarantine.
    """
    from services.backup import backup_dir, prune_after_publish
    from shared.private_storage import ensure_private_dir, ensure_private_file

    if staged.is_symlink() or not staged.is_file() or not DUMP_NAME_RE.fullmatch(staged.name):
        raise RuntimeError("scheduled backup result is not a managed regular artifact")
    if _sha256(staged) != digest:
        raise RuntimeError("scheduled backup changed after preparation")
    directory = ensure_private_dir(backup_dir())
    target = directory / staged.name
    try:
        os.link(staged, target)  # Exclusive publication; a prior artifact is never replaced.
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _publish_copy(staged, target, digest)
    ensure_private_file(target)
    staged.unlink()
    prune_after_publish(target)
    return target


def _publish_copy(staged: Path, target: Path, digest: str) -> None:
    """Cross-filesystem publication with the same exclusive, verified result.

    The copy stays open until it is linked, so a backup run sweeping this
    directory (it removes only intermediates no process holds open) never
    takes it mid-publication; a controller killed meanwhile leaves a closed
    copy that the next backup run sweeps.
    """
    fd, raw = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".copy", dir=target.parent)
    copy = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with staged.open("rb") as source, os.fdopen(fd, "w+b") as output:
            shutil.copyfileobj(source, output, 8 * 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
            output.seek(0)
            if hashlib.file_digest(output, "sha256").hexdigest() != digest:
                raise RuntimeError(
                    "scheduled backup copy differs from the closed worker's artifact"
                )
            os.link(copy, target)
        dirfd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        copy.unlink(missing_ok=True)


def _execute(request: dict[str, object], work: Path) -> dict[str, object]:
    if set(request) != {"kind", "now"}:
        raise ValueError("invalid logical backup request")
    if request["kind"] == "dump":
        from services.backup import run_backup

        stamp = request["now"]
        if not isinstance(stamp, str):
            raise TypeError("logical dump requires its captured timestamp")
        artifact = run_backup(datetime.fromisoformat(stamp), staging=work / "artifact")
        return {"artifact": artifact.name, "sha256": _sha256(artifact)}
    if request == {"kind": "restore", "now": None}:
        from scripts.restore_drill import run_drill

        scratch = work / "scratch"
        scratch.mkdir(mode=0o700)
        run_drill(foreground=True, scratch_root=scratch)
        return {"restored": True}
    raise ValueError("unknown logical backup operation")


def main() -> None:
    request, output = worker_request(sys.argv)
    from shared.log import init_gateway_process

    # The store-verified publish ACK is an INFO record: route it to the log sinks.
    init_gateway_process(name="pg-backup-worker")
    publish_result(output, _execute(request, output.parent))


if __name__ == "__main__":
    main()
