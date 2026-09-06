// Frontend type surface — HTTP wire schemas (Pydantic models) are derived from
// types-generated.ts; operator-facing projections are explicit overlays. The SSE event union (an Annotated
// discriminator union that OpenAPI can't fully express) stays hand-written,
// kept in sync via the task #11 wire-format roundtrip tests.
//
// Edit a backend Pydantic model → `./scripts/codegen-types.sh` regenerates
// types-generated.ts → consumers don't need to change import paths to
// pick up the new schema (re-export names here are stable). The
// pre-commit hook enforces codegen ↔ schema sync; drift fails the commit.

import type { components } from "./types-generated";

// --- HTTP schemas (gateway/schemas.py) — re-export from generated ---

type Schemas = components["schemas"];

export type CompactEnqueued = Schemas["CompactEnqueued"];
// CompactMode is the CompactEnqueued.mode field — OpenAPI flattens Literal
// to string + enum, and openapi-typescript turns it into a union literal.
// Pluck it directly as a top-level type.
export type CompactMode = CompactEnqueued["mode"];
export type CancelRequested = Schemas["CancelRequested"];

/** The persisted lifecycle vocabulary carried on the gateway wire. These are
 *  control-plane states, not the status vocabulary the console presents. */
export type WireAgentStatus = Schemas["AgentStatus"];
export type WireAgentRow = Schemas["AgentRow"];
export type WireAgentSummary = Schemas["AgentSummary"];

/** The console's complete, user-facing agent status model. Liveness remains a
 *  separate `AgentRow.liveness_state` axis, so an internally restarting agent
 *  whose runner is unreachable still renders as `idling` + `offline`, rather
 *  than leaking a control-plane transition or hiding the outage. */
export type PublicAgentStatus = Extract<
  WireAgentStatus,
  "running" | "idling" | "terminated"
>;
export type AgentRow = Omit<WireAgentSummary, "status"> & {
  readonly status: PublicAgentStatus;
};

/** Collapse every known wire lifecycle state into the public three-state model.
 *  The switch is deliberately exhaustive: adding a backend enum member fails
 *  type-checking here, and an unknown runtime value throws instead of silently
 *  inventing a display fallback. */
export function projectAgentStatusValue(status: WireAgentStatus): PublicAgentStatus {
  switch (status) {
    case "running":
      return "running";
    case "terminated":
      return "terminated";
    case "idling":
    case "restarting":
      return "idling";
    default: {
      const unknownStatus: never = status;
      throw new Error(`unknown internal agent status: ${String(unknownStatus)}`);
    }
  }
}

/** Project one raw gateway/SSE row before it enters any frontend cache. */
export function projectAgentStatus(row: WireAgentSummary): AgentRow {
  const status = projectAgentStatusValue(row.status);
  if (
    status === row.status &&
    !("fork_source_checkpoint_id" in row) &&
    !("last_probe_at" in row)
  ) {
    return row as AgentRow;
  }
  return {
    agent_id: row.agent_id,
    spawner: row.spawner,
    fork_source_agent_id: row.fork_source_agent_id,
    status,
    pid: row.pid,
    spawned_at: row.spawned_at,
    started_at: row.started_at,
    last_active_at: row.last_active_at,
    last_inbound_at: row.last_inbound_at,
    label: row.label,
    machine: row.machine,
    supports_vision: row.supports_vision,
    liveness_state: row.liveness_state,
    observation: row.observation,
    notices_awaiting_response: row.notices_awaiting_response,
    unread_notice_count: row.unread_notice_count,
    heartbeat_paused_until: row.heartbeat_paused_until,
  };
}
// OpenNotice rides the agent snapshot (notices_awaiting_response — the open
// require_response worklist). NoticeItem is the standalone feed element (the FYI
// queue + the resolved history). ResolveNoticeIn is the resolve request body.
export type OpenNotice = Schemas["OpenNotice"];
export type NoticeItem = Schemas["NoticeItem"];
// The unified inbox feed (Task #1024, R4 layer 2, Q1=A): open (FYI) +
// awaiting (require_response) + one keyset page of the resolved history.
export type NoticesFeed = Schemas["NoticesFeed"];
export type ResolveNoticeIn = Schemas["ResolveNoticeIn"];
export type AgentMessageEnqueued = Schemas["AgentMessageEnqueued"];
export type PendingInbound = Schemas["PendingInbound"];


