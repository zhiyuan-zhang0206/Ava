"""system + cluster status.

Split out of the former monolithic ops/schemas.py; FastAPI registers these
unchanged, so the OpenAPI codegen is byte-identical to the wire before.

`MachineStatus` (the roster row the CLI also decodes) is downshifted to
`shared.api_contracts.status` and re-exported here; the models below are the
gateway-only status surface.
"""

from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
)

# Re-exported so `gateway.schemas` keeps serving MachineStatus under its OpenAPI
# name; the definition lives in shared so `cli` can decode the roster too.
from shared.api_contracts.status import MachineStatus
from shared.last_update import LastUpdate


class ServiceItem(BaseModel):
    """Online status of a single daemon."""

    model_config = ConfigDict(frozen=True)

    name: str
    label: str
    online: bool | None  # None = unknown (cannot probe)
    pid: int | None
    detail: str | None


class ServicesStatus(BaseModel):
    """GET /api/status services sub-section."""

    model_config = ConfigDict(frozen=True)

    items: list[ServiceItem]


class ClusterPanel(BaseModel):
    """GET /api/status cluster sub-section — multi-machine view.

    `current_*` is the perspective of the gateway that received this
    request; `machines` is every machine registered in DB + live probe
    results.
    """

    model_config = ConfigDict(frozen=True)

    current_machine: str
    # This host's three capability flags (any combination on a single-box host).
    # current_serve_observability_station defaults False so pre-station clients
    # parse the payload.
    current_serve_gateway: bool
    current_serve_agent_runner: bool
    current_serve_observability_station: bool = False
    current_paused: bool  # whether the current gateway is paused (local is_paused())
    # The whole-cluster orchestration in flight on this gateway, or None
    # when idle. A rollout / restart runs for minutes in a detached session after
    # the trigger POST returns; this is the durable in-flight signal the panel
    # disables the Update / Restart actions on (current_paused only flips once the
    # orchestration reaches the gateway's own stop, leaving an early window
    # where a second trigger could fire).
    current_orchestration: Literal["rollout", "restart", "update"] | None = None
    machines: list[MachineStatus]
    # The cluster's pinned commit (`cluster_target_sha`), or None if no rollout
    # has pinned one yet. Lets the panel show "cluster pinned to <sha>" alongside
    # each machine's on_pin verdict.
    cluster_target_sha: str | None = None
    # The cluster's last update outcome, or None when none has been recorded. The
    # panel's failure banner is switched on `last_update.failed` — a stated fact —
    # rather than on a pin/head colour, which is a symptom several unrelated states
    # share (#1012).
    last_update: LastUpdate | None = None
    # The cluster's rollback anchor (`cluster_pin.last_known_good_sha`), so a pin
    # that moved backwards reads as "rolled back to this" rather than as drift.
    cluster_last_known_good_sha: str | None = None


class SystemStatus(BaseModel):
    """GET /api/status response — System Status panel data all in one go."""

    model_config = ConfigDict(frozen=True)

    services: ServicesStatus
    cluster: ClusterPanel


class AgentMachineRow(BaseModel):
    """One machine as exposed to agents via ava.agents.list_machines().

    Intentionally minimal: name + free-text description + determinate liveness.
    A paused host is still live, but a reached host whose status operation
    failed has no liveness verdict, so `live=False` until a probe returns a
    concrete paused value. role / gateway_url stay internal to ops and are not
    surfaced to agents.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str | None = None
    live: bool
    # Operator-set staging flag — agents see it so a peer can tell a staging
    # host from a production rollout target (e.g. before enrolling against it).
    is_staging: bool = False


class MachineDeleteResponse(BaseModel):
    """DELETE /api/cluster/machines/{name} response."""

    model_config = ConfigDict(frozen=True)

    deleted: bool  # True = row existed and was removed; False = already absent


class MachinePauseRequest(BaseModel):
    """POST /api/cluster/machines/{name}/pause body.

    `reason` is free-text why the machine is being pulled out (e.g. "a week off")
    — recorded on the machines row as `pause_reason` for the resume checklist.
    """

    reason: str = ""


class MachinePauseResponse(BaseModel):
    """POST /api/cluster/machines/{name}/pause response — what the pause did.

    The pause is the three-step operator act: drain (reassign in_progress
    tasks owned by the machine's agents to the drain owner), terminate every
    live agent on the machine (graceful via its ops server; agents whose
    graceful terminate could not be enqueued — machine already unreachable —
    are force-marked terminated in the shared DB), then set the pause latch.
    `paused_at`/`pause_reason` are the row values after the latch write.
    Idempotent: pausing an already-paused machine terminates nothing (its
    agents are already terminated) and returns the existing latch values.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    paused: bool  # True = the machine now carries the pause latch
    terminated_agents: int  # killed + marked terminated via the machine's ops server
    force_marked_agents: int  # ops unreachable; row force-marked terminated in the DB
    reassigned_tasks: int  # in_progress tasks drained to the drain owner
    paused_at: datetime | None = None
    pause_reason: str | None = None


class MachineResumeResponse(BaseModel):
    """POST /api/cluster/machines/{name}/resume response.

    `resumed` True = the pause latch was cleared (the machine is a normal
    cluster member again); False = it was not paused (idempotent no-op).
    """

    model_config = ConfigDict(frozen=True)

    name: str
    resumed: bool
