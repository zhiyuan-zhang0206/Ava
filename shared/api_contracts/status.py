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

from pydantic import (
    BaseModel,
    ConfigDict,
)

from shared.last_update import LastUpdate
from shared.resource_sample import ResourceSample


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
    # read it). `on_pin` is the gateway's server-side verdict comparing head_sha
    # to the cluster pin (`cluster_target_sha`): True = on the pinned commit,
    # False = drifted off it, None = no pin set yet or head_sha unknown. The pin
    # is cluster-global, so the verdict is computed once and stamped per row to
    # keep the roster a bare list (the CLI has no separate pin lookup).
    head_sha: str | None = None
    on_pin: bool | None = None
    # The commit the process that answered this node's ClusterStatus probe froze
    # at its own boot (`shared.process_sha`; None when the probe failed or the
    # process froze nothing). head_sha is the checkout the pin verdict compares;
    # running_sha is the code that process holds. They diverge when the checkout
    # advanced but the process was not restarted — a node shown "on pin ✓" can
    # still be running stale code, which only running_sha reveals.
    running_sha: str | None = None
    # The live deploy lease (`shared.cluster_lock.read_update_lease().describe()`):
    # holder, how long it has been held, when it lapses, plus the settle note when
    # it is a hold rather than an executing rollout. None = no live lease. The lease
    # is cluster-global, so — exactly like `on_pin` — it is read once server-side and
    # stamped identically onto every row to keep the roster a bare list.
    #
    # **This is the lease signal alone (signal 1 of `ops.deploy_window`), not that
    # module's full refusal verdict.** The roster does not run the local / remote
    # orchestration probes, so None here is not proof that no deploy is running: a
    # watchdog-spawned host-local `ava-updater` takes no lease at all. The lease is
    # shown because it is the one signal that stays true while the transitioning host
    # is unreachable, which is when an operator most needs it.
    deploy_hold: str | None = None
    # The cluster's last update outcome (`shared.last_update.LastUpdate`), or None
    # when no update has been recorded. Cluster-global, so — exactly like `on_pin`
    # and `deploy_hold` — it is read once server-side and stamped identically onto
    # every row, keeping the roster a bare list the CLI can render without a second
    # lookup.
    #
    # It is here because a failed rollout otherwise leaves only *symptoms* on this
    # roster: a head/pin mismatch, or a head_sha that disagrees with running_sha.
    # Those are shared by several unrelated states, so an operator reading one has
    # to reconstruct which. This states the fact instead (#1012).
    last_update: LastUpdate | None = None
    # The cluster's rollback anchor (`cluster_pin.last_known_good_sha`), stamped
    # cluster-globally like the fields above. Recorded since the pin existed and
    # shown nowhere until now, which is why a rollback read as the pin
    # inexplicably moving backwards instead of as a fall back to this commit.
    cluster_last_known_good_sha: str | None = None
    # This row's name appears in the live settle hold's recorded waiting-for set
    # (`shared.cluster_lock.settle_hosts` over the lease note) — the hosts that acked
    # their self-update and were still converging when the rollout's Phase B poll gave
    # up.
    #
    # **A record, not a verdict.** It is read off the lease row; no probe informs it.
    # True does not mean this host is still off the pin — the hold is only re-examined
    # (and released on convergence) when `ops.deploy_window.deploy_in_flight` is asked,
    # which reading a roster does not do. False does not mean converged — a host that
    # never acked is not covered by a settle hold at all. The live per-host question is
    # `head_sha` / `running_sha` / `on_pin`; this field is only "what the hold says it
    # is waiting for".
    settle_waited_on: bool = False
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
    watchdog_online: bool | None = None
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
