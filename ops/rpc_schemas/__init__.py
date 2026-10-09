"""Gateway <-> agent-runner RPC-shared schemas — the cross-process wire
contract. These types are produced/consumed on BOTH sides of the ops RPC
(the gateway HTTP surface AND the agent-runner ops handlers in ops/ +
services/), so by the import layering (base < ops < gateway) they live in
the ops layer: gateway imports them downward, and ops/services never have to
reach up into gateway.

This package door holds the op envelope and per-kind payload/result models.
Focused exchanges live in its submodules — the terminate exchange (request /
response / open-task hint) in `terminate`, the shared content guardrail in
`content`, the chat request models in `messages`, platform completion metadata
in `completion`, and the billing batch-recovery exchange in `billing_recovery`;
the door re-exports the models its importers use.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, TypeGuard, get_args
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from base.agents import (
    CrashRecoveryResult,
    RestartResult,
    ResurrectResult,
    ShellKillMode,
)
from base.agents.messages.envelope import reject_unnegotiated_caller, validate_writable_source
from base.agents.messages.inbound import WakeTriggerKind
from base.agents.observation.evidence import AvailabilityReason
from base.api_contracts.op_envelope import OpEnvelope as OpEnvelope

# Re-exported so existing `ops.rpc_schemas` importers keep their import paths.
from ops.rpc_schemas.billing_recovery import BillingBalanceReport as BillingBalanceReport
from ops.rpc_schemas.billing_recovery import BillingHaltedAliveRow as BillingHaltedAliveRow
from ops.rpc_schemas.billing_recovery import (
    BillingResurrectAgentOutcome as BillingResurrectAgentOutcome,
)
from ops.rpc_schemas.billing_recovery import (
    BillingResurrectAgentResponse as BillingResurrectAgentResponse,
)
from ops.rpc_schemas.billing_recovery import BillingResurrectRequest as BillingResurrectRequest
from ops.rpc_schemas.billing_recovery import BillingResurrectResponse as BillingResurrectResponse
from ops.rpc_schemas.content import UserContent
from ops.rpc_schemas.launch_retry import (
    LaunchReconciled as LaunchReconciled,
)
from ops.rpc_schemas.launch_retry import (
    LaunchReconcileRequest as LaunchReconcileRequest,
)
from ops.rpc_schemas.launch_retry import (
    RetryLaunchAccepted as RetryLaunchAccepted,
)
from ops.rpc_schemas.launch_retry import (
    RetryLaunchRequest as RetryLaunchRequest,
)
from ops.rpc_schemas.messages import AgentMessageIn as AgentMessageIn
from ops.rpc_schemas.messages import ContentBlock as ContentBlock
from ops.rpc_schemas.messages import ImageUrlContentBlock as ImageUrlContentBlock
from ops.rpc_schemas.messages import ImageUrlRef as ImageUrlRef
from ops.rpc_schemas.messages import TextContentBlock as TextContentBlock

# Re-exported so existing `ops.rpc_schemas` importers keep their import paths.
from ops.rpc_schemas.terminate import OpenTaskRow as OpenTaskRow
from ops.rpc_schemas.terminate import OpenTasksHint as OpenTasksHint
from ops.rpc_schemas.terminate import TerminateAgentRequest as TerminateAgentRequest
from ops.rpc_schemas.terminate import TerminateAgentResponse as TerminateAgentResponse


class SpawnAgentRequest(BaseModel):
    """POST /api/agents request body — for frontend / SDK; frontend does
    not need to know checkpoint ids.

    If `fork_from` is given, the gateway resolves the latest checkpoint
    internally and passes an explicit id to the underlying create_agent_row
    (consistent with SDK `ava.agents.spawn(fork_from=N)` logic).

    Fork config rule: a fork keeps the source's effective config so the
    inherited context stays cache-valid. The only allowed config change is
    ADDING skills to `skills_to_inject_into_system_prompt` /
    `skills_to_expand_at_start` (supersets); anything else is rejected with
    `fork_config_change_not_allowed`. A fork WITHOUT config inherits the
    source's resolved overlay + preset verbatim (the pre-2026-09-10 behavior
    dropped the source's overlay).

    `spawner` default "user" — frontend spawn button does not need to
    pass it; external callers (claude-code / SDK paths `agent:N` etc.)
    pass their own identifier so the frontend sidebar groups them under
    a separate root section by spawner.

    `prompt_source` is **required only when prompt is given** —
    INSERTed into inbound_messages.source; the envelope wrap marks who
    the message came from. Frontend passes "user"; SDK paths pass
    f"agent:{my_id}". This differs from spawner: spawner is "who
    created the agent", source is "from whom the prompt arrived".

    The schema does not give prompt_source a default — to avoid the
    "caller forgot to pass it and got silently tagged user,
    contaminating envelope source" anti-pattern (one of the
    `or default` forms AGENTS.md prohibits); both kinds of callers must
    explicitly identify themselves.
    """

    model_config = ConfigDict(extra="forbid")

    prompt: UserContent | None = None
    spawner: str = Field(default="user", min_length=1, max_length=64)
    fork_from: int | None = Field(default=None, gt=0)
    prompt_source: str | None = Field(default=None, min_length=1, max_length=64)
    # Target physical host (multi-machine placement); None = local. The gateway creates the row (create_agent_row) and forwards a launch op.
    machine: str | None = Field(default=None, min_length=1, max_length=64)
    # Per-agent config overlay; only per_agent=True fields are accepted (enforced
    # at agent boot via apply_config_overlay). May carry `config["preset"] =
    # "<name>"` — the spawn-boundary preset reference; the gateway resolves the
    # named preset's stored overlay as the base, explicit fields win per-key, and
    # the runner only ever sees the resolved map.
    config: dict[str, object] | None = Field(default=None)
    # Optional initial label (spawner-assigned role). Stored sticky so the
    # labeler does not overwrite it; the agent can change it via ava.self.set_label.
    label: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _validate_prompt_source(self) -> "SpawnAgentRequest":
        if self.prompt is not None and self.prompt_source is None:
            raise ValueError("prompt_source required when prompt is given")
        # Reject an unrecognized source at the boundary (422) instead of
        # deferring to the agent claim node, where wrap_inbound raises
        # ValueError on the bad source and kills the just-spawned process.
        # Same legal set as the claim-side wrap — single-sourced via
        # base.agents.messages.envelope.validate_source.
        if self.prompt_source is not None:
            validate_writable_source(self.prompt_source)
        return self


class LaunchAgentRequest(BaseModel):
    """Wake a gateway-created agent using its committed launch attempt.

    The gateway commits the row and first prompt before dispatch. The runner
    validates placement and attempt identity; it never inserts another prompt.
    Config snapshots carry the per-agent settings used for admission.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: int
    launch_attempt_id: UUID
    config: dict[str, object] | None = None
    birth_config: dict[str, object] | None = None


