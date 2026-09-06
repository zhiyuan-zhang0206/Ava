"""Gateway <-> agent-runner RPC-shared schemas — the cross-process wire
contract. These types are produced/consumed on BOTH sides of the ops RPC
(the gateway HTTP surface AND the agent-runner ops handlers in ops/ +
services/), so by the import layering (shared < ops < gateway) they live in
the ops layer: gateway imports them downward, and ops/services never have to
reach up into gateway. Split out of the former monolithic ops/schemas.py.
"""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from shared.envelope import reject_unnegotiated_caller, validate_source, validate_writable_source

_MAX_CONTENT_CHARS = 1_000_000
"""Prompt/reply content needs a memory-abuse guardrail, not a 64 KiB wire contract.

The model provider context window is the downstream input bound; one million
characters is roughly 1 MiB, leaving legitimate handoffs and reports intact
while failing fast on abusive request bodies.
"""

_UserContent = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=_MAX_CONTENT_CHARS),
]


class AgentExitedRequest(BaseModel):
    """The actual admitted runtime reporting exit, never a freshly read token."""

    model_config = ConfigDict(extra="forbid")
    generation: UUID | None = None
    owner: UUID | None = None

    @model_validator(mode="after")
    def require_complete_incarnation(self) -> "AgentExitedRequest":
        """The legacy SDK serializes its empty body as {}; partial tokens are invalid."""
        if (self.generation is None) != (self.owner is None):
            raise ValueError("generation and owner must be supplied together")
        return self


class TextContentBlock(BaseModel):
    """Text part of a multimodal chat message (OpenAI content-block shape)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["text"]
    text: str


class ImageUrlRef(BaseModel):
    """The `image_url` object of an image content block."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2048)


