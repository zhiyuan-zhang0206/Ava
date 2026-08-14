"""MachineStatus row shapers for the roster fan-out (gateway/routers/status.py).

The three abnormal-state rows — malformed probe, offline, identity mismatch —
are pure functions of a machines-table row: no probe state, no backoff, no
cluster-global markers. Split out of status.py so the roster module stays under
the 800-line budget while the row contract lives in one place."""

from __future__ import annotations

from datetime import datetime

from shared.api_contracts.status import MachineStatus


def malformed_probe_status(
    name: str,
    role: list[str],
    gateway_url: str | None,
    up_since_at: datetime,
    description: str | None,
    stopped_at: datetime | None,
    *,
    is_staging: bool = False,
) -> MachineStatus:
    """The MachineStatus row for a host whose ops server answered 200 but whose
    body did NOT validate as ClusterStatus.

    The documented online=True + paused=None abnormal state (see MachineStatus):
    the probe got through, but the response did not match the status_probe
    contract (a version-skewed / wrong server). A loud unknown state — never
    disguised as a determinate paused verdict, and distinct from offline (the
    probe never reached the host at all).
    """
    return MachineStatus(
        name=name,
        serve_gateway="gateway" in role,
        serve_agent_runner="agent-runner" in role,
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