class ConfigNormalization(BaseModel):
    """Spawn config_overlay settlement receipt (task #4306): the sent id and
    the registered fallback it was rewritten to; present only on a rewrite."""

    requested: str
    resolved: str


class SpawnedAgent(BaseModel):
    """Creation receipt; execution_observed never asserts a completed turn.

    Runner RPCs carry only `id`. The gateway adds the optional observation
    fields after the launch request is accepted, preserving older ID consumers.
    """

    id: int
    config_normalized: ConfigNormalization | None = None
    accepted: bool | None = None
    execution_observed: bool | None = None
    reason: AvailabilityReason | None = None
    observed_at: datetime | None = None


class ResurrectAgentRequest(BaseModel):
    """Resurrect agent request body.

    The public `POST /api/agents/{id}/resurrect` endpoint uses this for an
    explicit lifecycle wake with no work guard. The internal versioned
    pending-work path also carries it alongside the exact chat or compact
    request id and kind; controller recovery calls the lower lifecycle helper
    with its own exact death claim.

    `resurrected_by` default "user" — frontend resurrect button does not
    need to pass it; SDK paths pass f"agent:{my_id}"; pending-work and
    controller auto-resurrect pass "system". Written into the
    lifecycle 'resurrect' inbound's source; the claim-side dispatch
    composes it into the marker `[system ts] You have been resurrected
    by {resurrected_by}` so the agent knows who resurrected it.

    The value must pass `base.agents.messages.envelope.validate_source` (same check as
    `AgentMessageIn.source`): the same value becomes the prompt chat
    inbound's source, and the claim node's envelope wrap raises on
    anything outside the whitelist — killing the freshly resurrected
    process on its first claim. Validating here turns that process-fatal
    value into a 422 at the HTTP boundary (the agent-240 incident).

    `prompt` is **optional at this HTTP boundary**. The frontend resurrect
    button is a pure lifecycle event — there is no message to deliver, so it
    sends no prompt and the agent just gets the "you have been resurrected"
    marker. Peer agents no longer have a dedicated resurrect API — they send
    a chat message (`ava.agents.send_message`) and auto-resurrect handles the
    rest. When a prompt is given it is INSERTed as a chat inbound in **the same
    transaction** as the lifecycle 'resurrect' inbound. The session may be
    created before commit, but its child blocks on the agent row and cannot
    claim or process either inbound until both are committed.
    """

    resurrected_by: str = Field(default="user", min_length=1, max_length=64)
    prompt: UserContent | None = None

    @field_validator("resurrected_by")
    @classmethod
    def _check_envelope_source(cls, v: str) -> str:
        validate_writable_source(v)
        return v