export type StatsDashboard = Schemas["StatsDashboard"];
export type StatsTokens = Schemas["StatsTokens"];

// --- Metrics (settings Metrics tab) ---
//
// The envelope (meta + the `metrics` map) comes from the generated schema,
// but `metrics` is `Record<string, unknown>` there by design: the backend
// types it as a free-form dict so adding a `@metric` unit is one function with
// no schema churn. The per-unit data shapes below mirror the `data` dicts the
// units in shared/metrics.py emit; consumers narrow `report.metrics[key]` to
// the matching interface. Adding a unit that reuses these shapes needs no TS
// change beyond a new key access.
export type AlertsWindow = "1h" | "6h" | "24h" | "7d";

// --- Alerts (the system→human alert store, Task #1224) ---
//
// Generated re-exports of the gateway's Pydantic models (AlertRow /
// AlertsListMeta / AlertsListResponse) — the pre-commit types-codegen-fresh
// gate fails on any drift, so the alert types can never silently diverge
// from the backend. Alert is fully separate from Notice.
export type Alert = Schemas["AlertRow"];
export type AlertsResponse = Schemas["AlertsListResponse"];
export type AlertSeverity = Alert["severity"];
export type AlertStatus = Alert["status"];


// SpawnAgentRequest: backend `spawner: str = Field(default="user")` is
// emitted in OpenAPI as "required + default" (Pydantic default behavior),
// so openapi-typescript marks it required. But the frontend's default
// calls never pass spawner — we override it to optional to match runtime
// behavior (the server falls back to default "user" when the field is missing).
//
// `prompt_source` is `str | None = None` + model_validator enforcing
// "must be present when prompt is given" on the server — so the
// generated type is already nullable optional; inherit it directly with
// no override.
export type SpawnAgentRequest = Omit<Schemas["SpawnAgentRequest"], "spawner"> & {
  readonly spawner?: string;
};
export type SpawnedAgent = Schemas["SpawnedAgent"];

export type ModelsResponse = Schemas["ModelsResponse"];
export type DefaultModelView = Schemas["DefaultModelView"];

export type MemoryGraphNode = Schemas["MemoryGraphNode"];
export type MemoryGraphEdge = Schemas["MemoryGraphEdge"];
export type MemoryGraphResponse = Schemas["MemoryGraphResponse"];
export type MemoryNoteResponse = Schemas["MemoryNoteResponse"];

export type TerminateAgentResponse = Schemas["TerminateAgentResponse"];
export type RestartAgentResponse = Schemas["RestartAgentResponse"];
export type ResurrectAgentResponse = Schemas["ResurrectAgentResponse"];

export type TokenUsageResponse = Schemas["TokenUsageResponse"];

export type ContextBreakdownResponse = Schemas["ContextBreakdownResponse"];
export type ContextCategory = Schemas["ContextCategory"];
export type ContextSection = Schemas["ContextSection"];

// --- Run timeline (GET /api/agents/{id}/run-timeline) ---

export type RunTimelineResponse = Schemas["RunTimelineResponse"];

// --- Per-agent inspector panel (GET /api/agents/{id}/inspect) ---

export type AgentInspect = Schemas["AgentInspect"];
export type AgentInspectLive = Schemas["AgentInspectLive"];
export type ShellInfo = Schemas["ShellInfo"];
export type ShellCapture = Schemas["ShellCaptureResponse"];
export type AgentCost = Schemas["AgentCost"];
export type AgentStats = Schemas["AgentStats"];
export type AgentTps = Schemas["AgentTps"];
export type HeartbeatInfo = Schemas["HeartbeatInfo"];
export type HeartbeatLastPause = Schemas["HeartbeatLastPause"];

// --- Shell monitor page (GET /api/agents/{id}/shell/{sid}) ---
//
// --- ava.ui.show panel ---

export type PageRow = Schemas["PageRow"];
export type PageRegisterRequest = Schemas["PageRegisterRequest"];

