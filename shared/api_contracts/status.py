"""Machine-status API contract — the roster row the gateway serves and the CLI
renders.

Downshifted from `gateway/schemas/status.py` so both sides of the wire can name
one type: the gateway registers it on `/api/cluster/roster` + the status panel
(so it keeps its OpenAPI schema name `MachineStatus`), and `cli` thin clients
(`ava cluster status`) validate the response against it without importing up
into `gateway`. Gateway-only status models (ClusterPanel, SystemStatus,
ServiceItem, ...) stay in `gateway.schemas.status`.
"""

from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
)

from shared.resource_sample import ResourceSample

# Why a host's status snapshot reads `paused` — the first true clause of the
# paused verdict, in the verdict's own order (ops/cluster_status.py::_paused_reason):
#   no_state       — the deploy state read failed or its row is absent
#   business_pause — host_deploy_state.posture reads "paused"
#   maintenance    — a native admission hold (stop window / maintenance owner)
#   startup        — the serving gate has not reached `serving`
PausedReason = Literal["no_state", "business_pause", "maintenance", "startup"]


SchemaMismatchKind = Literal[
    "schema-ahead-of-code",
    "schema-behind-code",
    "divergent",
    "invalid-migration-layout",
    "unavailable",
]


class SchemaMismatchStatus(BaseModel):
    """A current schema mismatch, invalid layout, or unavailable comparison."""

    model_config = ConfigDict(frozen=True)

    kind: SchemaMismatchKind
    machine: str
    detail: str


class MachineStatus(BaseModel):
    """A machines-table row state — augmented with live probe results.

    `online` / `paused` come from each machine's ops `status_probe` within
    the configured roster deadline:
    - online=True + paused has a value: probe succeeded
    - online=False + paused=None: probe failed (network unreachable /
      gateway down)
    - online=True + paused=None: ops server responded, but the operation failed
      or its response did not match ClusterStatus (abnormal)
    """

    model_config = ConfigDict(frozen=True)

    name: str
    # Three orthogonal capability flags — any combination can be true on a
    # single host. The roster groups machines by capability rather than
    # carrying a single categorical "role", so a machine is never reasoned
    # about as "having multiple roles": it just answers independent yes/no
    # questions. serve_observability_station defaults False (added to the wire
    # in WP2) so a client talking to a pre-station gateway parses its rows —
    # the roster row's capability set is also derivable from the machines
    # table's role column server-side.
    serve_gateway: bool
    serve_agent_runner: bool
    serve_observability_station: bool = False
    gateway_url: str
    # When a process owning this machine last announced it was up (`ava start`,
    # or the ops daemon at its own boot) — a boot/announce stamp, NOT a
    # heartbeat, which is why it is rendered "up since" and never freshness-tested.
    # Liveness is `online`, a live probe. Called `last_seen_at` until #981.
    up_since_at: datetime
    online: bool
    paused: bool | None  # None = unknown (probe failed)
    # Which clause of the host's paused verdict fired, when known (no_state /
    # business_pause / maintenance / startup — see
    # ops/cluster_status.py::_paused_reason). None = not paused, or the row
    # carries no verdict to decompose (probe failed / abnormal-state row).
    paused_reason: PausedReason | None = None
    description: str | None = None  # free-text machine metadata; NULL when unset
    # Set when the host announced an intentional `ava stop` (cleared on next
    # `ava start`). Lets a consumer show offline+stopped_at as "stopped"
    # (deliberate) vs offline+null as "offline" (crash) — the live probe alone
    # cannot tell them apart.
    stopped_at: datetime | None = None
    # Operator-set staging flag (migration 20260812T000000): a staging host is
    # registered + roster-visible but excluded from the rollout target set
    # (`list_agent_runners` / the fan-out). Read-only on this wire — set via
    # `ava cluster mark-staging` / `unmark-staging`.
    is_staging: bool = False
    # This node's prod-source HEAD commit (None when the probe failed / could not
    # read it). There is no cluster pin to compare it against.
    head_sha: str | None = None
    # The commit the process that answered this node's ClusterStatus probe froze
    # at its own boot (`shared.process_sha`; None when the probe failed or the
    # process froze nothing). head_sha is the checkout; running_sha is the code
    # that process holds. They diverge when the checkout advanced but the process
    # was not restarted — only running_sha reveals the stale code.
    running_sha: str | None = None
    schema_mismatch: SchemaMismatchStatus | None = None
    # The live deploy lease (`shared.cluster_lock.read_update_lease().describe()`):
    # holder, how long it has been held, when it lapses, plus the settle note when
    # it is a hold rather than an executing rollout. None = no live lease. The lease
    # is cluster-global, so it is read once server-side and stamped identically
    # onto every row to keep the roster a bare list.
    #
    # **This is the lease signal alone (signal 1 of `ops.deploy_window`), not that
    # module's full refusal verdict.** The roster does not read the per-host posture
    # rows, so None here is not proof that no deploy is running: host-local
    # maintenance takes no cluster lease at all. The lease is
    # shown because it is the one signal that stays true while the transitioning host
    # is unreachable, which is when an operator most needs it.
    deploy_hold: str | None = None
    # The probe responder self-reported a machine_name that did NOT match the row
    # this probe targeted — a structural red flag that a loopback/misregistered
    # gateway_url made the gateway dial the wrong host (or itself) and answer under
    # the wrong identity. A loud third state, neither online nor a plain offline:
    # when True, `online` is False and the roster marks the row "identity-mismatch".
    identity_mismatch: bool = False
    # Per-host runtime, mirrored from this node's ClusterStatus probe. shell_count
    # is the live agent shell-session count; agent-host/watchdog are pidfile
    # liveness. All default to the "unknown" value used when a probe times out.
    shell_count: int = 0
    agent_host_online: bool | None = None
    supervisor_online: bool | None = None
    # Agent-runner detail surfaced on the Status Page.
    agent_count: int = 0
    session_count: int = 0
    # Sessions grouped by agent for hierarchical display.
    agent_groups: list[dict[str, object]] = []
    # This machine's LIVE CPU / memory / disk reading (shared.resource_sample) —
    # one sample, not a series: the history lives in Prometheus (issue #46) and
    # this is the degraded answer for a deployment without the LGTM backend.
    # None when psutil could not read the machine.
    resource: ResourceSample | None = None