class ImageUrlContentBlock(BaseModel):
    """Image part of a multimodal chat message (OpenAI content-block shape).

    `image_url.url` must reference an upload of the target agent
    (`/api/agents/{id}/uploads/<name>`); the message endpoint validates the
    ownership + that the file still exists on disk and 422s otherwise. Only a
    reference is carried on the wire, never base64 — the claim node inlines the
    bytes as native model content at delivery time.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["image_url"]
    image_url: ImageUrlRef


ContentBlock = Annotated[
    TextContentBlock | ImageUrlContentBlock,
    Field(discriminator="type"),
]


_MessageContent = (
    Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1),
    ]
    | Annotated[list[ContentBlock], Field(min_length=1)]
)


class CancelRequested(BaseModel):
    """POST /api/cancel response.

    `enqueued`: a durable kind='cancel' inbound was INSERTed. The in-flight
        llm/exec node interrupts on it if one is running; otherwise the next
        claim pass halts the agent to idle. The process stays alive.
    `already_terminated`: agent is dead — nothing to pause."""

    status: Literal["enqueued", "already_terminated"]


class SpawnAgentRequest(BaseModel):
    """POST /api/agents request body — for frontend / SDK; frontend does
    not need to know checkpoint ids.

    If `fork_from` is given, the gateway resolves the latest checkpoint
    internally and passes an explicit id to the underlying create_agent_row
    (consistent with SDK `ava.agents.spawn(fork_from=N)` logic).

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
    `or default` forms CLAUDE.md prohibits); both kinds of callers must
    explicitly identify themselves.
    """

    prompt: _UserContent | None = None
    spawner: str = Field(default="user", min_length=1, max_length=64)
    fork_from: int | None = Field(default=None, gt=0)
    prompt_source: str | None = Field(default=None, min_length=1, max_length=64)
    # Target physical host (multi-machine placement); None = local. The gateway creates the row (create_agent_row) and forwards a launch op.
    machine: str | None = Field(default=None, min_length=1, max_length=64)
    # Per-agent config overlay (currently {"llm_model": ...}); only per_agent=True
    # fields are accepted (enforced at agent boot via apply_config_overlay).
    config: dict[str, object] | None = Field(default=None)
    # Optional named config preset. When set, the gateway seeds config from the
    # preset's stored overlay, then lets `config` above win per-key. Resolved and
    # merged into `config` at the spawn boundary (post_agents), so the spawn op on
    # the runner never sees this field set.
    preset: str | None = Field(default=None, max_length=64)
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
        # shared.envelope.validate_source.
        if self.prompt_source is not None:
            validate_writable_source(self.prompt_source)
        return self


class LaunchAgentRequest(BaseModel):
    """The cross-machine LAUNCH op payload (Task #1236 follow-up).

    The gateway creates the agent row (agents + agents_meta, main role) and
    forwards only the launch to the target runner — the runner's ops server
    runs as the least-privilege `ava_runner` role, which by design cannot
    INSERT agents. `config` / `birth_config` are the per-agent overlay the
    child replays (carried in the child env, never argv); `prompt` /
    `prompt_source` / `label` are the plain-spawn first-prompt delivery
    (post-launch, runner-side — inbound INSERT is within the runner role).
    """

    agent_id: int
    config: dict[str, object] | None = None
    birth_config: dict[str, object] | None = None
    prompt: str | None = None
    prompt_source: str | None = None
    label: str | None = None

    @field_validator("prompt_source")
    @classmethod
    def _check_prompt_source(cls, value: str | None) -> str | None:
        if value is not None:
            validate_writable_source(value)
        return value


class SpawnedAgent(BaseModel):
    """POST /api/agents response — new agent_id (== agent_id)."""

    id: int


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

    The value must pass `shared.envelope.validate_source` (same check as
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
    prompt: _UserContent | None = None

    @field_validator("resurrected_by")
    @classmethod
    def _check_envelope_source(cls, v: str) -> str:
        validate_writable_source(v)
        return v


class AgentMessageIn(BaseModel):
    """POST /api/agents/{id}/messages request body — for SDK send_message and
    the `ava agents send` CLI (which the generated background-run / watcher
    completion notices invoke).

    Pure INSERT + return — the caller does not inspect status.
    Auto-resurrect on the gateway side ensures delivery to any agent,
    terminated or not.

    `source` is required — the SDK passes f"agent:{my_id}", the generated
    notices pass shell:N / watcher:N; there is no default to prevent callers
    from forgetting and having inbounds silently tagged as "user", muddying
    envelope labels. The valid set is in `shared/envelope.py:validate_source`
    (system / agent:N / user / ui:page:<name> / watcher:N / shell:N /
    schedule:N); an illegal source is intercepted by 422 at the HTTP layer —
    otherwise it would land in inbound_messages and the agent claim node
    would hit ValueError and kill the process.
    """

    content: _MessageContent
    source: str = Field(min_length=1, max_length=64)

    @field_validator("source")
    @classmethod
    def _check_envelope_source(cls, v: str) -> str:
        # Chat admission locks the actual target runtime in its INSERT transaction.
        # Lifecycle/bootstrap sources remain blanket-fenced until their handoff exists.
        validate_source(v)
        return v

    @model_validator(mode="after")
    def _non_empty_multimodal(self) -> "AgentMessageIn":
        # A block list must carry actual content: at least one image, or at
        # least one text block with non-whitespace text. Rejecting the empty
        # case here keeps the "whitespace-only 422s" contract the str path
        # already has (StringConstraints min_length=1), extended to blocks.
        if isinstance(self.content, list):
            has_image = any(isinstance(b, ImageUrlContentBlock) for b in self.content)
            has_text = any(isinstance(b, TextContentBlock) and b.text.strip() for b in self.content)
            if not has_image and not has_text:
                raise ValueError("content blocks must include an image or non-empty text")
        return self


class TerminateAgentRequest(BaseModel):
    """POST /api/agents/{id}/terminate request body — fully optional.

    `force` defaults to False for graceful termination. True directly kills the
    detached process and force-updates status when the agent cannot reach claim.

    `source` defaults to "user"; SDK paths pass f"agent:{my_id}". Claim
    includes this source in the lifecycle marker shown to the agent.

    `message`, when present, is queued as chat immediately before the terminate
    inbound. Lifecycle acceptance claims only the terminate row, so the chat
    remains pending for the next resurrection without another LLM turn.
    """

    force: bool = Field(default=False)
    source: str = Field(default="user", min_length=1, max_length=64)
    message: _UserContent | None = None

    @field_validator("source")
    @classmethod
    def _check_source(cls, value: str) -> str:
        # Lifecycle audit reasons include opaque legacy values such as
        # machine-pause; they are not chat envelope source identifiers.
        reject_unnegotiated_caller(value)
        return value

    @model_validator(mode="after")
    def _validate_message_source(self) -> "TerminateAgentRequest":
        if self.message is not None:
            validate_writable_source(self.source)
        return self


class TerminateAgentResponse(BaseModel):
    """POST /api/agents/{id}/terminate response.

    `enqueued`: termination accepted, including hosted force. Actual work may
        still be draining; this result does not prove exit.
    `already_terminated`: agent was already dead. Graceful termination is a
        no-op. Hosted force instead returns enqueued until its exact original
        host can prove quiescence; metadata status alone is not exit evidence.
    `force_killed`: force=true killed the agent's detached process + force
        marked terminated — agent may have been stuck and never took
        the graceful path.
    """

    status: Literal["enqueued", "already_terminated", "force_killed"]


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
        from shared.plugin_config_registry import InvalidConfigOverlay, validate_config_overlay

        try:
            validate_config_overlay(self.config_overlay)
        except InvalidConfigOverlay as exc:
            # Pydantic converts ValueError into a boundary validation failure;
            # propagating InvalidConfigOverlay directly would become a 500.
            raise ValueError(str(exc)) from exc
        return self


class RestartAgentResponse(BaseModel):
    """POST /api/agents/{id}/restart response.

    `enqueued`: restart inbound INSERTed; agent exits after the current
        turn + restarter daemon auto-respawns a fresh process attached
        to the same agent_id.
    `already_terminated`: agent is dead; restart does not apply — use
        resurrect.
    """

    status: Literal["enqueued", "already_terminated"]


class ResurrectAgentResponse(BaseModel):
    """Resurrect agent response — returned by `POST /api/agents/{id}/resurrect`
    (the frontend resurrect button) and the internal auto-resurrect op.

    `spawned`: agent was dead; UPDATEd 'terminated' -> 'idling' +
        started a fresh process attached to the same agent_id
        (LangGraph state preserved; agent wakes up from where it left
        off).
    `already_alive`: agent is still alive
        (running/idling/restarting); resurrect does
        not apply.
    """

    status: Literal["spawned", "already_alive"]


class ShellInfo(BaseModel):
    """One live persistent-shell session of an agent — a session named
    `…-agent-<id>-shell-<sid>[-<name>]`. `id` is the agent-local session id
    (the int the agent uses to drive it); `name` is the optional slug label
    (a watcher carries the conventional name "watcher"), None when unnamed.
    The agent's own process session has no `-shell-` segment and is excluded.

    `created_at` / `uptime_seconds` come from the runner's session record
    (the launch epoch, resolved to the cluster timezone); `expires_at` is
    the gateway-owned TTL deadline from `agent_shell_ttls` — None when the
    session has no TTL (watcher sessions and legacy pre-TTL shells)."""

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

# The op vocabulary — the discriminator the daemon's `_dispatch` switches on.
# Lives here (not cluster_rpc.py) so it sits beside the wire models it keys;
# `ops.cluster_rpc` re-exports it for its existing importers.
OpKind = Literal[
    "spawn-launch",
    "lifecycle",
    "cluster_stop",
    "cluster_update",
    "cluster_resume",
    "status_probe",
    "config_read",
    "config_write",
    "inventory_read",
    "inventory_write",
    "cluster_fetch",
    "shell_probe",
    "shell_kill",
    "agent_skill_view",
    "shell_capture",
    "upload_receive",
]


class OpEnvelope(BaseModel):
    """`POST /ops` request envelope. `kind` stays a bare str (not the OpKind
    Literal) so an unknown kind from a version-skewed peer becomes a 'failed' op
    result — the dispatch switch owns the kind vocabulary — rather than an
    envelope-parse rejection.

    `idempotency_key` is the caller-supplied dedup key for non-idempotent ops
    (spawn / cluster_update / lifecycle): every retry of one logical op carries
    the SAME key, and the ops server replays the first run's stored outcome
    instead of re-executing (services/agent_ops/daemon.py:_dispatch_idempotent),
    so a lost response cannot duplicate the effect. Absent (None) for
    idempotent ops and for version-skewed old callers — no dedup then."""

    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=128)


class OpResponse(BaseModel):
    """`POST /ops` response envelope. `result` is the per-kind result model's dict
    on 'completed', or an OpFailure dict on 'failed'."""

    status: Literal["completed", "failed"]
    result: dict[str, Any]


class OpFailure(BaseModel):
    """The `result` of a failed op — what the gateway's
    `_raise_proxied_wire_error_from_payload` reconstructs the original wire
    exception from. `reason` is an AvaAgentError reason-enum value when the op
    raised one; `detail` its message."""

    error: str
    detail: str | None = None
    reason: str | None = None


# ── per-OpKind request payloads (spawn-launch uses LaunchAgentRequest above;
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
    trigger_inbound_kind: Literal["chat", "compact_request", "system_note"] | None = None


class ClusterUpdatePayload(BaseModel):
    """`cluster_update` op payload."""

    restart_only: bool = False
    target_sha: str | None = None
    mode: str = "smooth"
    force_reap: bool = False


class ClusterTransitionPayload(BaseModel):
    """Exact deploy-lease capability for one stop/resume generation.

    Both fields are required. A delayed request from generation A must not be
    authorized by whichever generation happens to own the lease when it lands.
    """

    model_config = ConfigDict(extra="forbid")

    deploy_holder: str = Field(min_length=1)
    deploy_acquired_at: datetime

    @field_validator("deploy_acquired_at", mode="before")
    @classmethod
    def _require_rfc3339_offset(cls, value: object) -> object:
        if not isinstance(value, str):
            # Pydantic v2 converts ValueError into a 422 ValidationError; a
            # TypeError escapes validation and would turn malformed JSON into 500.
            raise ValueError(  # noqa: TRY004 — Pydantic validation contract
                "deploy_acquired_at must be an RFC3339 string"
            )
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("deploy_acquired_at must carry a timezone offset")
        return value


class ConfigWritePayload(BaseModel):
    """`config_write` op payload. `overrides` is a JSON-merge-patch over the host
    `.env` (a null value unsets a field), so its values stay open-typed."""

    overrides: dict[str, Any]
    local: bool = False


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


class ClusterSpawnSession(BaseModel):
    """`cluster_update` op result — the detached updater session's name +
    its tee'd log path (the shape `spawn_update` / `spawn_restart` return)."""

    session: str
    log: str


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

    mode: Literal["killed", "absent"]
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