class RestartAgentRequest(BaseModel):
    """POST /api/agents/{id}/restart request body — fully optional.

    `source` default "user" — frontend restart button does not need to
    pass it; SDK paths pass f"agent:{my_id}". Written into the lifecycle
    'restart' inbound's source; the claim-side dispatch composes it into
    the marker `[system ts] You have been restarted by {source}` so the
    agent knows who restarted it.

    `config_overlay`, when nonempty, is validated before it reaches either
    gateway or runner writes, merged into the persistent agent overlay, and
    carried in the restart inbound payload for the completion marker.
    """

    source: str = Field(default="user", min_length=1, max_length=64)
    config_overlay: dict[str, object] | None = None

    @field_validator("source")
    @classmethod
    def _check_source(cls, value: str) -> str:
        reject_unnegotiated_caller(value)
        return value

    @model_validator(mode="after")
    def _validate_config_overlay(self) -> "RestartAgentRequest":
        if self.config_overlay is None:
            return self
        from base.packages.plugins.config_registration import (
            InvalidConfigOverlay,
            validate_config_overlay_shape,
        )

        try:
            validate_config_overlay_shape(self.config_overlay)
        except InvalidConfigOverlay as exc:
            # Pydantic converts ValueError into a boundary validation failure;
            # propagating InvalidConfigOverlay directly would become a 500.
            raise ValueError(str(exc)) from exc
        return self


class RestartAgentResponse(BaseModel):
    """POST /api/agents/{id}/restart response.

    `enqueued`: native restart is durable; the host completes claim and
        checkpoint settlement before admitting the next incarnation.
    `already_terminated`: agent is dead; restart does not apply — use
        resurrect.
    """

    status: RestartResult


class ResurrectAgentResponse(BaseModel):
    """Resurrect agent response — returned by `POST /api/agents/{id}/resurrect`
    (the frontend resurrect button) and the internal auto-resurrect op.

    `spawned`: agent was dead; UPDATEd 'terminated' -> 'idling' +
        started a fresh process attached to the same agent_id
        (LangGraph state preserved; agent wakes up from where it left
        off).
    `already_alive`: agent is still alive
        (running/idling); resurrect does
        not apply.
    """

    status: ResurrectResult


