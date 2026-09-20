"""`cluster_bootstrap_hop` op handler: start one unit's restricted hop session.

The daemon-side half of task #4129 channel C (design
`managed-writer-dispatch-design-20260920.md` section 3.3): verify the payload's
request path as canonical private unit state (the stat face
`cli.commands._update_bootstrap._private_reference` and
`services.agent_ops.bootstrap.read_prepared_context` share, plus the size bound
`read_prepared_context` pairs it with), then spawn the detached `ava-updater`
session that runs the retained candidate image's `--bootstrap-hop` entry
against it.

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
import stat
import sys
from pathlib import Path

from ops import cluster_deploy
from ops.rpc_bootstrap_hop import BootstrapHopPayload, BootstrapHopResult
from ops.unit_local import machine_identity
from shared.config import settings
from shared.log import logger
from shared.runtime_release import ReleaseRejectedError

# The request carries whole resolved contexts (observations, operations,
# challenges, the predecessor's identity); the CI assembler's request measures a
# few KiB. 64 KiB is the same shape bound `read_prepared_context` enforces on the
# child side, so a swapped or miswired path refuses here instead of surfacing as
# a child failure. KEEP (task #3696 exception inventory): a relay-shape guard
# fixed by the request's shape, not a tuning knob.
_MAX_REQUEST_BYTES = 64 * 1024


def _private_request(text: str, home: Path) -> Path:
    """The payload's request path, verified as canonical private unit state.

    The same stat face as `_update_bootstrap._private_reference` (absolute,
    canonical, owned, 0600, inside `{home}/run`) plus the regular-file and size
    checks `read_prepared_context` pairs it with. Checked before anything is
    spawned -- a path that fails it is a wiring fault, not a verdict about the
    hop.
    """
    path = Path(text)
    try:
        info = path.stat() if path.is_absolute() and path.resolve(strict=True) == path else None
    except OSError:
        info = None
    if (
        info is None
        or path.parent != home / "run"
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
        or info.st_size > _MAX_REQUEST_BYTES
    ):
        raise ReleaseRejectedError(
            "bootstrap hop request must be a canonical private unit reference"
        )
    return path


def cluster_bootstrap_hop_op(payload: BootstrapHopPayload) -> BootstrapHopResult:
    if sys.platform != "linux":
        raise ReleaseRejectedError("restricted hop has no native proof on this platform")
    home = settings.general.ava_home
    machine = machine_identity(home)
    request = _private_request(payload.hop_request, home)
    logger.info(
        "[cluster_bootstrap_hop] start pid={pid} image={image} request={request}",
        pid=os.getpid(),
        image=payload.artifact_digest,
        request=request.name,
    )
    spawned = cluster_deploy.spawn_bootstrap_hop(request, artifact_digest=payload.artifact_digest)
    return BootstrapHopResult(
        machine=machine,
        home=str(home),
        session=spawned["session"],
        log=spawned["log"],
    )
