// ava:ref ui/web/src/docs/frontend-state/api-requests.ava.okf.md#request-budget
import { API_BASE } from "./api-base";
import { track } from "../telemetry/telemetry";
import type { ToUserDocument, NoticesFeed,
  UserSettingListResponse,
  UserSettingRow,
  AgentInspectStatistics,
  AgentInspectLive,
  AgentMachineRow,
  InspectWidget,
  AgentMessageEnqueued,
  AgentRow,
  WireAgentRow,
  WireAgentRoster,
  WireAgentDirectoryPage,
  AgentRoster,
  AgentDirectoryPage,
  ConversationSnapshotResponse,
  ContextBreakdownResponse,
  DefaultModelView,
  AlertsResponse,
  AlertsWindow,
  ResolveNoticeIn,
  NativeWorkTarget,
  NativeCancelAcceptance,
  RetryLaunchAccepted,
  ClusterStatus,
  CommandItem,
  ContentBlock,
  CompactTarget,
  CompactAcceptance,
  ConfigView,
  ConfigWriteResult,
  WireFleetGraph,
  GuideDraftResponse,
  TaskListResponse,
  TaskFields,
  TaskRow,
  TaskSummaryRow,
  InventoryAggregate,
  InventoryWriteResult,
  SkillView,
  SkillsView,
  MemoryGraphResponse,
  MemoryNoteResponse,
  ModelsResponse,
  PackageDraftResponse,
  PackageKind,
  PageRow,
  PendingInbound,
  ResolvedConfigView,
  RestartAgentResponse,
  ResurrectAgentResponse,
  RunTimelineContext,
  RunTimelineLinkContent,
  RunTimelineLinks,
  RunTimelineMessages,
  RunTimelineResponse,
  ShellCapture,
  SpawnAgentRequest,
  SpawnedAgent,
  StatsDashboard,
  AlertClassRow,
  AlertClassesResponse,
  AlertClassSample,
  EventResolutionRow,
  SystemStatus,
  TerminateAgentResponse,
  TimelineResponse,
  TokenUsageResponse,
  ReplySuggestionResponse,
  ScheduleCreate,
  ScheduleDraftResponse,
  ScheduleLogsView,
  ScheduleRunView,
  ScheduleSummary,
  ScheduleUpdate,
  ScheduleView,
  UiContributionsResponse,
  UploadedBatch,
} from "../contracts/types";
import { projectAgentStatus } from "../contracts/types";
import { sendMessageWithReconciliation } from "../agents/message-delivery";
import { submitUploadedFiles } from "./upload-delivery";
import { keyedHeaders, newOperationKey } from "./operation-key";

export { MessageDeliveryUnknownError } from "../agents/message-delivery";
export { API_BASE } from "./api-base";

export class ApiError extends Error {
  status: number;
  agentId?: number;
  code?: string;
  launchState?: { status: string; availability?: { reason: string; evidence_at?: string | null } | null };
  retryLaunchPath?: string;
  constructor(status: number, message: string, body?: Record<string, unknown>) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    const detail = body?.detail;
    if (detail && typeof detail === "object" && "code" in detail && typeof detail.code === "string") {
      this.code = detail.code;
      if ("message" in detail && typeof detail.message === "string") this.message = detail.message;
    }
    if (body?.reason === "agent_launch_failed" && typeof body.agent_id === "number") {
      this.agentId = body.agent_id;
      if (typeof body.retry_launch_path === "string") this.retryLaunchPath = body.retry_launch_path;
      if (body.state && typeof body.state === "object") {
        this.launchState = body.state as ApiError["launchState"];
      }
    }
  }
}

async function ok<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    let body: Record<string, unknown> | undefined;
    try {
      const parsed: unknown = JSON.parse(text);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        body = parsed as Record<string, unknown>;
      }
    } catch { body = undefined; }
    throw new ApiError(
      res.status,
      typeof body?.detail === "string" ? body.detail : `HTTP ${res.status}: ${text || res.statusText}`,
      body,
    );
  }
  return res.json() as Promise<T>;
}

