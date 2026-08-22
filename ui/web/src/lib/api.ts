// Thin fetch wrapper over FastAPI endpoints.
//
// API_BASE resolution order (at module evaluation; once on server, once on client):
//   1. `NEXT_PUBLIC_API_BASE` set → use it (override knob for unusual setups).
//   2. Not set + client → `${protocol}//<host>:<gateway-port>`. Frontend (:3000)
//      and the gateway are co-located on the gateway host but on different
//      ports, so the gateway is the same hostname on the gateway port — works for
//      localhost and any private-network address. The port is
//      `NEXT_PUBLIC_GATEWAY_PORT`, injected at build time from `AVA_GATEWAY_PORT`
//      (the frontend build command sets it; see cli/commands/_repo.py). Default
//      8000; override it on a host where another service holds the port.
//   3. SSR (window undefined) + no env → "" (SSR doesn't fetch, placeholder).
//
// The gateway is reachable only on the cluster's private network (one trust group)
// Authentication is via session cookie (browser) or Bearer token (SDK).

import type { NoticesFeed,
  UserSettingListResponse,
  UserSettingRow,
  AgentInspect,
  ComputerTraceResponse,
  AgentMachineRow,
  AgentMessageEnqueued,
  AgentRow,
  ContextBreakdownResponse,
  DefaultModelView,
  AlertsResponse,
  AlertsWindow,
  ResolveNoticeIn,
  CancelRequested,
  ClusterStatus,
  ClusterUpdateCheck,
  CommandItem,
  ContentBlock,
  CompactEnqueued,
  CompactMode,
  ConfigView,
  ConfigWriteResult,
  FleetGraph,
  GuideDraftResponse,
  TaskListResponse,
  InventoryAggregate,
  InventoryWriteResult,
  SkillView,
  SkillsView,
  MemoryGraphResponse,
  ModelsResponse,
  PackageDraftResponse,
  PackageKind,
  PageRow,
  PendingInbound,
  PresetUpdate,
  PresetView,
  ResolvedConfigView,
  RestartAgentResponse,
  ResurrectAgentResponse,
  ShellCapture,
  SpawnAgentRequest,
  SpawnedAgent,
  StatsDashboard,
  SystemStatus,
  TerminateAgentResponse,
  TimelineResponse,
  TokenUsageResponse,
  ScheduleCreate,
  ScheduleDraftResponse,
  ScheduleLogsView,
  ScheduleRunView,
  ScheduleSummary,
  ScheduleUpdate,
  ScheduleView,
  UiContributionsResponse,
  UploadedBatch,
} from "./types";
import { projectAgentStatus } from "./types";
import { sendMessageWithReconciliation } from "./message-delivery";

export { MessageDeliveryUnknownError } from "./message-delivery";

export const API_BASE = ((): string => {
  if (process.env.NEXT_PUBLIC_API_BASE) return process.env.NEXT_PUBLIC_API_BASE;
  if (typeof window === "undefined") return "";
  // Frontend (:3000) and the gateway are co-located on the gateway host
  // but on different ports, so the gateway is the same hostname on the gateway
  // port — works for localhost and any private-network address.
  const port = process.env.NEXT_PUBLIC_GATEWAY_PORT ?? "8000";
  const { hostname, protocol } = window.location;
  return `${protocol}//${hostname}:${port}`;
})();

// Thrown by `ok()` for any non-2xx response. Carries the HTTP status so
// callers can distinguish "the server answered but rejected the request"
// (e.g. 401 bad credentials) from a network-level failure (fetch reject —
// connection refused, DNS failure, timeout, CORS), which never reaches here
// at all and surfaces as a plain TypeError instead. See auth-context.tsx's
// `login()` for the canonical consumer of this distinction.
export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function ok<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, `HTTP ${res.status}: ${text || res.statusText}`);
  }
  return res.json() as Promise<T>;
}

const url = (path: string): string => `${API_BASE}${path}`;

