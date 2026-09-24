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

The op's payload/result schemas live here too, folded from
`ops/rpc_normal_continue.py` at the structure budget (task #4129 I6): the wire
shape rides with its handler.
"""

from __future__ import annotations

import os
import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ops import ops_bootstrap_hop, unit_local
from shared.config import settings
from shared.log import logger
from shared.managed_writer_barrier import Digest
from shared.runtime_release import ReleaseRejectedError


class NormalContinuePayload(BaseModel):
    """``cluster_normal_continue`` payload: the unit-local request path + step.

    `continue_request` names one existing private file inside this unit's `run/`
    directory (`{home}/run/...`, 0600, owned); `step` selects which half of the
    unit's publication tail this dispatch drives; `artifact_digest` selects the
    retained image whose interpreter runs the entry. Both locate and launch --
    the entry re-verifies the whole context from the request bytes itself.
    """

    model_config = ConfigDict(extra="forbid")

    continue_request: str = Field(min_length=1, max_length=4096)
    step: Literal["drive", "commit"]
    artifact_digest: Digest


class NormalContinueResult(BaseModel):
    """``cluster_normal_continue`` result: the spawned entry session, as facts.

    `session` is the detached `ava-updater` session's name and `log` its append
    target.
    """

    model_config = ConfigDict(extra="forbid")

    machine: str = Field(min_length=1, max_length=128)
    home: str = Field(min_length=1, max_length=4096)
    session: str = Field(min_length=1, max_length=128)
    log: str = Field(min_length=1, max_length=4096)


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
    spawned = ops_bootstrap_hop.spawn_normal_continue(
        request, step=payload.step, artifact_digest=payload.artifact_digest
    )
    return NormalContinueResult(
        machine=machine,
        home=str(home),
        session=spawned["session"],
        log=spawned["log"],
    )