const url = (path: string): string => `${API_BASE}${path}`;

export const assetUrl = (path: string): string => `${API_BASE}${path}`;

function apiTimingKey(path: string): string {
  const pathname = path.split("?", 1)[0].replace(/^\/api\/?/, "");

  return pathname
    .split("/")
    .map((segment) => (/^\d+$/.test(segment) ? "id" : segment))
    .join(".");
}

async function f(path: string, init?: RequestInit): Promise<Response> {
  const startedAt = performance.now();
  try {
    return await fetch(url(path), { ...init, credentials: "include" });
  } finally {
    const durationMs = performance.now() - startedAt;
    if (durationMs > 800 && path.split("?", 1)[0] !== "/api/frontend-telemetry") {
      track("api-timing", {
        key: apiTimingKey(path),
        value: Math.round(durationMs),
        dedupe: false,
      });
    }
  }
}

async function jsonWithTimeout<T>(
  path: string,
  init: RequestInit,
  timeoutMs: number,
  callerSignal?: AbortSignal,
  timeoutLabel = "request",
): Promise<T> {
  const controller = new AbortController();
  const abortFromCaller = () => controller.abort();
  if (callerSignal?.aborted) controller.abort();
  else callerSignal?.addEventListener("abort", abortFromCaller, { once: true });
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => {
      reject(new DOMException(`${timeoutLabel} exceeded ${timeoutMs}ms`, "TimeoutError"));
      controller.abort();
    }, timeoutMs);
  });
  try {

    return await Promise.race([
      f(path, { ...init, signal: controller.signal }).then(ok<T>),
      timeout,
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
    callerSignal?.removeEventListener("abort", abortFromCaller);
  }
}

const INSPECT_REQUEST_TIMEOUT_MS = 35_000;

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

function keyedJson(body: unknown, operationKey: string): RequestInit {
  return {
    method: "POST",
    headers: { "content-type": "application/json", ...keyedHeaders(operationKey) },
    body: JSON.stringify(body),
  };
}

async function draftAgent(
  path: string,
  body: unknown,
  operationKey: string,
): Promise<GuideDraftResponse> {
  const init = keyedJson(body, operationKey);
  const response = await f(path, init);
  const accepted = await ok<unknown>(response);
  if (response.status !== 200 || !accepted || typeof accepted !== "object" ||
    !("agent_id" in accepted) || typeof accepted.agent_id !== "number" ||
    !Number.isSafeInteger(accepted.agent_id) || accepted.agent_id <= 0) {
    throw new Error("Draft agent acceptance is unconfirmed");
  }
  return accepted as GuideDraftResponse;
}
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
  head: (path: string): Promise<Response> => f(path, { method: "HEAD" }),

  getTimeline: (
    agentId: number,
    opts?: { limit?: number; before?: string; signal?: AbortSignal },
  ): Promise<TimelineResponse> => {
    const params = new URLSearchParams();
    if (opts?.limit != null) params.set("limit", String(opts.limit));
    if (opts?.before != null) params.set("before", opts.before);
    const qs = params.toString();
    return f(`/api/agents/${agentId}/timeline${qs ? `?${qs}` : ""}`, { signal: opts?.signal }).then(
      ok<TimelineResponse>,
    );
  },

  getTokenUsage: (agentId: number, signal?: AbortSignal): Promise<TokenUsageResponse> => {
    return f(`/api/agents/${agentId}/token-usage`, { signal }).then(ok<TokenUsageResponse>);
  },

  getReplySuggestion: (agentId: number, signal: AbortSignal): Promise<ReplySuggestionResponse> => {
    return f(`/api/agents/${agentId}/reply-suggestion`, { ...POST, signal }).then(
      ok<ReplySuggestionResponse>,
    );
  },

  getConversationSnapshot: (
    agentId: number,
    signal?: AbortSignal,
  ): Promise<ConversationSnapshotResponse> => {
    return f(`/api/agents/${agentId}/conversation-snapshot`, { signal }).then(
      ok<ConversationSnapshotResponse>,
    );
  },

  getContextBreakdown: (agentId: number): Promise<ContextBreakdownResponse> => {
    return f(`/api/agents/${agentId}/context-breakdown`).then(ok<ContextBreakdownResponse>);
  },

  getRunTimeline: (
    agentId: number,
    options?: { from?: string; to?: string; recentHours?: number; maxUnits?: number; snapshot?: string; signal?: AbortSignal },
  ): Promise<RunTimelineResponse> => {
    const params = new URLSearchParams();
    if (options?.from != null) params.set("from", options.from);
    if (options?.to != null) params.set("to", options.to);
    if (options?.recentHours != null) params.set("recent_hours", String(options.recentHours));
    if (options?.maxUnits != null) params.set("max_units", String(options.maxUnits));
    if (options?.snapshot != null) params.set("snapshot", options.snapshot);
    const query = params.toString();
    return jsonWithTimeout<RunTimelineResponse>(
      `/api/agents/${agentId}/run-timeline${query ? `?${query}` : ""}`,
      {},
      INSPECT_REQUEST_TIMEOUT_MS,
      options?.signal,
      "run timeline",
    );
  },

  getRunTimelineLinks: (
    agents: readonly number[],
    window: { from: string; to: string },
  ): Promise<RunTimelineLinks> => {
    const params = new URLSearchParams({ agents: agents.join(","), from: window.from, to: window.to });
    return f(`/api/insights/run-timeline/links?${params.toString()}`).then(ok<RunTimelineLinks>);
  },

  getRunTimelineLinkContent: (
    ref: { inbound_id: number } | { notice_id: number },
  ): Promise<RunTimelineLinkContent> => {
    const params = new URLSearchParams(Object.entries(ref).map(([k, v]) => [k, String(v)]));
    return f(`/api/insights/run-timeline/link-content?${params.toString()}`).then(ok<RunTimelineLinkContent>);
  },

  getRunTimelineContext: (agentId: number, at: number, snapshot?: string): Promise<RunTimelineContext> => {
    const params = new URLSearchParams({ at: String(at) });
    if (snapshot !== undefined) params.set("snapshot", snapshot);
    return f(`/api/agents/${agentId}/run-timeline/context?${params.toString()}`).then(ok<RunTimelineContext>);
  },

  getRunTimelineMessages: (
    agentId: number,
    range: { start: number; end: number; limit?: number; full?: boolean; snapshot?: string },
  ): Promise<RunTimelineMessages> => {
    const params = new URLSearchParams({ start: String(range.start), end: String(range.end) });
    if (range.limit != null) params.set("limit", String(range.limit));
    if (range.full) params.set("full", "true");
    if (range.snapshot !== undefined) params.set("snapshot", range.snapshot);
    return f(`/api/agents/${agentId}/run-timeline/messages?${params.toString()}`).then(
      ok<RunTimelineMessages>,
    );
  },

  getAgentInspectStatistics: (
    agentId: number,
    hours?: number | null,
    signal?: AbortSignal,
  ): Promise<AgentInspectStatistics> => {
    const params = new URLSearchParams();
    if (hours != null) params.set("hours", String(hours));
    const qs = params.toString();
    return jsonWithTimeout<AgentInspectStatistics>(
      `/api/agents/${agentId}/inspect/statistics${qs ? `?${qs}` : ""}`,
      {},
      INSPECT_REQUEST_TIMEOUT_MS,
      signal,
      "Inspector request",
    );
  },

  getAgentInspectLive: (
    agentId: number,
    signal?: AbortSignal,
  ): Promise<AgentInspectLive> => {
    return jsonWithTimeout<AgentInspectLive>(
      `/api/agents/${agentId}/inspect/live`,
      {},
      INSPECT_REQUEST_TIMEOUT_MS,
      signal,
      "Inspector live request",
    );
  },

  getAgentInspectWidgets: (
    agentId: number,
    signal?: AbortSignal,
  ): Promise<InspectWidget[]> => {
    return jsonWithTimeout<InspectWidget[]>(
      `/api/agents/${agentId}/inspect/widgets`,
      {},
      INSPECT_REQUEST_TIMEOUT_MS,
      signal,
      "Inspector widgets request",
    );
  },

  getAgentShell: (agentId: number, sessionId: number, lines?: number): Promise<ShellCapture> => {
    const qs = lines != null ? `?lines=${lines}` : "";
    return f(`/api/agents/${agentId}/shell/${sessionId}${qs}`).then(ok<ShellCapture>);
  },

  getPendingMessages: (agentId: number, signal?: AbortSignal): Promise<PendingInbound[]> => {
    return f(`/api/agents/${agentId}/pending`, { signal }).then(ok<PendingInbound[]>);
  },

  getCommands: (agentId?: number | null): Promise<CommandItem[]> => {
    const qs = agentId != null ? `?agent_id=${encodeURIComponent(agentId)}` : "";
    return f(`/api/commands${qs}`).then(ok<CommandItem[]>);
  },

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

  resolveNotice: (
    agentId: number,
    noticeId: number,
    body: ResolveNoticeIn,
    idempotencyKey: string = newOperationKey(),
  ): Promise<{ status: string }> => {
    return f(
      `/api/keyed/v1/agents/${agentId}/notices/${noticeId}/resolve`,
      keyedJson(body, idempotencyKey),
    ).then(ok<{ status: string }>);
  },

  observeCompact: (agentId: number): Promise<CompactTarget> =>
    f(`/api/keyed/v1/agents/${agentId}/compact-target`).then(ok<CompactTarget>),

  compact: (target: CompactTarget, operationKey: string): Promise<CompactAcceptance> =>
    f(`/api/keyed/v1/agents/${target.source.agent_id}/compact-history`, keyedJson(target, operationKey))
      .then(ok<CompactAcceptance>),

  observeWork: (agentId: number): Promise<NativeWorkTarget> =>
    f(`/api/keyed/v1/agents/${agentId}/native-work`).then(ok<NativeWorkTarget>),

  cancel: (target: NativeWorkTarget, operationKey: string): Promise<NativeCancelAcceptance> =>
    f(`/api/keyed/v1/agents/${target.agent_id}/cancel-work`, keyedJson(target, operationKey))
      .then(ok<NativeCancelAcceptance>),

  getAgentRoster: (signal?: AbortSignal): Promise<AgentRoster> =>
    f("/api/agents/roster", { signal }).then(ok<WireAgentRoster>).then((roster) => ({
      agents: roster.agents.map(projectAgentStatus), ancestors: roster.ancestors,
    })),

  listAgents: (options: { scope?: "live" | "terminated" | "all"; query?: string; beforeId?: number; limit?: number; signal?: AbortSignal } = {}): Promise<AgentDirectoryPage> => {
    const params = new URLSearchParams({ scope: options.scope ?? "live", limit: String(options.limit ?? 100) });
    if (options.query) params.set("query", options.query);
    if (options.beforeId != null) params.set("before_id", String(options.beforeId));
    return f(`/api/agents?${params}`, { signal: options.signal }).then(ok<WireAgentDirectoryPage>)
      .then((page) => ({ ...page, agents: page.agents.map(projectAgentStatus) }));
  },

  getAgent: (agentId: number, signal?: AbortSignal): Promise<AgentRow> =>
    f(`/api/agents/${agentId}`, { signal }).then(ok<WireAgentRow>).then(projectAgentStatus),

  spawnAgent: (req: SpawnAgentRequest = {}, operationKey: string = newOperationKey()): Promise<SpawnedAgent> =>
    f("/api/keyed/v1/agents", keyedJson(req, operationKey)).then(ok<SpawnedAgent>),
  retryAgentLaunch: (agentId: number, priorAttempt: string, operationKey: string): Promise<RetryLaunchAccepted> =>
    f(`/api/keyed/v1/agents/${agentId}/retry-launch`, keyedJson({ expected_prior_attempt_id: priorAttempt }, operationKey))
      .then(ok<RetryLaunchAccepted>),

  terminateAgent: (agentId: number, force = false): Promise<TerminateAgentResponse> => {
    return f(
      `/api/agents/${agentId}/terminate`,
      force ? POST_JSON({ force: true }) : POST,
    ).then(ok<TerminateAgentResponse>);
  },

  forceExpireImpersonation: (agentId: number, sessionId: number): Promise<{ session_id: number; status: "expired" | "not_open" }> =>
    f(`/api/agents/${agentId}/impersonation/force-expire`, POST_JSON({ session_id: sessionId }))
      .then(ok<{ session_id: number; status: "expired" | "not_open" }>),

  restartAgent: (agentId: number): Promise<RestartAgentResponse> => {
    return f(`/api/agents/${agentId}/restart`, POST).then(ok<RestartAgentResponse>);
  },

  resurrectAgent: (
    agentId: number,
    prompt?: string,
  ): Promise<ResurrectAgentResponse> => {

    return f(
      `/api/agents/${agentId}/resurrect`,
      POST_JSON(prompt !== undefined ? { prompt } : {}),
    ).then(ok<ResurrectAgentResponse>);
  },

  patchAgentLabel: (agentId: number, label: string): Promise<void> => {
    return f(`/api/agents/${agentId}`, PATCH_JSON({ label })).then(async (res) => {
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status}: ${text || res.statusText}`);
      }
    });
  },

  getToUser: (agentId: number, signal?: AbortSignal, references: string[] = []): Promise<ToUserDocument> => {
    const query = new URLSearchParams(references.map((reference) => ["reference", reference]));
    return f(`/api/agents/${agentId}/to-user${query.size ? `?${query}` : ""}`, { signal }).then(ok<ToUserDocument>);
  },

  listPages: (agentId: number): Promise<PageRow[]> => {
    return f(`/api/agents/${agentId}/pages`).then(ok<PageRow[]>);
  },

  listAllPages: (): Promise<PageRow[]> => {
    return f(`/api/pages`).then(ok<PageRow[]>);
  },

  getFleetGraph: (opts?: { hours?: number; decayLambda?: number }): Promise<WireFleetGraph> => {
    const params = new URLSearchParams();
    if (opts?.hours != null) params.set("hours", String(opts.hours));
    if (opts?.decayLambda != null) params.set("decay_lambda", String(opts.decayLambda));
    const qs = params.toString();
    return f(`/api/fleet/graph${qs ? `?${qs}` : ""}`).then(ok<WireFleetGraph>);
  },

  getTasks: <T extends TaskFields = "full">(
    opts?: { window?: string; fields?: T },
  ): Promise<TaskListResponse<T extends "full" ? TaskRow : TaskSummaryRow>> => {
    const params = new URLSearchParams();
    if (opts?.window != null) params.set("window", opts.window);
    if (opts?.fields != null) params.set("fields", opts.fields);
    const qs = params.toString();
    return f(`/api/tasks${qs ? `?${qs}` : ""}`).then(
      ok<TaskListResponse<T extends "full" ? TaskRow : TaskSummaryRow>>,
    );
  },

  getMemoryGraph: (): Promise<MemoryGraphResponse> => {
    return f("/api/memory/graph").then(ok<MemoryGraphResponse>);
  },

  getMemoryNote: (path: string): Promise<MemoryNoteResponse> => {
    const q = new URLSearchParams({ path });
    return f(`/api/memory/note?${q.toString()}`).then(ok<MemoryNoteResponse>);
  },

  getStatsDashboard: (hours: number, signal?: AbortSignal): Promise<StatsDashboard> => {
    return jsonWithTimeout<StatsDashboard>(
      `/api/stats/dashboard?hours=${hours}`,
      {},
      40_000,
      signal,
      "stats dashboard",
    );
  },

  getAlertClasses: (hours: number, signal?: AbortSignal): Promise<AlertClassesResponse> => {
    return jsonWithTimeout<AlertClassesResponse>(
      `/api/stats/alert-classes?hours=${hours}`,
      {},
      40_000,
      signal,
      "alert classes",
    );
  },

  getAlertClassSamples: (
    cls: Pick<AlertClassRow, "level" | "event_name" | "source" | "process">,
    hours: number,
    signal?: AbortSignal,
  ): Promise<AlertClassSample[]> => {
    const q = new URLSearchParams({
      level: cls.level,
      event_name: cls.event_name,
      source: cls.source,
      process: cls.process,
      hours: String(hours),
    });
    return f(`/api/stats/alert-classes/samples?${q.toString()}`, { signal })
      .then(ok<{ samples: AlertClassSample[] }>)
      .then((body) => body.samples);
  },

  dismissAlertClass: (
    cls: Pick<AlertClassRow, "category" | "level" | "event_name" | "source" | "process">,
  ): Promise<EventResolutionRow> =>
    f("/api/event-resolutions", POST_JSON(cls)).then(ok<EventResolutionRow>),

  reopenAlertClass: (dismissalId: number): Promise<EventResolutionRow> =>
    f(`/api/event-resolutions/${dismissalId}/reopen`, POST).then(ok<EventResolutionRow>),

  getAlerts: (params: {
    window?: AlertsWindow;
    status?: string;
    severity?: string;
    limit?: number;
  } = {}): Promise<AlertsResponse> => {
    const q = new URLSearchParams();
    if (params.window) q.set("window", params.window);
    if (params.status) q.set("status", params.status);
    if (params.severity) q.set("severity", params.severity);
    if (params.limit) q.set("limit", String(params.limit));
    const qs = q.toString();
    return f(`/api/alerts${qs ? `?${qs}` : ""}`).then(ok<AlertsResponse>);
  },

  getConfig: (machine?: string): Promise<ConfigView> => {
    const qs = machine != null ? `?machine=${encodeURIComponent(machine)}` : "";
    return f(`/api/config${qs}`).then(ok<ConfigView>);
  },

  getResolvedConfig: (model?: string): Promise<ResolvedConfigView> => {
    const qs = model != null ? `?model=${encodeURIComponent(model)}` : "";
    return f(`/api/config/resolved${qs}`).then(ok<ResolvedConfigView>);
  },

  getMachines: (): Promise<AgentMachineRow[]> => {
    return f("/api/cluster/machines").then(ok<AgentMachineRow[]>);
  },

  getModels: (): Promise<ModelsResponse> => {
    return f("/api/models").then(ok<ModelsResponse>);
  },

  getDefaultModel: (): Promise<DefaultModelView> => {
    return f("/api/config/default-model").then(ok<DefaultModelView>);
  },

  putDefaultModel: (model: string): Promise<DefaultModelView> => {
    return f("/api/config/default-model", PUT_JSON({ model })).then(ok<DefaultModelView>);
  },

  submitUploadedFiles,

  uploadFiles: (
    agentId: number,
    files: File[],
    onProgress?: (pct: number) => void,
    operationKey: string = newOperationKey(),
  ): Promise<UploadedBatch> => {
    const keyed = keyedHeaders(operationKey);
    const formData = new FormData();
    for (const file of files) {
      formData.append("files", file, file.name);
    }
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", url(`/api/agents/${agentId}/uploads`));
      xhr.withCredentials = true;
      for (const [name, value] of Object.entries(keyed)) xhr.setRequestHeader(name, value);
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
          try { const err = JSON.parse(xhr.responseText) as { detail?: string }; if (err.detail) msg = err.detail; } catch { msg = `HTTP ${xhr.status}`; }
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

  getClusterStatus: (): Promise<ClusterStatus> => {
    return f("/api/cluster/status").then(ok<ClusterStatus>);
  },

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

  getInventory: (): Promise<InventoryAggregate> => {
    return f("/api/inventory").then(ok<InventoryAggregate>);
  },

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

  getSkills: (): Promise<SkillsView> => {
    return f("/api/skills").then(ok<SkillsView>);
  },

  putSkillEnabled: (name: string, enabled: boolean): Promise<SkillView> => {
    return f("/api/skills", PUT_JSON({ name, enabled })).then(ok<SkillView>);
  },

  login: (username: string, password: string): Promise<{ ok: boolean }> => {
    return f("/api/auth/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ username, password }),
    }).then(ok<{ ok: boolean }>);
  },

  logout: (): Promise<{ ok: boolean }> => {
    return f("/api/auth/logout", { method: "POST" }).then(
      ok<{ ok: boolean }>,
    );
  },

  checkAuth: (): Promise<{ authenticated: boolean }> => {
    return f("/api/auth/check").then(ok<{ authenticated: boolean }>);
  },

  listSchedules: (): Promise<ScheduleSummary[]> => {
    return f("/api/schedules").then(ok<ScheduleSummary[]>);
  },

  getSchedule: (id: number): Promise<ScheduleView> => {
    return f(`/api/schedules/${id}`).then(ok<ScheduleView>);
  },

  createSchedule: (body: ScheduleCreate, operationKey: string = newOperationKey()): Promise<ScheduleView> => {
    return f("/api/schedules", keyedJson(body, operationKey)).then(ok<ScheduleView>);
  },

  updateSchedule: (id: number, body: ScheduleUpdate, operationKey: string = newOperationKey()): Promise<ScheduleView> => {
    return f(`/api/schedules/${id}`, {
      method: "PUT",
      headers: { "content-type": "application/json", ...keyedHeaders(operationKey) },
      body: JSON.stringify(body),
    }).then(ok<ScheduleView>);
  },

  deleteSchedule: (id: number): Promise<{ status: string }> => {
    return f(`/api/schedules/${id}`, { method: "DELETE" }).then(ok<{ status: string }>);
  },

  startSchedule: (id: number, operationKey: string = newOperationKey()): Promise<ScheduleView> => {
    return f(`/api/schedules/${id}/start`, { ...POST, headers: keyedHeaders(operationKey) }).then(ok<ScheduleView>);
  },

  stopSchedule: (id: number, operationKey: string = newOperationKey()): Promise<ScheduleView> => {
    return f(`/api/schedules/${id}/stop`, { ...POST, headers: keyedHeaders(operationKey) }).then(ok<ScheduleView>);
  },

  restartSchedule: (id: number, operationKey: string = newOperationKey()): Promise<ScheduleView> => {
    return f(`/api/schedules/${id}/restart`, { ...POST, headers: keyedHeaders(operationKey) }).then(ok<ScheduleView>);
  },

  scheduleLogs: (id: number, lines = 200): Promise<ScheduleLogsView> => {
    return f(`/api/schedules/${id}/logs?lines=${lines}`).then(ok<ScheduleLogsView>);
  },

  scheduleRuns: (id: number, limit = 50): Promise<ScheduleRunView[]> => {
    return f(`/api/schedules/${id}/runs?limit=${limit}`).then(ok<ScheduleRunView[]>);
  },

  draftSchedule: (nl: string, operationKey: string = newOperationKey()): Promise<ScheduleDraftResponse> => {
    return draftAgent("/api/keyed/v1/schedules/draft", { nl }, operationKey);
  },

  draftGuide: (nl: string, operationKey: string = newOperationKey()): Promise<GuideDraftResponse> => {
    return draftAgent("/api/keyed/v1/guide/draft", { nl }, operationKey);
  },

  draftPackage: (kind: PackageKind, nl: string, operationKey: string = newOperationKey()): Promise<PackageDraftResponse> => {
    return draftAgent("/api/keyed/v1/packages/draft", { kind, nl }, operationKey);
  },

  getSettings: (): Promise<UserSettingListResponse> => {
    return f("/api/settings").then(ok<UserSettingListResponse>);
  },

  putSetting: (key: string, value: unknown): Promise<UserSettingRow> => {
    return f(`/api/settings/${encodeURIComponent(key)}`, PUT_JSON({ value })).then(
      ok<UserSettingRow>,
    );
  },

  getUiContributions: (): Promise<UiContributionsResponse> => {
    return f("/api/ui/contributions").then(ok<UiContributionsResponse>);
  },
};