class RecoverCrashMarkedResponse(BaseModel):
    """`recover-crash-marked-v2` response — the home runner's adjudication of
    harvesting a crash-marked idling corpse whose chat has stalled pending
    (the delivery watchdog's stalled-recovery request, task #3618).

    `harvested`: the row matched every settle guard and was flipped to the
        reaper's terminal shape (terminated, termination_source='reaper';
        the crash marker is kept, so the relaxed reaper trigger can resume
        the leftover work on the home machine's next beat).
    `already_terminated`: the row is already terminated — an idempotent
        repeat of a completed harvest.
    `refused`: an adjudication guard failed (fail-closed); `reason` names it
        ('not_marked', 'not_settled:<status>', 'wrong_machine',
        'lease_alive', 'wake_suppressed', or the active wake-suppression
        reason — 'permanent_provider_reject' when the recovery breaker
        halted the agent)."""

    status: CrashRecoveryResult
    reason: str | None = None


class ShellInfo(BaseModel):
    """One live persistent-shell session of an agent — a session named
    `…-agent-<id>-shell-<sid>[-<name>]`. `id` is the agent-local session id
    (the int the agent uses to drive it); `name` is the optional slug label
    (a watcher carries the conventional name "watcher"), None when unnamed.
    The agent's own process session has no `-shell-` segment and is excluded.

    `created_at` / `uptime_seconds` come from the runner's session record
    (the launch epoch, resolved to the cluster timezone); `expires_at` is
    the gateway-owned TTL deadline from `agent_shell_ttls` — None when the
    session has no TTL row (page and schedule sessions carry their own
    lifecycle)."""

    model_config = ConfigDict(frozen=True)

    id: int
    name: str | None = None
    created_at: datetime | None = None
    uptime_seconds: int = 0
    expires_at: datetime | None = None


class PageRow(BaseModel):
    """Single agent_pages row view — element of GET /api/agents/{aid}/pages +
    return of register/close endpoints.

    `url`: absolute gateway reverse-proxy URL
    (`http://<gateway>/pages/<id>-<name>/`) — the gateway serves
    the page content, so this is the only address the browser needs; the
    page server's host:port stays inside the gateway."""

    id: int
    agent_id: int
    name: str
    port: int
    title: str | None
    serve_dir: str | None
    url: str
    created_at: datetime
    closed_at: datetime | None


class SessionInfo(BaseModel):
    """A single live session entry — name, creation time, uptime seconds."""

    model_config = ConfigDict(frozen=True)

    name: str
    created_at: datetime | None = None
    uptime_seconds: int = 0


# ─── ops cluster-RPC wire ────────────────────────────────────────────────────
# The gateway <-> agent-runner control-op contract. Both ends validate against
# these instead of hand-indexing dicts: the gateway routers that call
# `dispatch_to_machine` and the agent-runner ops server's `_dispatch`. The
# request envelope carries one OpKind + its payload; the response envelope
# carries the op outcome + the per-kind result (or an OpFailure on failure).

# The op vocabulary — the discriminator the daemon's `_dispatch` switches on;
# lives here (not cluster_rpc.py) beside its models; `ops.cluster.rpc` re-exports it.
OpKind = Literal[
    "spawn-launch-v2",
    "launch-reconcile-v1",
    "lifecycle",
    "status_probe",
    "config_read",
    "config_write",
    "config_audit_read",
    "inventory_read",
    "inventory_write",
    "shell_probe",
    "shell_kill",
    "agent_skill_view",
    "shell_capture",
    "upload_receive",
    "upload-receive-v1",
]


def is_op_kind(kind: str) -> TypeGuard[OpKind]:
    """Admit the current wire vocabulary before lookup, dedupe or dispatch effects."""
    return kind in get_args(OpKind)


class OpStatus(StrEnum):
    """Completion of one machine RPC, independent of its per-kind business result."""

    COMPLETED = "completed"
    FAILED = "failed"


class OpResponse(BaseModel):
    """`POST /ops` response envelope. `result` is the per-kind result model's dict
    on 'completed', or an OpFailure dict on 'failed'."""

    status: OpStatus
    result: dict[str, Any]


class OpFailure(BaseModel):
    """The `result` of a failed op — what the gateway's
    `_raise_proxied_wire_error_from_payload` reconstructs the original wire
    exception from. `reason` is an AvaAgentError reason-enum value when the op
    raised one; `detail` its message."""

    error: str
    detail: str | None = None
    reason: str | None = None