// --- Each item from GET /api/agents/{id}/timeline ---
//
// `BackendTimelineItem` is based on `Schemas["TimelineItem"]`:
// - adds a frontend-only `partial` field — the backend doesn't send it;
//   the reducer adds it locally when a `*_delta` arrives before a
//   *_start (a fallback for users joining mid-stream and missing the
//   start). The renderer uses it to add a visual hint (ellipsis + italic).
// - adds a frontend-only `interrupted` field — when SSE disconnects
//   (network flap / gateway restart / server error), still-streaming
//   items are marked interrupted to distinguish "the message simply
//   ends here" from "streaming interrupted, content may be incomplete".
//   The reducer applies it to all partial items on
//   processConnectionEvent("closed").
// - narrows `source` / `created_at` / `inbound_id` from `T | null |
//   undefined` to `T | null` — Pydantic emits every field by default
//   (default None serializes to null), so they never get omitted on
//   the wire; undefined doesn't appear at runtime. This override aligns
//   the TS type with runtime so callers don't need defensive `??` fallbacks.
export type BackendTimelineItem = Omit<
  Schemas["TimelineItem"],
  "source" | "created_at" | "inbound_id"
> & {
  readonly source: string | null;
  readonly created_at: string | null;
  readonly inbound_id: number | null;
  readonly partial?: boolean;
  readonly interrupted?: boolean;
  // Frontend-only: Date.now() stamped when a reasoning item is first created
  // from a reasoning_start/delta SSE event. The thinking toggle chip ticks
  // "Thinking for Xs" off it while the block streams; on commit the snapshot
  // replaces the item with the backend version carrying the authoritative
  // reasoning_ms, so this is only read pre-commit. Absent on committed items.
  readonly reasoningStartedAt?: number;
  // Frontend-only: the block's elapsed (Date.now() − reasoningStartedAt) frozen
  // the moment its live clock stops — when a later block starts or the turn
  // ends (timeline.freezeReasoningClocks). The chip shows this frozen "Thought
  // for Xs" in the window between the clock stopping and the snapshot
  // committing the authoritative backend reasoning_ms (which then replaces the
  // whole item, dropping this stamp). Absent on committed items.
  readonly reasoningElapsedMs?: number;
  // Frontend-only: Date.now() stamped when an agent_code item is first
  // created from a code_start SSE event. The code toggle chip ticks
  // "writing code for Xs" off it while the code streams; on commit the
  // snapshot replaces the item with the backend version (no committed
  // code_ms yet), clearing this stamp so the live clock stops.
  readonly codeStartedAt?: number;
  /** Frontend-frozen elapsed while the agent was writing code; cleared when the
   *  backend commits code_ms. Set by clearCodeClocks when code writing finishes. */
  readonly codeElapsedMs?: number;
  // Frontend-only: Date.now() stamped when a code_output item is first
  // created from an exec_start / exec_output_chunk SSE event. The output
  // toggle chip ticks "Running for Xs" off it while the execution streams;
  // on commit the snapshot replaces the item with the backend version
  // carrying the authoritative exec_ms, so this is only read pre-commit.
  readonly execStartedAt?: number;
};


// GET /api/agents/{id}/timeline response: one tail window of rendered items +
// the authoritative `msg_count` (len(state.messages)) + `has_more`. msg_count
// is sent by the server rather than inferred from max rendered msg_idx, so the
// streaming future-partial merge uses the exact boundary even if a trailing
// message renders to nothing. `has_more` reports whether older items exist
// before this window (drives scroll-up load).
export interface TimelineResponse {
  readonly items: BackendTimelineItem[];
  readonly msg_count: number;
  readonly has_more: boolean;
}

// --- SystemEvent (events.py) — hand-written mirror ---
//
// All events flow through a single SSE channel `/api/agents/{id}/system`.
// OpenAPI can't precisely express a Pydantic Annotated discriminator
// union (it collapses to anyOf without a narrowing discriminator), so
// openapi-typescript's output is unusable — hand-written + kept in sync
// via the task #11 Python ↔ TS roundtrip equivalence tests (the
// backend adding a field to events.py forces a fixture update; the
// frontend failing to parse it fails the test and catches drift).
//
// readonly fields mirror the Python `_Base(BaseModel, frozen=True)` —
// consumers (useEventStream → reducer) must not mutate, blocked at
// compile time.

interface BaseEvent {
  readonly agent_id: number;
}

