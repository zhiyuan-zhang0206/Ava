"""Channel E's unit-side handler: start one unit's continuation step.

The daemon-side half of task #4129 channel E (design
`managed-writer-dispatch-design-20260920.md` section 3.3): verify the payload's
request path as canonical private unit state (the shared
`ops.unit_local.private_unit_reference` stat face the hop handler also uses),
then spawn the detached `ava-updater` session that runs the retained candidate
image's `--normal-release` (step "drive") or `--normal-commit` (step "commit")
entry against it.

The handler never reads the request's content, never pauses anything and seeds
no handoff of its own: the request is a request, and the child's own
compare-and-set plus its re-read of the live rollout lease refuse a live or
unproven owner.

Linux-only by construction (the design's Q7, the same gate as the restricted
hop): the platform check refuses before anything is spawned -- the POSIX
command spelling in `spawn_normal_continue` is reached only past this gate.
"""

from __future__ import annotations

import os
import sys

from ops import unit_local, updater_entries
from ops.rpc_normal_continue import NormalContinuePayload, NormalContinueResult
from shared.config import settings
from shared.log import logger
from shared.runtime_release import ReleaseRejectedError


def cluster_normal_continue_op(payload: NormalContinuePayload) -> NormalContinueResult:
    if sys.platform != "linux":
        raise ReleaseRejectedError("normal continuation has no native proof on this platform")
    home = settings.general.ava_home
    machine = unit_local.machine_identity(home)
    request = unit_local.private_unit_reference(payload.continue_request, home)
    logger.info(
        "[cluster_normal_continue] start pid={pid} image={image} step={step} request={request}",
        pid=os.getpid(),
        image=payload.artifact_digest,
        step=payload.step,
        request=request.name,
    )
    spawned = updater_entries.spawn_normal_continue(
        request, step=payload.step, artifact_digest=payload.artifact_digest
    )
    return NormalContinueResult(
        machine=machine,
        home=str(home),
        session=spawned["session"],
        log=spawned["log"],
    )
