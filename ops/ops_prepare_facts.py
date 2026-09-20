"""`cluster_prepare_facts` op handler: relay one unit's prepared-facts shipment.

The daemon-side half of task #4129 channel A (design
`managed-writer-dispatch-design-20260920.md` §3.1): validate the explicit
context, spawn the candidate image's `cli.prepared_facts` entry with the same
references, and return its shipment unchanged. Read-only, never a stop: the
entry owns every authority check (live rollout lease, registration, image
verification) — this handler must not duplicate them, must not fall back to
the source checkout, and must pass nothing beyond the explicit child
projection (the live daemon's environment may carry unrelated state).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from ops.rpc_prepare_facts import PrepareFactsPayload, PrepareFactsResult
from ops.unit_local import candidate_interpreter
from shared.config import settings
from shared.log import logger
from shared.proc import run_bounded
from shared.runtime_release import ReleaseRejectedError

# One synchronous bounded child. The inventory chain's own native reads are
# individually bounded (≤10s native deadlines) but it also verifies two
# retained images and reads the registration; 120s covers a cold chain with a
# wide margin while keeping the op inside the rollout's preparation window
# (the op is a no-effect precondition step; a refusal must surface long before
# the rollout's own stop ladder starts). KEEP (task #3696 exception inventory):
# this kills a runaway child, it is not a tuning knob — the parent RPC's own
# per-attempt budget (`gateway.cluster_rpc_timeout_seconds`) governs the
# success path, so no larger value makes a slow op succeed.
_PREPARE_FACTS_TIMEOUT_S = 120.0

# The relay bound: a receipt + candidate plan shipment measures ~10 KiB; 256
# KiB is a shape guard against a runaway child, not a tightening of the
# entry's own budgets. KEEP (task #3696 exception inventory): a self-imposed
# relay guard fixed by the shipment's shape, not a tunable limit.
_MAX_SHIPMENT_BYTES = 256 * 1024


def _child_environment(home: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home.parent),
        "AVA_HOME": str(home),
        "AVA_DB_URL": settings.data_plane.db_url,
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _child_argv(interpreter: Path, payload: PrepareFactsPayload) -> list[str]:
    operation = payload.operation
    return [
        str(interpreter),
        "-I",
        "-B",
        "-X",
        "utf8",
        "-m",
        "cli.prepared_facts",
        "--operation-holder",
        operation.holder,
        "--operation-acquired-at",
        operation.acquired_at.isoformat(),
        "--operation-target-sha",
        operation.target_sha,
        "--artifact-digest",
        payload.candidate.artifact_digest,
        "--manifest-digest",
        payload.candidate.manifest_digest,
        "--schema-digest",
        payload.candidate.schema_digest,
        "--recovery-artifact-digest",
        payload.recovery.artifact_digest,
        "--recovery-manifest-digest",
        payload.recovery.manifest_digest,
        "--recovery-schema-digest",
        payload.recovery.schema_digest,
    ]


def cluster_prepare_facts_op(payload: PrepareFactsPayload) -> PrepareFactsResult:
    home = settings.general.ava_home
    interpreter = candidate_interpreter(home, payload.candidate.artifact_digest)
    started = time.monotonic()
    logger.info(
        "[cluster_prepare_facts] start pid={pid} image={image}",
        pid=os.getpid(),
        image=payload.candidate.artifact_digest,
    )
    try:
        result = run_bounded(
            _child_argv(interpreter, payload),
            timeout=_PREPARE_FACTS_TIMEOUT_S,
            capture_output=True,
            text=True,
            env=_child_environment(home),
            cwd=str(home),
        )
    except subprocess.TimeoutExpired as exc:
        raise ReleaseRejectedError("candidate prepared-facts entry timed out") from exc
    if result.returncode != 0:
        tail = (result.stderr or "")[-2000:].strip() or "(no stderr)"
        raise ReleaseRejectedError(f"candidate prepared-facts entry refused: {tail}")
    body = result.stdout or ""
    if len(body) > _MAX_SHIPMENT_BYTES:
        raise ReleaseRejectedError("prepared-facts shipment exceeds its relay bound")
    try:
        json.loads(body)
    except ValueError as exc:
        raise ReleaseRejectedError("prepared-facts shipment is not one JSON document") from exc
    try:
        # JSON mode: the shipment is JSON text, and its strict evidence models
        # (tuples, aware datetimes) validate from JSON-native values only.
        parsed = PrepareFactsResult.model_validate_json(body)
    except ValueError as exc:
        raise ReleaseRejectedError("prepared-facts shipment shape is invalid") from exc
    logger.info(
        "[cluster_prepare_facts] ok home={home} elapsed={elapsed:.1f}s",
        home=home,
        elapsed=time.monotonic() - started,
    )
    return parsed
