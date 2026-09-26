"""MachineStatus row shapers + row stamping for the roster fan-out (gateway/routers/status.py).

The three abnormal-state rows — reachable-unknown, offline, identity mismatch —
are pure functions of a machines-table row: no probe state, no backoff, no
cluster-global markers. `stamp_cluster_globals` applies the cluster-global
deploy lease onto every assembled row.
Split out of status.py so the roster module stays under the 800-line budget
while the row contract lives in one place."""

from __future__ import annotations

import logging
from datetime import datetime

from shared.api_contracts.status import MachineStatus
from shared.cluster_lock import DeployLease

_log = logging.getLogger("gateway.routers._roster_rows")


def reachable_unknown_status(
    name: str,
    role: list[str],
    gateway_url: str | None,
    up_since_at: datetime,
    description: str | None,
    stopped_at: datetime | None,
    *,
    is_staging: bool = False,
) -> MachineStatus:
    """The MachineStatus row for a reachable host whose status is unknown.

    The documented online=True + paused=None abnormal state (see MachineStatus):
    the probe got through, but either the operation failed or its response did
    not match the status_probe contract. A loud unknown state — never disguised
    as a determinate paused verdict, and distinct from offline (the probe never
    reached the host at all).
    """
    return MachineStatus(
        name=name,
        serve_gateway="gateway" in role,
        serve_agent_runner="agent-runner" in role,
        serve_observability_station="observability-station" in role,
        gateway_url=gateway_url or "",
        up_since_at=up_since_at,
        online=True,
        paused=None,
        description=description,
        stopped_at=stopped_at,
        is_staging=is_staging,
        head_sha=None,
    )


def offline_status(
    name: str,
    role: list[str],
    gateway_url: str | None,
    up_since_at: datetime,
    description: str | None,
    stopped_at: datetime | None,
    *,
    is_staging: bool = False,
) -> MachineStatus:
    """The MachineStatus row for a host we could not get a usable probe from."""
    return MachineStatus(
        name=name,
        serve_gateway="gateway" in role,
        serve_agent_runner="agent-runner" in role,
        serve_observability_station="observability-station" in role,
        gateway_url=gateway_url or "",
        up_since_at=up_since_at,
        online=False,
        paused=None,
        description=description,
        stopped_at=stopped_at,
        is_staging=is_staging,
        head_sha=None,
    )


def identity_mismatch_status(
    name: str,
    role: list[str],
    gateway_url: str | None,
    up_since_at: datetime,
    description: str | None,
    stopped_at: datetime | None,
    *,
    is_staging: bool = False,
) -> MachineStatus:
    """The MachineStatus row for a host whose ops server answered under a DIFFERENT
    machine_name than the row we targeted.

    A loud, distinct state — `online=False` + `identity_mismatch=True` — so a
    loopback/misregistered gateway_url that makes the gateway dial itself (and
    answer 200 under its own name) can no longer masquerade as the target host
    online. Structurally impossible to render as a false green.
    """
    return MachineStatus(
        name=name,
        serve_gateway="gateway" in role,
        serve_agent_runner="agent-runner" in role,
        serve_observability_station="observability-station" in role,
        gateway_url=gateway_url or "",
        up_since_at=up_since_at,
        online=False,
        paused=None,
        description=description,
        stopped_at=stopped_at,
        is_staging=is_staging,
        head_sha=None,
        identity_mismatch=True,
    )


def stamp_cluster_globals(
    machines: list[MachineStatus],
    *,
    deploy_lease: DeployLease | None,
) -> list[MachineStatus]:
    """Apply the cluster-global deploy lease to every assembled row; return them sorted."""
    hold_detail = deploy_lease.describe() if deploy_lease is not None else None
    stamped: list[MachineStatus] = []
    for m in machines:
        stamped.append(m.model_copy(update={"deploy_hold": hold_detail}))
    stamped.sort(key=lambda m: m.name)
    return stamped
