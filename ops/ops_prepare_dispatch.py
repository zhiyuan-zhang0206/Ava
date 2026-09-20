"""`cluster_prepare_dispatch` op handler: seal one unit's plan copy and validate it locally.

The daemon-side half of task #4129 channel B (design
`managed-writer-dispatch-design-20260920.md` section 3.2): write the dispatched
sealed plan -- and the unit's staged hop projections (channel C) -- into the
unit's private `run/` directory under their content names, spawn the retained
candidate image's `cli.prepared_plan` entry against the plan, and answer with
the unit's acknowledgement. The payload's `artifact_digest` only locates the
interpreter that runs the validation; the entry re-derives every binding from
the plan bytes itself, and the hop projections are staged exactly as named --
the consuming hop entry re-reads them, so nothing here is authority.

Read-only, never a stop: a validation refusal travels back as the
acknowledgement's `refusal` field -- the coordinator's gate prints the whole
roster's verdict at once, so a unit that validated and refused is an answer,
not an op exception. Faults of the dispatch itself (a payload that is not one
ASCII JSON document, a missing retained image, a timed-out or unintelligible
child) stay op failures. The daemon must not duplicate the entry's checks,
must not fall back to the source checkout, and must pass nothing beyond the
explicit child projection (the live daemon's environment may carry unrelated
state).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

from ops.rpc_prepare_dispatch import (
    PREPARED_HOP_PREFIX,
    DispatchValidation,
    PrepareDispatchPayload,
    PrepareDispatchResult,
    prepared_plan_name,
)
from ops.unit_local import candidate_interpreter, machine_identity
from shared.config import settings
from shared.log import logger
from shared.private_storage import write_private_bytes
from shared.proc import run_bounded
from shared.runtime_release import ReleaseRejectedError

# One synchronous bounded child. The entry verifies two retained images and the
# local receipt chain; 120s covers a cold chain with a wide margin (the sibling
# `cluster_prepare_facts` bound; the parent RPC's own per-attempt budget governs
# the success path, so no larger value makes a slow op succeed).
# KEEP (task #3696 exception inventory): this kills a runaway child, it is not a
# tuning knob.
_VALIDATE_TIMEOUT_S = 120.0

# The relay bound on the dispatched plan: the sealed plan measures a few KiB;
# 256 KiB is a shape guard against a runaway sender, not a tightening of the
# entry's own budgets. KEEP (task #3696 exception inventory): a self-imposed
# relay guard fixed by the plan's shape, not a tunable limit.
_MAX_PLAN_BYTES = 256 * 1024

# The acknowledgement is two 64-hex digests plus a JSON envelope (~200 bytes);
# 4 KiB is the same kind of runaway-shape guard on the child's output. KEEP
# (task #3696 exception inventory): fixed by the acknowledgement's shape.
_MAX_ACK_BYTES = 4 * 1024

# A hop projection measures a few KiB (a prepared observation context or the
# request referencing it); 64 KiB is the same shape bound the consuming entries
# enforce (`read_prepared_context`, `_update_bootstrap._private_reference`), so
# an oversized projection refuses here instead of surfacing as a child fault.
# KEEP (task #3696 exception inventory): a relay-shape guard fixed by the
# projections' shape, not a tuning knob.
_MAX_PROJECTION_BYTES = 64 * 1024

# The projection kinds are lowercase kebab tokens (`candidate-context`,
# `request`); a shape guard, not a vocabulary -- the name's digest binds the
# bytes either way.
_HOP_PROJECTION_KIND = re.compile(r"^[a-z][a-z0-9-]*$")


def _child_environment(home: Path) -> dict[str, str]:
    # No database projection: the entry's validation is local and read-only.
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home.parent),
        "AVA_HOME": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _child_argv(interpreter: Path, plan_path: Path) -> list[str]:
    return [
        str(interpreter),
        "-I",
        "-B",
        "-X",
        "utf8",
        "-m",
        "cli.prepared_plan",
        "--prepared",
        str(plan_path),
    ]


def _hop_projection_path(name: str, content: str, home: Path) -> Path:
    """Verify one dispatched projection is exactly what its name claims.

    The name must be `prepared-hop-<kind>-<sha256(content)>.json`: content
    addressed, so a mismatched pair -- bytes the consuming hop entry could
    never have been waiting for -- refuses before anything lands. ASCII keeps
    the staged bytes inside every consuming entry's own read face.
    """
    if not name.startswith(PREPARED_HOP_PREFIX) or not name.endswith(".json"):
        raise ReleaseRejectedError("dispatched projection lacks its prepared-hop name")
    body = name[len(PREPARED_HOP_PREFIX) : -len(".json")]
    if len(body) <= 65 or body[-65] != "-":
        raise ReleaseRejectedError("dispatched projection lacks its prepared-hop name")
    kind, digest = body[:-65], body[-64:]
    if not _HOP_PROJECTION_KIND.fullmatch(kind) or not content.isascii():
        raise ReleaseRejectedError("dispatched projection is not a canonical hop projection")
    raw = content.encode("ascii")
    if len(raw) > _MAX_PROJECTION_BYTES:
        raise ReleaseRejectedError("dispatched projection exceeds its relay bound")
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ReleaseRejectedError("dispatched projection name does not match its bytes")
    try:
        json.loads(content)
    except ValueError as exc:
        raise ReleaseRejectedError("dispatched projection is not one JSON document") from exc
    return home / "run" / name


def cluster_prepare_dispatch_op(payload: PrepareDispatchPayload) -> PrepareDispatchResult:
    home = settings.general.ava_home
    machine = machine_identity(home)
    if not payload.plan_json.isascii():
        raise ReleaseRejectedError("dispatched plan is not ASCII JSON text")
    raw = payload.plan_json.encode("ascii")
    if len(raw) > _MAX_PLAN_BYTES:
        raise ReleaseRejectedError("dispatched plan exceeds its relay bound")
    try:
        json.loads(payload.plan_json)
    except ValueError as exc:
        raise ReleaseRejectedError("dispatched plan is not one JSON document") from exc
    plan_digest = hashlib.sha256(raw).hexdigest()
    plan_path = home / "run" / prepared_plan_name(plan_digest)
    # Validate every projection before the first write lands: a malformed
    # payload must leave no partial staging behind. Plan bytes first, then the
    # content-named projections the hop entry will read.
    staged = [
        (_hop_projection_path(projection.name, projection.content, home), projection.content)
        for projection in payload.projections
    ]
    write_private_bytes(plan_path, raw)
    for projection_path, projection_text in staged:
        write_private_bytes(projection_path, projection_text.encode("ascii"))
    interpreter = candidate_interpreter(home, payload.artifact_digest)
    started = time.monotonic()
    logger.info(
        "[cluster_prepare_dispatch] start pid={pid} image={image}",
        pid=os.getpid(),
        image=payload.artifact_digest,
    )
    try:
        result = run_bounded(
            _child_argv(interpreter, plan_path),
            timeout=_VALIDATE_TIMEOUT_S,
            capture_output=True,
            text=True,
            env=_child_environment(home),
            cwd=str(home),
        )
    except subprocess.TimeoutExpired as exc:
        raise ReleaseRejectedError("candidate prepared-plan entry timed out") from exc
    if result.returncode != 0:
        tail = (result.stderr or "")[-2000:].strip() or "(no stderr)"
        return PrepareDispatchResult(
            machine=machine,
            home=str(home),
            plan_digest=plan_digest,
            refusal=f"candidate prepared-plan entry refused: {tail}",
        )
    body = result.stdout or ""
    if len(body) > _MAX_ACK_BYTES:
        raise ReleaseRejectedError("prepared-plan acknowledgement exceeds its relay bound")
    try:
        validation = DispatchValidation.model_validate_json(body)
    except ValueError as exc:
        raise ReleaseRejectedError("prepared-plan acknowledgement shape is invalid") from exc
    if validation.plan_digest != plan_digest:
        raise ReleaseRejectedError("candidate entry validated a different plan digest")
    logger.info(
        "[cluster_prepare_dispatch] ok home={home} elapsed={elapsed:.1f}s",
        home=home,
        elapsed=time.monotonic() - started,
    )
    return PrepareDispatchResult(
        machine=machine,
        home=str(home),
        plan_digest=plan_digest,
        receipt_digest=validation.receipt_digest,
    )
