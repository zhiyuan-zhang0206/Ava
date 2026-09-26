"""Fixed logical-backup operations in one launch-owned trusted process group."""

from __future__ import annotations

import asyncio
import hashlib
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal

from services.pitr.worker_process import publish_result, run_operation, worker_request
from shared.paths import ava_home
from shared.process_env import inherited_process_env

Job = Literal["dump", "restore"]


def _sha256(path: Path) -> str:
    with path.open("rb") as artifact:
        return hashlib.file_digest(artifact, "sha256").hexdigest()


async def run_job(kind: Job, *, now: datetime | None = None) -> None:
    """Accept a worker result only after its entire inherited group closed."""
    from services.backup import commit_scheduled_backup

    completed = await run_operation(
        "services.backup_scheduler.worker",
        {"kind": kind, "now": now.isoformat() if now is not None else None},
        control_root=ava_home() / "backups" / "operations",
        env=inherited_process_env(),
    )
    result = completed.result
    if kind == "dump":
        if set(result) != {"artifact", "sha256"}:
            raise RuntimeError(f"logical backup returned an invalid result: {completed.work}")
        name, digest = result["artifact"], result["sha256"]
        if not isinstance(name, str) or Path(name).name != name or not isinstance(digest, str):
            raise RuntimeError(f"logical backup result escaped its controls: {completed.work}")
        # Hashing and linking a multi-GiB artifact is bounded local I/O; the
        # scheduler's health server shares this loop.
        await asyncio.to_thread(commit_scheduled_backup, completed.work / "artifact" / name, digest)
    elif result != {"restored": True}:
        raise RuntimeError(f"logical restore returned an invalid result: {completed.work}")
    completed.retire()


def _execute(request: dict[str, object], work: Path) -> dict[str, object]:
    if set(request) != {"kind", "now"}:
        raise ValueError("invalid logical backup request")
    if request["kind"] == "dump":
        from services.backup import prepare_scheduled_backup

        stamp = request["now"]
        if not isinstance(stamp, str):
            raise TypeError("logical dump requires its captured timestamp")
        artifact = prepare_scheduled_backup(datetime.fromisoformat(stamp), work / "artifact")
        return {"artifact": artifact.name, "sha256": _sha256(artifact)}
    if request == {"kind": "restore", "now": None}:
        from scripts.restore_drill import run_drill

        run_drill(foreground=True)
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