export interface ChatStartEvent extends BaseEvent {
  readonly role: "chat_start";
  readonly item_id: string;
}
export interface ChatDeltaEvent extends BaseEvent {
  readonly role: "chat_delta";
  readonly item_id: string;
  readonly content: string;
}
export interface CompactRequestEvent extends BaseEvent {
  readonly role: "compact_request";
  readonly content: string;
}
export interface CompactDoneEvent extends BaseEvent {
  readonly role: "compact_done";
}
export interface CodeStartEvent extends BaseEvent {
  readonly role: "code_start";
  readonly item_id: string;
}
export interface CodeDeltaEvent extends BaseEvent {
  readonly role: "code_delta";
  readonly item_id: string;
  readonly content: string;
}
export interface ReasoningStartEvent extends BaseEvent {
  readonly role: "reasoning_start";
  readonly item_id: string;
}
export interface ReasoningDeltaEvent extends BaseEvent {
  readonly role: "reasoning_delta";
  readonly item_id: string;
  readonly content: string;
}
export interface ExecStartEvent extends BaseEvent {
  readonly role: "exec_start";
  /** Stable item_id shared with ExecOutputChunk / ExecOutput — the frontend
   *  creates a code_output placeholder on exec_start so the output block
   *  appears immediately, before any chunk arrives. */
  readonly item_id: string;
}
export interface ExecOutputChunkEvent extends BaseEvent {
  readonly role: "exec_output_chunk";
  readonly item_id: string;
  readonly content: string;
  readonly keepalive?: boolean;
}
export interface ExecOutputEvent extends BaseEvent {
  readonly role: "exec_output";
  readonly item_id: string;
  readonly content: string;
}
export interface ErrorEvent extends BaseEvent {
  readonly role: "error";
  readonly content: string;
  readonly error_class?: string | null;
  readonly provider?: string | null;
  readonly status?: number | null;
  readonly reason?: string | null;
  readonly blocked?: boolean;
  readonly recovery?: string | null;
}
export interface CancelledEvent extends BaseEvent {
  readonly role: "cancelled";
}
export interface InboundArrivedEvent extends BaseEvent {
  readonly role: "inbound_arrived";
  readonly inbound_id: number;
  readonly kind: string;
  readonly source: string;
  readonly content: string;
}
export interface TokenUsageEvent extends BaseEvent {
  readonly role: "token_usage";
  readonly input_tokens: number;
  readonly output_tokens: number;
  readonly reasoning_tokens?: number;
}
export interface LLMDoneEvent extends BaseEvent {
  readonly role: "llm_done";
}
export interface InboundCommittedEvent extends BaseEvent {
  readonly role: "inbound_committed";
  readonly inbound_id: number;
}
export interface LabelUpdatedEvent extends BaseEvent {
  readonly role: "label_updated";
  readonly label: string | null;
}
export interface PageOpenedEvent extends BaseEvent {
  readonly role: "page_opened";
  readonly page_id: number;
  readonly name: string;
  readonly port: number;
  readonly title: string | null;
  readonly url: string;
}
export interface PageClosedEvent extends BaseEvent {
  readonly role: "page_closed";
  readonly name: string;
}


export interface TimelineSnapshotEvent extends BaseEvent {
  readonly role: "timeline_snapshot";
  readonly items: readonly BackendTimelineItem[];
  /** Length of LangGraph state.messages at snapshot time — the frontend
   * uses partial.msg_idx == msg_count to detect a future single position. */
  readonly msg_count: number;
}

// AgentSnapshot is structurally identical to WireAgentRow (the HTTP schema)
// — see shared/agent_snapshot.py for the canonical Python definition.
// It is projected by the agents fold before entering the public AgentRow cache.
export type AgentSnapshot = WireAgentRow;

export interface AgentSpawnedEvent extends BaseEvent {
  readonly role: "agent_spawned";
  readonly snapshot: AgentSnapshot;
}
export interface AgentUpdatedEvent extends BaseEvent {
  readonly role: "agent_updated";
  readonly snapshot: AgentSnapshot;
}
export interface NoticePostedEvent extends BaseEvent {
  readonly role: "notice_posted";
  readonly notice_id: number;
  readonly priority: string;
  readonly title: string;
  /** The task this notice belongs to, or null — lets the FYI feed group by task. */
  readonly task_id: number | null;
}
export interface NoticeResolvedEvent extends BaseEvent {
  readonly role: "notice_resolved";
  readonly notice_id: number;
}
export interface TaskCreatedEvent extends BaseEvent {
  readonly role: "task_created";
  readonly task_id: number;
}
export interface TaskUpdatedEvent extends BaseEvent {
  readonly role: "task_updated";
  readonly task_id: number;
}
export interface ClusterUpdateStartedEvent extends BaseEvent {
  readonly role: "cluster_update_started";
  readonly kind: "rollout" | "restart";
  readonly origin: string;
}