// Absolute url for a gateway-served asset (e.g. an uploaded image's reference
// path) — the frontend (:3000) and gateway (:8000) are different origins, so an
// <img src> needs the gateway base prepended.
export const assetUrl = (path: string): string => `${API_BASE}${path}`;

// All fetch calls go through this. ``credentials: "include"`` sends the
// session cookie on cross-origin requests (frontend :3000 -> gateway :8000).
function f(path: string, init?: RequestInit): Promise<Response> {
  return fetch(url(path), { ...init, credentials: "include" });
}

async function jsonWithTimeout<T>(
  path: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<T> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new DOMException(`request exceeded ${timeoutMs}ms`, "TimeoutError"));
    }, timeoutMs);
  });
  try {
    // The bound covers BOTH fetch and body consumption. Clearing the timer when
    // headers arrive leaves `res.json()` able to hang the Composer forever.
    return await Promise.race([
      f(path, { ...init, signal: controller.signal }).then(ok<T>),
      timeout,
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

function isAmbiguousDeliveryError(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true;
  return error.status === 408 || error.status === 429 || error.status >= 500;
}

const POST: RequestInit = { method: "POST" };
const POST_JSON = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify(body),
});
const PATCH_JSON = (body: unknown): RequestInit => ({
  method: "PATCH",
  headers: { "content-type": "application/json" },
  body: JSON.stringify(body),
});

const PUT_JSON = (body: unknown): RequestInit => ({
  method: "PUT",
  headers: { "content-type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  // One task's computer-use desktop-action trail (Phase 3, task #1101):
  // session envelope + chronological computer_action rows for a task_id.
  getComputerTrace: (taskId: number): Promise<ComputerTraceResponse> => {
    return f(`/api/computer/traces?task_id=${taskId}`).then(ok<ComputerTraceResponse>);
  },

  // Returns one tail window of the timeline (newest `limit` items). Pass
  // `before` = the oldest item_id currently held to page further back for
  // scroll-up history; `has_more` reports whether older items remain.
  getTimeline: (
    agentId: number,
    opts?: { limit?: number; before?: string },
  ): Promise<TimelineResponse> => {
    const params = new URLSearchParams();
    if (opts?.limit != null) params.set("limit", String(opts.limit));
    if (opts?.before != null) params.set("before", opts.before);
    const qs = params.toString();
    return f(`/api/agents/${agentId}/timeline${qs ? `?${qs}` : ""}`).then(
      ok<TimelineResponse>,
    );
  },

  getTokenUsage: (agentId: number): Promise<TokenUsageResponse> => {
    return f(`/api/agents/${agentId}/token-usage`).then(ok<TokenUsageResponse>);
  },

  getContextBreakdown: (agentId: number): Promise<ContextBreakdownResponse> => {
    return f(`/api/agents/${agentId}/context-breakdown`).then(ok<ContextBreakdownResponse>);
  },

  // Per-agent inspector panel: live persistent shells + frozen config overlay
  // + LLM cost + turn/exec stats. Single-agent counterpart to getStatsDashboard
  // (fleet-wide). Polled while the panel is open. `hours` windows cost + stats
  // (whitelisted 1/6/24/72/168); omitted = cumulative since spawn. `sinceCompact`
  // windows them to events since the agent's latest compact halt instead
  // (takes precedence over `hours` backend-side).
  getAgentInspect: (
    agentId: number,
    hours?: number | null,
    sinceCompact?: boolean,
  ): Promise<AgentInspect> => {
    const params = new URLSearchParams();
    if (hours != null) params.set("hours", String(hours));
    if (sinceCompact) params.set("since_compact", "true");
    const qs = params.toString();
    return f(`/api/agents/${agentId}/inspect${qs ? `?${qs}` : ""}`).then(ok<AgentInspect>);
  },

  // A recent tail of one of the agent's persistent shells — the terminal
  // output the /shell/{agentId}/{sessionId} page fetches on demand. `lines`
  // controls how many trailing lines to capture (50–2000, default 200).
  // 404 if the agent or the live shell session is gone.
  getAgentShell: (agentId: number, sessionId: number, lines?: number): Promise<ShellCapture> => {
    const qs = lines != null ? `?lines=${lines}` : "";
    return f(`/api/agents/${agentId}/shell/${sessionId}${qs}`).then(ok<ShellCapture>);
  },

  getPendingMessages: (agentId: number): Promise<PendingInbound[]> => {
    return f(`/api/agents/${agentId}/pending`).then(ok<PendingInbound[]>);
  },

  getCommands: (): Promise<CommandItem[]> => {
    return f("/api/commands").then(ok<CommandItem[]>);
  },

  // `content` is a plain string, or a list of OpenAI-shaped content blocks for
  // a multimodal message (text + image_url referencing an upload of this agent).
  sendMessage: (
    agentId: number,
    content: string | ContentBlock[],
    clientMessageId: string,
  ): Promise<AgentMessageEnqueued> => {
    return sendMessageWithReconciliation(
      agentId,
      content,
      clientMessageId,
      jsonWithTimeout,
      isAmbiguousDeliveryError,
    );
  },

  // The unified inbox feed (Task #1024, R4 layer 2, decision Q1=A): one
  // request carries the whole Inbox panel — the open queue split by kind
  // (open = FYI, awaiting = require_response) plus one keyset page of the
  // resolved history and its next_cursor. The frontend's Inbox consumes
  // ONLY this endpoint now; the standalone /open + /resolved endpoints stay
  // for the IM bridge and CLI.
  getNotices: (params?: {
    limit?: number;
    resolvedLimit?: number;
    beforeAt?: string;
    beforeId?: number;
  }): Promise<NoticesFeed> => {
    const sp = new URLSearchParams();
    if (params?.limit != null) sp.set("limit", String(params.limit));
    if (params?.resolvedLimit != null) sp.set("resolved_limit", String(params.resolvedLimit));
    if (params?.beforeAt != null) sp.set("before_at", params.beforeAt);
    if (params?.beforeId != null) sp.set("before_id", String(params.beforeId));
    const qs = sp.toString();
    return f(`/api/notices${qs ? `?${qs}` : ""}`).then(ok<NoticesFeed>);
  },

  // Resolve one open notice. `action` is the explicit close verb: 'answer' /
  // 'dismiss' on a require_response notice ('answer' needs a reply), 'read' on an
  // FYI notice. A non-empty reply (and a dismissed require_response notice) wakes
  // the agent with a chat inbound. 409 if the notice is no longer open or the
  // action does not match its kind.
  resolveNotice: (
    agentId: number,
    noticeId: number,
    body: ResolveNoticeIn,
  ): Promise<{ status: string }> => {
    return f(
      `/api/agents/${agentId}/notices/${noticeId}/resolve`,
      POST_JSON(body),
    ).then(ok<{ status: string }>);
  },

  compact: (agentId: number, mode: CompactMode): Promise<CompactEnqueued> => {
    return f(`/api/agents/${agentId}/compact?mode=${mode}`, POST).then(
      ok<CompactEnqueued>,
    );
  },

  cancel: (agentId: number): Promise<CancelRequested> => {
    // Each agent runs its own turn — the gateway watcher dispatches to
    // the corresponding cancel_event. Returns as soon as the signal is
    // sent; the actual kernel response is delivered to the UI via the
    // SSE `cancelled` event.
    return f("/api/cancel", POST_JSON({ agent_id: agentId })).then(
      ok<CancelRequested>,
    );
  },

  // --- lifecycle ---
  //
  // Selecting a sidebar row = selecting the agent to view;
  // spawn / terminate all operate by agent_id.

  listAgents: (): Promise<AgentRow[]> => {
    return f("/api/agents")
      .then(ok<AgentRow[]>)
      .then((rows) => rows.map(projectAgentStatus));
  },

  spawnAgent: (req: SpawnAgentRequest = {}): Promise<SpawnedAgent> => {
    return f("/api/agents", POST_JSON(req)).then(ok<SpawnedAgent>);
  },

  // force=false (default) → graceful path: backend inserts a terminate
  // inbound and the agent exits after its current turn. force=true → backend
  // skips the inbound path and directly kills the OS process + force-marks
  // status='terminated'; for agents stuck in a loop / hung call that never
  // reach the point where the graceful signal is consumed.
  terminateAgent: (agentId: number, force = false): Promise<TerminateAgentResponse> => {
    return f(
      `/api/agents/${agentId}/terminate`,
      force ? POST_JSON({ force: true }) : POST,
    ).then(ok<TerminateAgentResponse>);
  },

  restartAgent: (agentId: number): Promise<RestartAgentResponse> => {
    return f(`/api/agents/${agentId}/restart`, POST).then(ok<RestartAgentResponse>);
  },

  resurrectAgent: (
    agentId: number,
    prompt?: string,
  ): Promise<ResurrectAgentResponse> => {
    // The resurrect button is a pure lifecycle event — no prompt; the agent
    // just wakes (it still gets the "you have been resurrected" marker). A
    // prompt, when given (the "with prompt..." path), is inserted as a chat
    // inbound in the same transaction as the resurrect lifecycle inbound.
    // resurrected_by is left to the backend default ("user").
    return f(
      `/api/agents/${agentId}/resurrect`,
      POST_JSON(prompt !== undefined ? { prompt } : {}),
    ).then(ok<ResurrectAgentResponse>);
  },

  // --- agent label ---
  //
  // The label is auto-generated by a gateway BackgroundTask running a
  // backend LLM whenever spawn carries a prompt (<=12 chars, language
  // follows the prompt). Users can also override / reset it via PATCH.
  // An empty string resets back to NULL, and the frontend falls back to
  // showing `#N`. After the DB write, the backend publishes a
  // LabelUpdated SSE event to push every client.

  patchAgentLabel: (agentId: number, label: string): Promise<void> => {
    return f(`/api/agents/${agentId}`, PATCH_JSON({ label })).then(async (res) => {
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status}: ${text || res.statusText}`);
      }
    });
  },

  // --- ava.ui.show pages ---
  //
  // page-load fetches open pages; SSE page_opened / page_closed cover
  // increments. PageRow.url is a gateway reverse-proxy URL
  // (shape: /api/pages/<agent_id>-<name>/); the frontend opens it in a new
  // tab.

  listPages: (agentId: number): Promise<PageRow[]> => {
    return f(`/api/agents/${agentId}/pages`).then(ok<PageRow[]>);
  },

  // Every agent's currently-open pages in one fetch — the fleet-wide twin of
  // listPages, so a many-agent view (the inbox) can surface each agent's live
  // page without a per-agent request. SSE page_opened / page_closed cover deltas.
  listAllPages: (): Promise<PageRow[]> => {
    return f(`/api/pages`).then(ok<PageRow[]>);
  },

  // --- fleet graph ---
  // The weighted agent-relationship graph for the fleet Graph View
  // (spawn/fork lineage + aggregated message edges + dynamic weights).
  // `hours` windows both node score and edge events (whitelisted backend-side,
  // 1/6/24/72/168; omitted = all-time). `decayLambda` is the per-day edge-weight
  // decay constant (default 0.5 backend-side).
  getFleetGraph: (opts?: { hours?: number; decayLambda?: number }): Promise<FleetGraph> => {
    const params = new URLSearchParams();
    if (opts?.hours != null) params.set("hours", String(opts.hours));
    if (opts?.decayLambda != null) params.set("decay_lambda", String(opts.decayLambda));
    const qs = params.toString();
    return f(`/api/fleet/graph${qs ? `?${qs}` : ""}`).then(ok<FleetGraph>);
  },

  // --- tasks ---
  // The task registry (GET /api/tasks) — the full agent_tasks table. Data
  // source for the Task Graph (free force-directed view).
  getTasks: (): Promise<TaskListResponse> => {
    return f("/api/tasks").then(ok<TaskListResponse>);
  },

  // --- memory graph ---
  // OKF concept-note graph rendered by /okf.
  getMemoryGraph: (): Promise<MemoryGraphResponse> => {
    return f("/api/memory/graph").then(ok<MemoryGraphResponse>);
  },

  // --- stats ---
  // One endpoint loads all the stat cards at the top of the sidebar in a
  // single request (live count / tokens / avg turn / warnings+errors).
  // `hours` selects the aggregation window — whitelisted backend-side
  // (1/6/24/72/168), anything else 422s.
  getStatsDashboard: (hours: number): Promise<StatsDashboard> => {
    return f(`/api/stats/dashboard?hours=${hours}`).then(ok<StatsDashboard>);
  },

  // --- ops monitor (Insights Ops tab) ---
  // Time-bucketed ops series over agent_events — SSE/event-log backlog, LLM
  // latency + TPS, process restart counts — one round trip per window.
  // Polled at 60s while the Ops section is visible (useSectionVisible).

  // --- alerts (the system→human alert store, Task #1224) ---
  // Alert instances in the Alertmanager webhook shape, separate from Notice.
  // The SSE live tail (/api/alerts/stream) folds into the ["alerts"] cache;
  // this fetch is the initial load + refetch fallback. Read alerts are
  // excluded by default unless includeRead is set.
  getAlerts: (params: {
    window?: AlertsWindow;
    status?: string;
    severity?: string;
    includeRead?: boolean;
    limit?: number;
  } = {}): Promise<AlertsResponse> => {
    const q = new URLSearchParams();
    if (params.window) q.set("window", params.window);
    if (params.status) q.set("status", params.status);
    if (params.severity) q.set("severity", params.severity);
    if (params.includeRead) q.set("include_read", "true");
    if (params.limit) q.set("limit", String(params.limit));
    const qs = q.toString();
    return f(`/api/alerts${qs ? `?${qs}` : ""}`).then(ok<AlertsResponse>);
  },

  // Mark specific alerts read — or with `all: true`, every alert. Returns
  // the number updated. ids are alerts row ids (int, per the backend schema).
  markAlertsRead: (ids: readonly number[]): Promise<{ updated: number }> =>
    f("/api/alerts/read", PATCH_JSON({ ids })).then(ok<{ updated: number }>),
  markAllAlertsRead: (): Promise<{ updated: number }> =>
    f("/api/alerts/read", PATCH_JSON({ all: true })).then(ok<{ updated: number }>),

  // --- config (runtime config panel) ---
  //
  // `machine` targets a specific host's view; omitted = this gateway
  // (cluster + this host's own host fields). For a remote machine the host
  // fields carry that machine's values + capability hints, while cluster /
  // agent fields are the gateway's own (machine-independent).

  getConfig: (machine?: string): Promise<ConfigView> => {
    const qs = machine != null ? `?machine=${encodeURIComponent(machine)}` : "";
    return f(`/api/config${qs}`).then(ok<ConfigView>);
  },

  // Per-model resolution of the settings that have per-model defaults, each row
  // naming the layer its effective value came from. No `machine` — every
  // per-model-defaultable field is cluster-scope. Omitting `model` resolves
  // against the cluster's own model.
  getResolvedConfig: (model?: string): Promise<ResolvedConfigView> => {
    const qs = model != null ? `?model=${encodeURIComponent(model)}` : "";
    return f(`/api/config/resolved${qs}`).then(ok<ResolvedConfigView>);
  },

  // Every registered machine + its live status (name / description / live).
  // Backs the config-page machine selector and ava.agents.list_machines().
  getMachines: (): Promise<AgentMachineRow[]> => {
    return f("/api/cluster/machines").then(ok<AgentMachineRow[]>);
  },

  // --- system status ---

  getModels: (): Promise<ModelsResponse> => {
    return f("/api/models").then(ok<ModelsResponse>);
  },

  // The model a NEW agent is born on, and which layer produced it ("cluster" =
  // the DB row set here, "config" = the .env / code default showing through).
  // Its own narrow endpoint, not a field on PUT /api/config.
  getDefaultModel: (): Promise<DefaultModelView> => {
    return f("/api/config/default-model").then(ok<DefaultModelView>);
  },

  // Set the cluster's default model. 400s on an id outside the spawnable roster.
  // Applies to agents born after the write — every existing agent keeps the model
  // frozen on its own row.
  putDefaultModel: (model: string): Promise<DefaultModelView> => {
    return f("/api/config/default-model", PUT_JSON({ model })).then(ok<DefaultModelView>);
  },

  // --- File Upload ---

  // `deliver=false` saves the files silently and returns their reference urls
  // without notifying the agent — the native-image-attachment path, where the
  // caller carries the returned url into the next multimodal message. The
  // default (true) is the paperclip / drag-drop notify path for arbitrary files.
  uploadFiles: (
    agentId: number,
    files: File[],
    onProgress?: (pct: number) => void,
    deliver = true,
  ): Promise<UploadedBatch> => {
    const formData = new FormData();
    for (const file of files) {
      formData.append("files", file, file.name);
    }
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", url(`/api/agents/${agentId}/uploads?deliver=${deliver}`));
      xhr.withCredentials = true;
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) {
          onProgress(Math.round((e.loaded / e.total) * 100));
        }
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(JSON.parse(xhr.responseText) as UploadedBatch);
        } else {
          let msg = `HTTP ${xhr.status}`;
          try { const err = JSON.parse(xhr.responseText) as { detail?: string }; if (err.detail) msg = err.detail; } catch { /* */ }
          reject(new Error(msg));
        }
      };
      xhr.onerror = () => reject(new Error("Upload failed"));
      xhr.send(formData);
    });
  },

  getSystemStatus: (): Promise<SystemStatus> => {
    return f("/api/status").then(ok<SystemStatus>);
  },

  // This host's cluster snapshot. Unlike /api/status, this path bypasses the
  // cluster-paused 503 middleware, so it is the one status source readable while
  // the cluster is paused — used to tell a live rollout (paused + orchestration
  // set) from a stranded pause (paused + orchestration null) for recovery.
  getClusterStatus: (): Promise<ClusterStatus> => {
    return f("/api/cluster/status").then(ok<ClusterStatus>);
  },

  // Operator stranded-cluster recovery: force-clear a pause + update lock left by
  // a hard-killed rollout. 409 if an orchestration is actually in flight. Returns
  // {unlocked_holder}. Bypasses the 503 middleware, so callable while wedged.
  recoverCluster: (): Promise<{ unlocked_holder: string | null }> => {
    return f("/api/cluster/recover", POST).then(ok<{ unlocked_holder: string | null }>);
  },

  // Trigger the whole-cluster rolling upgrade. Gateway only: pauses every
  // agent-runner, updates all hosts in sequence, then resumes agents on new code.
  // Returns 202 with {session, log}. 409 means a rollout or update is already
  // in flight — caller should wait and retry.
  triggerClusterRollout: (): Promise<{ session: string; log: string }> => {
    return f("/api/cluster/rollout", POST).then(ok<{ session: string; log: string }>);
  },

  // Read-only preflight for the Update button: how far the gateway is
  // behind origin/main and which side a rollout would restart. behind===0 means
  // "no updates" — the UI shows that and does not launch a rollout.
  checkClusterUpdate: (): Promise<ClusterUpdateCheck> => {
    return f("/api/cluster/update-check").then(ok<ClusterUpdateCheck>);
  },

  // Graceful whole-cluster restart with NO git pull — bounce every service on
  // the current code to apply config changes. Gateway only. Returns 202
  // with {session, log}; 409 means a restart/rollout/update is already in flight.
  triggerClusterRestart: (): Promise<{ session: string; log: string }> => {
    return f("/api/cluster/restart", POST).then(ok<{ session: string; log: string }>);
  },

  // Full-replace of the override layer. `machine` targets a host's overrides
  // (host-scope keys only — a cluster key on a remote PUT is rejected 400);
  // omitted = this gateway. Returns per-field verdicts: `applied` is true
  // iff every field passed (atomic — one bad field writes nothing), `results`
  // carries the per-field ok/reason, `restart_required` the union of written
  // fields' restart targets for the per-machine banner.
  async putConfig(
    body: Record<string, unknown>,
    machine?: string,
  ): Promise<ConfigWriteResult> {
    const qs = machine != null ? `?machine=${encodeURIComponent(machine)}` : "";
    const res = await f(`/api/config${qs}`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status}: ${text || res.statusText}`);
    }
    return res.json() as Promise<ConfigWriteResult>;
  },

  // --- inventory (Plugins & MCP, cross-machine matrix) ---
  //
  // GET /api/inventory (no machine arg) returns the collapsed cross-machine
  // matrix: every machine considered + per plugin/MCP-server its per-host
  // state. The single-machine ?machine= view exists on the backend but the UI
  // drives entirely off the aggregate, so only the matrix getter is exposed.
  getInventory: (): Promise<InventoryAggregate> => {
    return f("/api/inventory").then(ok<InventoryAggregate>);
  },

  // Enable/disable plugins + MCP servers on ONE host. `machine` targets that
  // host's inventory; the body carries the desired on/off per item name.
  // Returns per-item verdicts: `applied` is true iff every item passed (atomic
  // — one rejected item writes nothing), `plugin_results` / `mcp_results` carry
  // the per-item ok/reason.
  async putInventory(
    body: { plugins?: Record<string, boolean>; mcp_servers?: Record<string, boolean> },
    machine: string,
  ): Promise<InventoryWriteResult> {
    const res = await f(`/api/inventory?machine=${encodeURIComponent(machine)}`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status}: ${text || res.statusText}`);
    }
    return res.json() as Promise<InventoryWriteResult>;
  },

  // This gateway host's installed skills — name / source layer (core/plugin/
  // machine) / enabled / local drift. Read-only, single-host (skills are
  // per-machine; there is no cross-machine matrix like inventory).
  getSkills: (): Promise<SkillsView> => {
    return f("/api/skills").then(ok<SkillsView>);
  },

  // Toggle one skill's enabled flag in the install registry. The change is
  // immediate; the skill scanner picks it up on the next agent spawn.
  putSkillEnabled: (name: string, enabled: boolean): Promise<SkillView> => {
    return f("/api/skills", PUT_JSON({ name, enabled })).then(ok<SkillView>);
  },

  // ── Auth ───────────────────────────────────────────────────────────

  // Authenticate with the cluster password. The username field is accepted
  // for Chrome password-manager compatibility but not validated — only the
  // password (cluster secret) matters. On success the gateway sets a session
  // cookie; subsequent requests carry it automatically.
  login: (username: string, password: string): Promise<{ ok: boolean }> => {
    return f("/api/auth/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ username, password }),
    }).then(ok<{ ok: boolean }>);
  },

  // Clear the session cookie. Idempotent — always succeeds.
  logout: (): Promise<{ ok: boolean }> => {
    return f("/api/auth/logout", { method: "POST" }).then(
      ok<{ ok: boolean }>,
    );
  },

  // Check whether the current request carries a valid session cookie.
  checkAuth: (): Promise<{ authenticated: boolean }> => {
    return f("/api/auth/check").then(ok<{ authenticated: boolean }>);
  },

  // --- Schedules (GET /api/schedules) ---

  listSchedules: (): Promise<ScheduleSummary[]> => {
    return f("/api/schedules").then(ok<ScheduleSummary[]>);
  },

  getSchedule: (id: number): Promise<ScheduleView> => {
    return f(`/api/schedules/${id}`).then(ok<ScheduleView>);
  },

  createSchedule: (body: ScheduleCreate): Promise<ScheduleView> => {
    return f("/api/schedules", POST_JSON(body)).then(ok<ScheduleView>);
  },

  updateSchedule: (id: number, body: ScheduleUpdate): Promise<ScheduleView> => {
    return f(`/api/schedules/${id}`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(ok<ScheduleView>);
  },

  deleteSchedule: (id: number): Promise<{ status: string }> => {
    return f(`/api/schedules/${id}`, { method: "DELETE" }).then(ok<{ status: string }>);
  },

  startSchedule: (id: number): Promise<ScheduleView> => {
    return f(`/api/schedules/${id}/start`, POST).then(ok<ScheduleView>);
  },

  stopSchedule: (id: number): Promise<ScheduleView> => {
    return f(`/api/schedules/${id}/stop`, POST).then(ok<ScheduleView>);
  },

  restartSchedule: (id: number): Promise<ScheduleView> => {
    return f(`/api/schedules/${id}/restart`, POST).then(ok<ScheduleView>);
  },

  scheduleLogs: (id: number, lines = 200): Promise<ScheduleLogsView> => {
    return f(`/api/schedules/${id}/logs?lines=${lines}`).then(ok<ScheduleLogsView>);
  },

  scheduleRuns: (id: number, limit = 50): Promise<ScheduleRunView[]> => {
    return f(`/api/schedules/${id}/runs?limit=${limit}`).then(ok<ScheduleRunView[]>);
  },

  draftSchedule: (nl: string): Promise<ScheduleDraftResponse> => {
    return f("/api/schedules/draft", POST_JSON({ nl })).then(ok<ScheduleDraftResponse>);
  },

  // Spawn an ava-guide agent for a natural-language ops request; returns its id
  // so the Control page's Guide entry can open the conversation.
  draftGuide: (nl: string): Promise<GuideDraftResponse> => {
    return f("/api/guide/draft", POST_JSON({ nl })).then(ok<GuideDraftResponse>);
  },

  // Spawn an ava-package-installer agent to find, install, and verify a skill /
  // plugin / MCP server; returns its id so the Control page can open the
  // conversation. The whole lifecycle happens in that session — there is no
  // install-by-URL endpoint on purpose.
  draftPackage: (kind: PackageKind, nl: string): Promise<PackageDraftResponse> => {
    return f("/api/packages/draft", POST_JSON({ kind, nl })).then(ok<PackageDraftResponse>);
  },

  // --- Presets (config templates; GET /api/presets) ---
  //
  // A preset bundles a per-agent config overlay under a name; selecting one at
  // spawn time seeds the new agent's config from it (an explicit config wins
  // per field). Managed on the /presets page; picked in the spawn dialog.
  // Creation is natural-language only — the presets page composes a prompt
  // pointing a plain spawnAgent() call at ava.skills.ava_guide.presets, which
  // designs the config and creates it directly; this client has no raw
  // createPreset and no dedicated draft endpoint.

  listPresets: (): Promise<PresetView[]> => {
    return f("/api/presets").then(ok<PresetView[]>);
  },

  updatePreset: (id: number, body: PresetUpdate): Promise<PresetView> => {
    return f(`/api/presets/${id}`, PATCH_JSON(body)).then(ok<PresetView>);
  },

  deletePreset: (id: number): Promise<{ status: string }> => {
    return f(`/api/presets/${id}`, { method: "DELETE" }).then(ok<{ status: string }>);
  },

  // --- User settings (GET /api/settings, PUT /api/settings/{key}) ---
  //
  // Persistent key-value preferences stored server-side (user_settings DB
  // table). Each key maps to an opaque JSONB value; the frontend owns the
  // shape and validation of its own keys. Defaults live in USER_SETTING_DEFAULTS.

  getSettings: (): Promise<UserSettingListResponse> => {
    return f("/api/settings").then(ok<UserSettingListResponse>);
  },

  putSetting: (key: string, value: unknown): Promise<UserSettingRow> => {
    return f(`/api/settings/${encodeURIComponent(key)}`, PUT_JSON({ value })).then(
      ok<UserSettingRow>,
    );
  },

  // --- Plugin console contributions (GET /api/ui/contributions) ---
  //
  // The merged, plugin-attributed declaration set of the cluster's enabled
  // plugins. Read like any other server data; it changes only when a plugin is
  // installed, enabled, or upgraded.

  getUiContributions: (): Promise<UiContributionsResponse> => {
    return f("/api/ui/contributions").then(ok<UiContributionsResponse>);
  },
};
