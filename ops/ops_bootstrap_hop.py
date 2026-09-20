"""Channel C's unit-side handlers: start one unit's restricted hop session, and
report its bootstrap-recovery journal slot read-only.

The daemon-side half of task #4129 channel C (design
`managed-writer-dispatch-design-20260920.md` section 3.3): verify the payload's
request path as canonical private unit state (the shared
`ops.unit_local.private_unit_reference` stat face), then spawn the detached
`ava-updater` session that runs the retained candidate image's `--bootstrap-hop`
entry against it.

The handler never reads the request's content, never pauses anything and seeds
no handoff of its own: the request is a request, and the child's own
compare-and-set (`begin_bootstrap_after_dead_owner`) plus
`prepare_bootstrap_hop`'s re-read of the live rollout lease are what refuse a
live or unproven owner. That is also what keeps the coordinator's pre-stop
abort exact (task #4129 C-4): until the child's first effect, this unit's whole
footprint of the hop is one private file it did not write plus a process that
will refuse rather than act.

Linux-only by construction: the restricted hop's native proof (cron ownership,
A/B observation) has no other platform's arm, so the platform check refuses
before anything is spawned -- the POSIX command spelling in
`spawn_bootstrap_hop` is reached only past this gate.
"""

from __future__ import annotations

import os
import sys
from typing import cast

from ops import unit_local, updater_entries
from ops.rpc_bootstrap_hop import (
    BootstrapHopPayload,
    BootstrapHopResult,
    BootstrapRecoveryReadPayload,
    BootstrapRecoveryReadResult,
)
from shared import updater_handoff
from shared.config import settings
from shared.log import logger
from shared.runtime_release import ReleaseRejectedError


def cluster_bootstrap_hop_op(payload: BootstrapHopPayload) -> BootstrapHopResult:
    if sys.platform != "linux":
        raise ReleaseRejectedError("restricted hop has no native proof on this platform")
    home = settings.general.ava_home
    machine = unit_local.machine_identity(home)
    request = unit_local.private_unit_reference(payload.hop_request, home)
    logger.info(
        "[cluster_bootstrap_hop] start pid={pid} image={image} request={request}",
        pid=os.getpid(),
        image=payload.artifact_digest,
        request=request.name,
    )
    spawned = updater_entries.spawn_bootstrap_hop(request, artifact_digest=payload.artifact_digest)
    return BootstrapHopResult(
        machine=machine,
        home=str(home),
        session=spawned["session"],
        log=spawned["log"],
    )


def cluster_bootstrap_recovery_read_op(
    payload: BootstrapRecoveryReadPayload,
) -> BootstrapRecoveryReadResult:
    """Report this unit's bootstrap-recovery journal slot, read-only (C-4).

    The exact pre-stop abort's per-unit no-effect proof: the hop child's first
    durable write is this journal (before it, every action was a staged-file
    write or a read-only check), so "absent on every unit" is exactly "no unit
    has acted". A present journal is reported as the fact it is -- readable or
    not -- and the coordinator refuses; nothing here writes, stops or starts
    anything.
    """
    del payload  # no arguments: the question is about this unit's own slot
    home = settings.general.ava_home
    machine = unit_local.machine_identity(home)
    try:
        envelope = updater_handoff.read_bootstrap_recovery()
    except updater_handoff.BootstrapRecoveryInvalidError:
        # Present but unreadable is still a recorded effect.
        return BootstrapRecoveryReadResult(machine=machine, home=str(home), journal_present=True)
    if envelope is None:
        return BootstrapRecoveryReadResult(machine=machine, home=str(home), journal_present=False)
    stage: str | None = None
    normal_stage: str | None = None
    journal_raw = envelope.get("journal")
    if isinstance(journal_raw, dict):
        body = cast("dict[str, object]", journal_raw)
        value = body.get("stage")
        if isinstance(value, str):
            stage = value
        normal_raw = body.get("normal_release")
        if isinstance(normal_raw, dict):
            normal_value = cast("dict[str, object]", normal_raw).get("stage")
            if isinstance(normal_value, str):
                normal_stage = normal_value
    return BootstrapRecoveryReadResult(
        machine=machine,
        home=str(home),
        journal_present=True,
        journal_stage=stage,
        normal_release_stage=normal_stage,
    )