export type SystemEvent =
  | ChatStartEvent
  | ChatDeltaEvent
  | CompactRequestEvent
  | CompactDoneEvent
  | CodeStartEvent
  | CodeDeltaEvent
  | ReasoningStartEvent
  | ReasoningDeltaEvent
  | ExecStartEvent
  | ExecOutputChunkEvent
  | ExecOutputEvent
  | ErrorEvent
  | CancelledEvent
  | InboundArrivedEvent
  | InboundCommittedEvent
  | LabelUpdatedEvent
  | PageOpenedEvent
  | PageClosedEvent
  | TokenUsageEvent
  | LLMDoneEvent
  | TimelineSnapshotEvent
  | AgentSpawnedEvent
  | AgentUpdatedEvent
  | NoticePostedEvent
  | NoticeResolvedEvent
  | TaskCreatedEvent
  | TaskUpdatedEvent
  | ClusterUpdateStartedEvent;



// --- Config (runtime config panel) ---
//
// `ConfigFieldView` is the per-field view returned by GET /api/config;
// `ConfigView` is the top-level schema (field list + raw_overrides).
// Same pattern as ExtensionsView: raw_overrides is what the frontend
// uses for delta updates.
//
// Hand-written mirror of the generated ConfigFieldView (types-generated.ts).
// The generated type carries `scope`; kept here so consumers import from
// the stable `@/lib/types` path without touching the generated layer directly.

export interface ConfigFieldView {
  readonly name: string;
  readonly field_type: "bool" | "string" | "int" | "float" | "enum";
  readonly current_value: boolean | string | number | null;
  readonly default_value: boolean | string | number | null;
  readonly description: string;
  readonly group: string; // owning-domain label (e.g. "LLM", "Data plane") — the second-level bucket
  // Owning machine capability — the top-level config-panel section. "common" =
  // not owned by a single capability (cluster-wide policy or shared host identity).
  readonly capability: "gateway" | "agent-runner" | "common";
  readonly scope: "cluster-pinned" | "cluster-default" | "host" | "agent";
  readonly restart_required: "agent" | "ops" | "gateway" | "all" | "";
  // Whether a spawn/restart config overlay may override this field per agent —
  // drives the panel's "per-agent" tag and filter.
  readonly per_agent: boolean;
  readonly writable: boolean;
  readonly sensitive: boolean;
  readonly env_var: string;
  readonly remote_writable: boolean;
  // For an "enum" field, the allowed values rendered as a select; null for
  // every other field_type.
  readonly choices?: readonly string[] | null;
  // Host-side read-time capability hint; null for non-host fields and for
  // host fields with no static pre-grey gate. `reason` explains a false
  // `can_enable` (e.g. browser_enabled -> "no display detected on <host>").
  readonly can_enable: boolean | null;
  readonly reason: string | null;
}

export interface ConfigView {
  readonly fields: ConfigFieldView[];
  readonly raw_overrides: Record<string, unknown>;
  // The target machine's capability set: the gateway's own on the Cluster (self)
  // view, the ?machine= host's on a remote view. A subset of {gateway,
  // agent-runner} (never "common", a config bucket not a machine capability).
  // Drives which capability sections a remote view renders.
  readonly machine_capabilities: readonly ("gateway" | "agent-runner")[];
}

// PUT /api/config result — per-field verdicts + whether the write committed.
// Re-exported from the generated layer so the config page imports from the
// stable `@/lib/types` path.
export type ConfigFieldWriteResult = Schemas["ConfigFieldWriteResult"];
export type ConfigWriteResult = Schemas["ConfigWriteResult"];

// GET /api/config/resolved?model= — every per-model-defaultable setting resolved
// for one model, each row naming the layer its effective value came from
// (shared default < per-model default < explicit .env value). Read-only: a row's
// `name` is the same key GET/PUT /api/config uses, so the per-model view links
// back to that field's existing editor instead of carrying a write path.
export type ResolvedFieldView = Schemas["ResolvedFieldView"];
export type ResolvedConfigView = Schemas["ResolvedConfigView"];

