"""Operation-scoped base candidate and restore proof for PITR activation."""

from __future__ import annotations

import asyncio
import threading
from contextlib import suppress

from services.pitr.base_manifest import CandidateManifest
from services.pitr.base_operation_runtime import publish_restore, run_restore
from services.pitr.base_worker import run_candidate
from services.pitr.restore_manifest import ProtectedManifest
from shared.paths import ava_home


def build_activation_candidate(
    *, operation_id: str, chain_id: str, stop: threading.Event
) -> CandidateManifest:
    if not chain_id.endswith(f"-{operation_id}"):
        raise RuntimeError("activation candidate chain differs from operation")
    root = ava_home() / "physical-backup"
    manifest_path = root / "base-manifests" / f"{chain_id}.candidate.json"
    if manifest_path.is_file():
        candidate = CandidateManifest.from_json(manifest_path.read_text())
        if candidate.chain_id != chain_id:
            raise RuntimeError("durable activation candidate differs from intent")
        return candidate
    return asyncio.run(run_candidate(chain_id=chain_id, stop=stop))


async def restore_activation_candidate(
    candidate: CandidateManifest, stop: threading.Event
) -> ProtectedManifest:
    task = asyncio.create_task(run_restore(candidate))
    while not task.done():
        await asyncio.wait({task}, timeout=1)
        if stop.is_set():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise RuntimeError("PITR restore cancelled after deployment lease loss")
    outcome = task.result()

    def require_ownership() -> None:
        if stop.is_set():
            raise RuntimeError("PITR protected publication lost its deployment lease")

    publish_restore(candidate, outcome, require_ownership=require_ownership)
    path = ava_home() / "physical-backup" / "protected-manifests" / f"{candidate.chain_id}.json"
    return ProtectedManifest.from_json(path.read_text())
