"""MachineStatus row shapers + row stamping for the roster fan-out (gateway/routers/status.py).

The three abnormal-state rows — reachable-unknown, offline, identity mismatch —
are pure functions of a machines-table row: no probe state, no backoff, no
cluster-global markers. `pin_verdict` / `stamp_cluster_globals` apply the
cluster-global facts (pin, deploy lease, last update, stranded holds) onto every
assembled row, and `read_stranded_holds` is the one DB read that feeds them.
Split out of status.py so the roster module stays under the 800-line budget
while the row contract lives in one place."""

from __future__ import annotations

import logging
from datetime import datetime

from shared.api_contracts.status import MachineStatus
from shared.cluster_lock import DeployLease, settle_hosts
from shared.last_update import LastUpdate

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


def pin_verdict(head_sha: str | None, cluster_target_sha: str | None) -> bool | None:
    """Whether a node is on the cluster pin. None when there is no pin yet or the
    node's head_sha is unknown (the comparison is meaningless); else head == pin."""
    if cluster_target_sha is None or head_sha is None:
        return None
    return head_sha == cluster_target_sha


def stamp_cluster_globals(
    machines: list[MachineStatus],
    *,
    cluster_target_sha: str | None,
    deploy_lease: DeployLease | None,
    last_update: LastUpdate | None,
    last_known_good_sha: str | None,
    stranded_holds: dict[str, tuple[datetime, str | None]] | None = None,
) -> list[MachineStatus]:
    """Apply the cluster-global facts to every assembled row; return them sorted.

    The hold's OWN population is read back from its note — never the machine
    table; a row absent from it is "not named by this hold", not "converged".
    `stranded_holds` is the one per-host entry here (task #3132); every other
    fact stamps every row identically.
    """
    hold_detail = deploy_lease.describe() if deploy_lease is not None else None
    waited_on = frozenset(settle_hosts(deploy_lease.note) if deploy_lease is not None else [])
    held = stranded_holds or {}
    stamped: list[MachineStatus] = []
    for m in machines:
        since, reason = held.get(m.name, (None, None))
        stamped.append(
            m.model_copy(
                update={
                    "on_pin": pin_verdict(m.head_sha, cluster_target_sha),
                    "deploy_hold": hold_detail,
                    "settle_waited_on": m.name in waited_on,
                    "last_update": last_update,
                    "cluster_last_known_good_sha": last_known_good_sha,
                    "stranded_hold_since": since,
                    "stranded_hold_reason": reason,
                }
            )
        )
    stamped.sort(key=lambda m: m.name)
    return stamped


def read_stranded_holds() -> dict[str, tuple[datetime, str | None]]:
    """Machine -> `(since, reason)` for every host with a stranded-hold record.

    Read once per roster assembly and stamped per row, the same shape and same
    degradation as the other cluster-global readers: a transient
    `OperationalError` (the window this record exists for is exactly when such
    reads are likeliest) leaves the banner off rather than taking the roster
    down. Deliberately a DB read, not a probe: the held host's own ops server
    is usually down with it, so only the row it wrote before going quiet can
    carry the fact (task #3132).
    """
    import psycopg

    from shared.host_deploy_state import read_all

    try:
        return {
            name: (state.stranded_hold_since, state.stranded_hold_reason)
            for name, state in read_all().items()
            if state.stranded_hold_since is not None
        }
    except psycopg.OperationalError:
        return {}
    except Exception:
        _log.exception("reading stranded-hold records failed (the held-host banner will be blank)")
        return {}