// GET /api/cluster/machines row — name + free-text description + live status.
// Drives the config-page machine selector.
export type AgentMachineRow = Schemas["AgentMachineRow"];

// --- Inventory (Plugins & MCP, cross-machine matrix) ---
//
// GET /api/inventory -> InventoryAggregate (the column = machine, row =
// plugin/MCP-server matrix). Per-host cell state is InventoryItemHostState.
// PUT /api/inventory?machine= returns InventoryWriteResult (per-item ok/reason
// + atomic `applied`). Re-exported from the generated layer so the inventory
// page imports from the stable `@/lib/types` path.
export type InventoryAggregate = Schemas["InventoryAggregate"];
export type InventoryItemAggregate = Schemas["InventoryItemAggregate"];
export type InventoryItemHostState = Schemas["InventoryItemHostState"];
export type InventoryWriteResult = Schemas["InventoryWriteResult"];
export type InventoryItemWriteResult = Schemas["InventoryItemWriteResult"];

// GET /api/skills -> SkillsView: this gateway host's skills load dir correlated
// with the install registry (name / source layer / enabled / local drift).
export type SkillsView = Schemas["SkillsView"];
export type SkillView = Schemas["SkillView"];
export type SkillLayer = SkillView["layer"];


// --- System Status (GET /api/status) ---

// SystemStatus family — the /api/status set, re-exported from the
// generated layer to stay in sync with the backend.
export type ServiceItem = Schemas["ServiceItem"];
export type ServicesStatus = Schemas["ServicesStatus"];
export type MachineStatus = Schemas["MachineStatus"];
export type ResourceSample = Schemas["ResourceSample"];
export type ClusterPanel = Schemas["ClusterPanel"];
export type ClusterStatus = Schemas["ClusterStatus"];
export type ClusterUpdateCheck = Schemas["UpdateCheck"];
export type SystemStatus = Schemas["SystemStatus"];

// --- Plugin console contributions (GET /api/ui/contributions) ---
//
// What the cluster's enabled plugins declare under `contributions.ui` in their
// ava-plugin.json. Declarations are data the console's own components render —
// no plugin JavaScript ever enters this bundle.

export type UiThemeContribution = Schemas["UiThemeContribution"];
export type UiNavContribution = Schemas["UiNavContribution"];
export type UiContributionsResponse = Schemas["UiContributionsResponse"];

// --- File Upload ---

export type UploadedFile = Schemas["UploadedFile"];
export type UploadedBatch = Schemas["UploadedBatch"];

// --- Multimodal message content (POST /api/agents/{id}/messages) ---

export type TextContentBlock = Schemas["TextContentBlock"];
export type ImageUrlContentBlock = Schemas["ImageUrlContentBlock"];
export type ContentBlock = TextContentBlock | ImageUrlContentBlock;

// --- Schedules (GET /api/schedules) ---

export type ScheduleSummary = Schemas["ScheduleSummary"];
export type ScheduleView = Schemas["ScheduleView"];
export type ScheduleCreate = Schemas["ScheduleCreate"];
export type ScheduleUpdate = Schemas["ScheduleUpdate"];
export type ScheduleRunView = Schemas["ScheduleRunView"];
export type ScheduleLogsView = Schemas["ScheduleLogsView"];
export type ScheduleDraftResponse = Schemas["ScheduleDraftResponse"];

// --- Ava Guide (natural-language ops assistant; POST /api/guide/draft) ---
export type GuideDraftResponse = Schemas["GuideDraftResponse"];

// --- Package install (POST /api/packages/draft) ---
//
// Every skill / plugin / MCP install starts as a natural-language request that
// spawns an installer agent — there is no URL form, because the user is not
// expected to know which package is any good.
export type PackageKind = Schemas["PackageDraftRequest"]["kind"];
export type PackageDraftResponse = Schemas["PackageDraftResponse"];

// --- Presets (config templates for spawning agents; GET /api/presets) ---

export type PresetView = Schemas["PresetView"];
export type PresetUpdate = Schemas["PresetUpdate"];

// --- Composer commands (registered prompt templates) ---

export type CommandItem = Schemas["CommandItem"];