# ── per-OpKind request payloads (spawn-launch-v2 uses LaunchAgentRequest above;
# SpawnAgentRequest is the REST body, not an op payload) ──


class LifecyclePayload(BaseModel):
    """`lifecycle` op payload: the agent lifecycle path plus the per-action
    request body, which the op validates per-action into a
    Terminate/Resurrect/Restart request. The trigger id+kind pair is internal
    to auto-resurrect: the home runner uses it as the final pending-work CAS;
    manual lifecycle calls omit both."""

    path: str
    body: dict[str, Any] = Field(default_factory=dict)
    trigger_inbound_id: int | None = Field(default=None, gt=0)
    trigger_inbound_kind: WakeTriggerKind | None = None


class ConfigWritePayload(BaseModel):
    """`config_write` op payload. `overrides` is a JSON-merge-patch over the host
    `.env` (a null value unsets a field), so its values stay open-typed.

    `actor` / `trace_id` are stamped by the dispatching gateway from verified
    request state for the write audit — deliberately not part of the caller's
    JSON contract (`gateway/http/auth/docs/request_principal.ava.okf.md`)."""

    overrides: dict[str, Any]
    local: bool = False
    actor: str | None = None
    trace_id: str | None = None


class InventoryWritePayload(BaseModel):
    """`inventory_write` op payload — the plugin + MCP enable toggles."""

    plugins: dict[str, bool]
    mcp_servers: dict[str, bool]


class ShellProbePayload(BaseModel):
    """`shell_probe` op payload: whose live persistent shells to list."""

    agent_id: int


class ShellKillPayload(BaseModel):
    """`shell_kill` op payload: one persistent session to reclaim."""

    agent_id: int
    session_id: int


class AgentSkillViewPayload(BaseModel):
    """`agent_skill_view` op payload: whose command view to build."""

    agent_id: int


class ShellCapturePayload(BaseModel):
    """`shell_capture` op payload: one session's terminal tail, captured on
    the machine the agent runs on.

    `lines` is the scrollback-capture depth (the same bound the gateway's
    local capture enforces: 50-2000, default 200)."""

    agent_id: int
    session_id: int
    lines: int = 200


class UploadReceivePayload(BaseModel):
    """`upload_receive` op payload: pull one uploaded file from the gateway
    onto this runner's local uploads dir.

    The gateway stores every upload on its own disk and serves it back at
    ``/api/agents/<id>/uploads/<name>``. An agent that runs on a REMOTE
    runner cannot read the gateway's local disk, so the gateway dispatches
    this op after saving: the runner fetches the file over HTTP (same
    cluster-secret bearer as every other runner -> gateway dial) and writes
    it into its own ``~/Downloads/AvaAgent-<id>/``. The notification message
    then carries this host's absolute path — the address the agent can act
    on directly."""

    agent_id: int
    name: str


# ── per-OpKind result models ──


class FieldWriteResult(BaseModel):
    """One field/item's write verdict: `ok`, plus a human `reason` when rejected.
    Shared by config field results and inventory plugin/MCP results. The gateway
    keeps its own frontend-facing ConfigFieldWriteResult / InventoryItemWriteResult
    (structurally identical) so the OpenAPI schema the frontend consumes is
    unchanged; this is the wire-side twin the ops functions build."""

    ok: bool
    reason: str | None = None


class HostConfigField(BaseModel):
    """One host-scope config field as `config_read` reports it — the effective
    value (sensitive ones masked upstream) plus its edit/capability flags."""

    value: object
    overridden: bool
    remote_writable: bool
    can_enable: bool | None = None
    reason: str | None = None


class ConfigAuditReadPayload(BaseModel):
    """`config_audit_read` op payload — how many recent records to read (1..200)."""

    # Read ceiling (200) mirrors the config-audit endpoint's le; callers pass
    # their own window (task #3696 exception inventory).
    last: int = Field(ge=1, le=200)