// --- Fleet graph (GET /api/fleet/graph) ---
//
// The weighted agent-relationship graph behind the fleet Graph View: nodes are
// agents, edges are permanent spawn/fork/resurrect lineage + decaying
// agent-to-agent message traffic (one edge per (from,to,event_type),
// event_count accumulating repeats, weight a recency*frequency score).
//
// The raw endpoint types come from OpenAPI below. The console overlays only
// its public lifecycle vocabulary and normalized edge-event vocabulary; every
// other field (including liveness, nullable machine, data staleness, telemetry
// health, and snapshot time) stays pinned to the generated wire contract.

// Lineage ties (spawn / fork / resurrect) + the message tie. The backend emits
// the raw event_log value `send_message`; normalizeGraph maps it to `message`
// (the graph's vocabulary), so the view keys on `message` here.
export type GraphEventType = "spawn" | "fork" | "resurrect" | "message";

export type WireFleetGraphNode = Schemas["FleetGraphNode"];
export type WireFleetGraphEdge = Schemas["FleetGraphEdge"];
export type WireFleetGraph = Schemas["FleetGraphResponse"];

export type FleetGraphNode = Omit<WireFleetGraphNode, "status"> & {
  readonly status: PublicAgentStatus;
};

export type FleetGraphEdge = Omit<WireFleetGraphEdge, "event_type"> & {
  readonly event_type: GraphEventType;
};

export type FleetGraph = Omit<WireFleetGraph, "nodes" | "edges"> & {
  readonly nodes: readonly FleetGraphNode[];
  readonly edges: readonly FleetGraphEdge[];
};
// --- Tasks (GET /api/tasks) ---
//
// The task registry — persistent, process-decoupled work items that outlive
// the agent doing them. Backs the Task Graph (a free D3-force view).

// 'ongoing' marks long-running active work. Regular tasks reach it through
// update/PATCH; the system root is permanently ongoing and immutable.
export type TaskStatus = "in_progress" | "done" | "cancelled" | "ongoing";

// The task's stakes axis (P0 highest .. P3 lowest) — the same four rungs as a
// notice priority, so it reuses the PRIORITY_* style maps in lib/notices.ts.
export type TaskPriority = "P0" | "P1" | "P2" | "P3";

export interface TaskSummaryRow {
  readonly id: number;
  readonly parent_id: number | null;
  readonly title: string;
  readonly status: TaskStatus;
  readonly priority: TaskPriority;
  readonly owner: number | null;
  readonly owner_label?: string | null;
  readonly created_by: string;
  readonly created_at: string;
  readonly updated_at: string;
  // Reminder fields — the gateway always emits them (Pydantic defaults), so
  // they match the generated schema's optionality exactly (mirrored here so
  // the hover detail card can show them).
  readonly remind_interval_seconds?: number | null;
  readonly last_reminded_at?: string | null;
  readonly reminder_count: number;
  // Out-of-window structural ancestor delivered by a windowed GET /api/tasks
  // (the task graph renders it dimmed so the tree never dangles).
  readonly ghost?: boolean;
}

export interface TaskRow extends TaskSummaryRow {
  readonly description: string;
  readonly results: string | null;
}

export type TaskFields = "full" | "summary";

export interface TaskListResponse<T extends TaskSummaryRow = TaskSummaryRow> {
  readonly tasks: readonly T[];
}


// --- User Settings (GET/PUT /api/settings) ---
//
// Persistent key-value preferences stored in the user_settings DB table.
// The frontend owns the shape and validation of each key's value; the
// gateway treats values as opaque JSONB.

export interface UserSettingRow {
  readonly key: string;
  readonly value: unknown;
  readonly updated_at: string;
}

export interface UserSettingListResponse {
  readonly settings: readonly UserSettingRow[];
}

/** Known settings keys and their value shapes. Add a key here when you
 *  introduce a new setting — this is the single source of defaults and
 *  types for the whole frontend. */
// Quiet-by-default (RCS — reduce context switch): anything that leaks dynamic
// change into the user's periphery (status colors, activity lines, awaiting-reply
// badges) defaults OFF; showing it is an explicit opt-in. Static presence
// (agent ID / label) is always shown.
export const USER_SETTING_DEFAULTS: Record<string, unknown> = {
  // UI language for the interface ("en" | "zh"). Drives next-intl's active
  // locale via the LanguageProvider bridge; agent content and other data
  // surfaces stay untranslated. Default "en" — English UI until the user opts
  // into another locale in Display settings.
  "display.language": "en",
  "display.show_machine_name": true,
  "display.time_mode": "last_active",
  "display.show_terminated": false,
  "display.date_format": "relative",
  "display.show_activity_line": false,
  "display.show_agent_status": false,
  "display.show_timestamp_weekday": true,
  // Render agent reasoning content as markdown in the timeline; off shows raw text.
  "display.render_reasoning_markdown": true,
  // (display.collapse_agent_runs was removed from the defaults — it had zero
  // readers; run-collapse grouping is always on and the Steps header toggle
  // controls expand/collapse via display.expand_runs_mode.)
  // Width tier of the composer's collapsed context-usage bar (ContextMeter).
  // "comfortable" is one notch above the original fixed size — a bare dot at
  // tens of thousands of tokens was unreadable at the old width.
  "display.context_meter_width": "comfortable",
  // Max width of the timeline content column as a fraction of the viewport
  // (see lib/timeline-width.ts for clamping + the 1280px ceiling). 0.4 =
  // 768px on a 1920px screen — the pre-setting fixed width from #714.
  "display.timeline_width_ratio": 0.4,
  // Default-expanded state of "N steps" run blocks. The Details control in the
  // header (ContentToggle) and the "Collapse details by default" row in the
  // Display settings both control this flag — acting as expand-all /
  // collapse-all across every run. Default true: steps start expanded.
  "display.expand_runs_mode": "all",
  // Inspector side-panel open/closed — a workspace preference shared by the
  // composer's toggle and the timeline layout. Default CLOSED (user ruling
  // 2026-08-23, superseding the 2026-08-05 floating desktop panel); the
  // composer's toggle opens it.
  "display.inspector_open": false,
  // Sidebar layout. The homepage split ratio is device-local state owned by
  // react-resizable-panels; stored display.sidebar_width is a legacy pixel
  // value and is ignored — kept in the type only so old rows read cleanly.
  "display.sidebar_width": 240,
  "display.sidebar_collapsed": false,
  "display.sidebar_view_mode": "flat",
  // Flat-list sort: { key: "id" | "last_active" | "status", dir: "asc" | "desc" }.
  "display.sidebar_sort": { key: "id", dir: "desc" },
  // Sidebar stats aggregation window (`?hours=`). Must stay within STATS_WINDOWS.
  "display.stats_window_hours": 24,
  "display.run_timeline_window_hours": 2,
  // Fleet view surfaces.
  "display.fleet_left_view": "graph",
  "display.fleet_queue_collapsed": false,
  "display.task_graph_mode": "graph",
  "display.task_show_done": false,
  "display.task_show_canceled": false,
  // Task graph time filter — last-activity window ("24h" | "7d" | "30d" | "all").
  "display.task_window": "24h",
  // Shell tail page terminal theme: "system" | "light" | "dark".
  "display.shell_terminal_theme": "system",
  // Plugin-contributed skin, as "<plugin>/<theme>" (themePackId). null = the
  // console's own palette. A pack re-values the :root color tokens on the root
  // element, so it applies over whichever of light/dark is active — a plugin
  // that wants both ships one pack per mode.
  "display.theme_pack": null,
  "behavior.confirm_terminate": true,
  "behavior.confirm_restart": true,
  "behavior.confirm_force_kill": true,
  // Sticky spawn-composer picker defaults. null = "no override" (use the cluster
  // default model / no preset / provider-default effort).
  "behavior.spawn_model": null,
  "behavior.spawn_preset": null,
  "behavior.spawn_reasoning_effort": null,
  "notification.awaiting_reply": false,
  // Model picker visibility: model names the user has hidden from the spawn
  // model select. An empty list means all models are visible.
  "models.hidden": [],
  // Force-directed layout knobs for the fleet Graph / Task Graph
  // (display.graph_force_params / display.task_force_params) are DB-backed too,
  // but their default objects differ per view and live with those views — the
  // useForceParams hook merges settings over the view's passed defaults, so the
  // full ForceParams objects are intentionally not enumerated here.
} as const;

/** Time display mode for agent rows. `hidden` renders no timestamp at all. */
export type TimeMode = "last_active" | "spawned" | "hidden";

/** Width tier for the composer's collapsed context-usage bar. */
export type ContextMeterWidth = "compact" | "comfortable" | "wide";

/** Date format mode. */
export type DateFormat = "relative" | "absolute";