class ConfigAuditReadResult(BaseModel):
    """`config_audit_read` op result — this host's recent `.env`-write records,
    newest first (raw record shape; values were redacted at write time)."""

    machine: str
    records: list[dict[str, object]]


class ConfigReadResult(BaseModel):
    """`config_read` op result — this host's host-scope fields + its editable
    override set."""

    machine: str
    host_fields: dict[str, HostConfigField]
    raw_overrides: dict[str, object]


class ConfigWriteOpResult(BaseModel):
    """`config_write` op result — per-field verdicts + the atomic `applied` flag +
    the union of restart targets. The gateway maps this onto its frontend-facing
    ConfigWriteResult (dropping `machine`)."""

    machine: str
    results: dict[str, FieldWriteResult]
    applied: bool
    restart_required: list[str]


class InventoryReadItem(BaseModel):
    """One plugin / MCP server as `inventory_read` reports it on a single host."""

    enabled: bool
    can_enable: bool | None = None
    reason: str | None = None
    description: str


class InventoryReadResult(BaseModel):
    """`inventory_read` op result — this host's plugin + MCP enable inventory."""

    machine: str
    plugins: dict[str, InventoryReadItem]
    mcp_servers: dict[str, InventoryReadItem]


class InventoryWriteOpResult(BaseModel):
    """`inventory_write` op result — per-item plugin + MCP verdicts + the atomic
    `applied` flag. The gateway maps this onto its frontend-facing
    InventoryWriteResult (dropping `machine`)."""

    machine: str
    plugin_results: dict[str, FieldWriteResult]
    mcp_results: dict[str, FieldWriteResult]
    applied: bool


class ShellProbeResult(BaseModel):
    """`shell_probe` op result — this host's live persistent-shell sessions
    for one agent, newest id last (same shape the gateway's local probe
    returns; the gateway forwards it into the inspector panel)."""

    shells: list[ShellInfo]


class ShellKillResult(BaseModel):
    """`shell_kill` result; absent means the session already ended.

    `interrupted` is True when the killed session carried live processes (a
    running foreground/background job) at kill time — the gateway notifies
    the owner only then, so an empty shell's reaping stays silent. Absent
    sessions always report False. `name` is the shell's optional display
    name, for a notice that names what was interrupted."""

    mode: ShellKillMode
    interrupted: bool = False
    name: str | None = None


class OpsCommandItem(BaseModel):
    """One command-autocomplete item returned by an agent-runner op.

    Kept in the RPC contract rather than importing the gateway's REST schema:
    ops is the machine-side boundary and returns only the three fields the
    autocomplete consumer needs.
    """

    name: str
    description: str
    instruction_hint: str


class AgentSkillViewResult(BaseModel):
    """`agent_skill_view` result — commands visible to one agent on this host.

    ``mcp_names`` is groundwork for the phase-2 per-agent MCP view: the enabled
    MCP server names on the runner host. Nothing consumes the field yet.
    """

    commands: list[OpsCommandItem]
    mcp_names: list[str] = Field(default_factory=list)


class ShellCaptureResult(BaseModel):
    """`shell_capture` op result — one session's terminal tail captured on
    the host that runs it: the reconstructed full session name plus the
    captured lines (newline-split, trailing newline stripped).

    `created_at` / `uptime_seconds` ride along from the resolved session
    record (the launch epoch + probe-time uptime) so the shell monitor page
    can render runtime/TTL meta in its title bar without a second probe."""

    session_name: str
    lines: list[str]
    created_at: datetime | None = None
    uptime_seconds: int = 0


class UploadReceiveResult(BaseModel):
    """`upload_receive` op result — the local absolute path the file was
    written to on this runner (``~/Downloads/AvaAgent-<id>/<name>``), the
    address the agent notification message carries so a remote agent can
    read the file off its own disk."""

    path: str


class AgentSessionGroup(BaseModel):
    """One agent's shell/watcher sessions, grouped for the status page. The
    agent process itself is native, so a group forms only for an agent with at
    least one shell; `label` is resolved later (empty at grouping time)."""

    agent_id: int
    label: str
    shells: list[SessionInfo]
