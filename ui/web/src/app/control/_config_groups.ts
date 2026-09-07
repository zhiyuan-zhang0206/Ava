// Frontend-side display grouping + tag derivation for the Config section.
//
// The backend serves 11 coarse domain groups (LLM / Agent / Daemon / …); the
// Control page displays 17 finer semantic groups (per the approved control
// prototype). The regrouping is pure presentation, so it lives here as a
// static env-var → group map rather than an API change. Fields the map does
// not know (added after this file) fall back to a per-backend-group default,
// and a completely unknown backend group lands in a trailing "Other" bucket —
// nothing ever disappears from the panel.
//
// Group ids double as element ids / URL anchors and MUST match the Config
// sub-entries in _sections.ts (the nav links jump to them).

import type { ConfigFieldView } from "@/lib/types";

import { CONTROL_SECTIONS } from "./_sections";

export const OTHER_GROUP_ID = "config-other";

// The per-model resolution view is a nav sub-entry but NOT a field bucket: it
// renders its own element under this id (_per_model.tsx), so it is excluded
// from the field-group list below — otherwise the id would exist twice and the
// nav anchor would land on whichever came first.
export const PER_MODEL_GROUP_ID = "config-per-model";

// Same deal: the cluster default-model control renders its own element
// (_default_model.tsx) and edits a DB row, not a `.env` field, so it is a nav
// sub-entry but never a field bucket.
export const DEFAULT_MODEL_GROUP_ID = "config-default-model";

// Display order INSIDE each group is the array order here (the prototype's
// curated order); unmapped fields append after, alphabetically.
// Exported for the consistency test (HIDDEN_ENV_VARS ∩ GROUP_ENV_VARS == ∅).
export const GROUP_ENV_VARS: Record<string, string[]> = {
  "config-llm": [
    "AVA_MODEL",
    "AVA_LABELER_MODEL",
    "AVA_REASONING_EFFORT",
    "AVA_DASHSCOPE_BASE_URL",
    "AVA_UNDERSTAND_TEXT_MODEL",
    "AVA_UNDERSTAND_MEDIA_MODEL",
    "AVA_UNDERSTAND_MEDIA_RESOLUTION",
    "AVA_UNDERSTAND_MEDIA_THINKING_LEVEL",
    "AVA_UNDERSTAND_MEDIA_BASE_URL",
    "AVA_LLM_STREAM_TTFT_TIMEOUT_SECONDS",
    "AVA_LLM_STREAM_INTER_CHUNK_TIMEOUT_SECONDS",
    "AVA_LLM_NON_STREAMING_FALLBACK_TIMEOUT_SECONDS",
    "AVA_LLM_RETRY_MAX_ATTEMPTS",
    "AVA_LLM_RETRY_INITIAL_INTERVAL_SECONDS",
    "AVA_LLM_RETRY_MAX_INTERVAL_SECONDS",
    "AVA_LLM_RETRY_MAX_CONSECUTIVE_SAME_ERROR",
    "AVA_LLM_FATAL_PROVIDER_ERROR_TYPES",
    "AVA_LLM_OVERRIDE",
  ],
  "config-prompts": [
    "AVA_SYSTEM_PROMPT_EXTRA",
    "AVA_SYSTEM_PROMPT_SDK_OVERVIEW",
    "AVA_SYSTEM_PROMPT_PREFER_SDK",
    "AVA_SYSTEM_PROMPT_CODEACT",
    "AVA_AGENT_COMMUNICATION_STYLE",
    "AVA_SYSTEM_PROMPT_MEMORY",
    "AVA_SYSTEM_PROMPT_CONCISENESS",
    "AVA_SYSTEM_PROMPT_REPORTING",
    "AVA_SYSTEM_PROMPT_CAUTION",
    "AVA_SYSTEM_PROMPT_ALIGN",
    "AVA_SYSTEM_PROMPT_DELEGATION_CHECK",
    "AVA_SYSTEM_PROMPT_CROSS_MACHINE_DELEGATION",
    "AVA_SYSTEM_PROMPT_FILE_DRIVEN_WORK",
    "AVA_SYSTEM_PROMPT_TEMPORAL",
    "AVA_SYSTEM_PROMPT_INVEST_FUTURE",
  ],
  "config-exec": [
    "AVA_EXEC_TIMEOUT_SECONDS",
    "AVA_EXEC_NODE_TIMEOUT_SECONDS",
    "AVA_EXEC_OUTPUT_MAX_CHARS",
    "AVA_EXEC_OUTPUT_ACCUMULATION_MAX_CHARS",
    "AVA_SYNTAX_FIX_RUFF_FORMAT",
    "AVA_MCP_CONNECT_TIMEOUT_SECONDS",
    "AVA_MCP_DAEMON_START_TIMEOUT_SECONDS",
    "AVA_MCP_DAEMON_STOP_TIMEOUT_SECONDS",
    "AVA_SECURITY_SCAN_ENABLED",
  ],
  "config-memory": [
    "AVA_AUTO_COMPACT_FRACTION",
    "AVA_COMPACT_REMINDER_FRACTION",
    "AVA_MEMORY_INDEX_INJECT",
    "AVA_MEMORY_PER_AGENT_INJECT",
    "AVA_MEMORY_PER_AGENT_INDEX_MAX_LINES",
    "AVA_PASSIVE_MEMORY_RECALL",
    "AVA_AGENT_REPLY_REMINDER_CADENCE",
  ],
  "config-sdk": [
    "AVA_SDK_DISABLE",
    "AVA_SDK_EXPAND",
    "AVA_SKILLS_TO_INJECT_INTO_SYSTEM_PROMPT",
    "AVA_SKILLS_TO_EXPAND_AT_START",
    "AVA_WORKSPACE_IN_SYSTEM_PROMPT",
    "AVA_COMMANDS_ENABLED",
  ],
  "config-agent-infra": [
    "AVA_DB_NOTIFY_WAIT_TIMEOUT_SECONDS",
    "AVA_NODE_STALL_DUMP_SECONDS",
    "AVA_DB_POOL_ACQUIRE_TIMEOUT_SECONDS",
  ],
  "config-daemon-heartbeat": [
    "AVA_HEARTBEAT_ENABLED",
    "AVA_HEARTBEAT_INTERVAL_SECONDS",
    "AVA_HEARTBEAT_IDLE_THRESHOLD_SECONDS",
  ],
  "config-daemon-tasks": [
    "AVA_AUTO_RESURRECT_ENABLED",
    "AVA_AUTO_RESURRECT_BACKOFF_SECONDS",
    "AVA_TASK_MAINTENANCE_ENABLED",
    "AVA_TASK_MAINTENANCE_INTERVAL_SECONDS",
    "AVA_TASK_REMINDER_BACKOFF_SECONDS",
    "AVA_TASK_ESCALATE_N",
    "AVA_EVENTS_MAINTENANCE_INTERVAL_SECONDS",
  ],
  "config-gateway": [
    "AVA_GATEWAY_HTTP_TIMEOUT_SECONDS",
    "AVA_SSE_DISCONNECT_POLL_SECONDS",
    "AVA_SSE_THROTTLE_RATE",
    "AVA_GATEWAY_RELOAD",
    "AVA_AUTH_MIDDLEWARE_ENABLED",
  ],
  "config-connection": [
    "AVA_GATEWAY_PORT",
    "AVA_GATEWAY_URL",
    "AVA_MACHINE_HOST",
    "AVA_MACHINE_SERVE_GATEWAY",
    "AVA_MACHINE_SERVE_AGENT_RUNNER",
    "AVA_TRUSTED_CIDRS",
  ],
  "config-dataplane": [
    "AVA_PGBOUNCER_ENABLED",
    "AVA_EVENTS_CHANNEL",
    "AVA_DB_SSLMODE",
    "AVA_DB_POOL_MIN_SIZE",
    "AVA_DB_POOL_MAX_SIZE",
  ],
  "config-security": [
    "AVA_DB_URL",
    "AVA_REDIS_URL",
    "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "MIMO_API_KEY",
    "MOONSHOT_API_KEY",
    "GLM_API_KEY",
    "DASHSCOPE_API_KEY",
  ],
  "config-general": [
    "AVA_HOME",
    "AVA_TIMEZONE",
    "AVA_MESSAGE_TIMESTAMPS",
    "AVA_MESSAGE_TIMESTAMP_WEEKDAY",
    "AVA_TIMELINE_COMPACT_HISTORY",
    "AVA_TRACK_BRANCH",
    "AVA_CLUSTER_REGISTRY",
    "AVA_MACHINE_NAME",
    "AVA_MACHINE_DESCRIPTION",
    "AVA_MEMORY_REMOTE",
    "AVA_MEMORY_KEEP_LOCAL",
    "AVA_CROSS_MACHINE_TRANSFER_BACKEND",
    "AVA_REQUIRE_GITHUB_PR",
  ],
  "config-observability": [
    "AVA_TRACE_ENABLED",
    "AVA_TRACE_TAGS",
    "AVA_TRACE_RETENTION_DAYS",
    "AVA_TELEMETRY_OTLP_ENABLED",
    "AVA_TELEMETRY_OTLP_ENDPOINT",
    "AVA_TELEMETRY_OTLP_PORT",
  ],
  "config-web": [
    "AVA_WEB_SEARCH_TIMEOUT_SECONDS",
    "AVA_WEB_FETCH_TIMEOUT_SECONDS",
    "AVA_WEB_MAX_RESULTS",
    "AVA_WEB_BRAVE_SEARCH_ENDPOINT",
    "BRAVE_API_KEY",
    "AVA_WEB_JINA_BASE_URL",
    "JINA_API_KEY",
    "AVA_TELEGRAM_BOT_TOKEN",
    "AVA_TELEGRAM_OWNER_ID",
  ],
  "config-services": [
    "AVA_BROWSER_ENABLED",
    "AVA_CHROME_BINARY",
    "AVA_BROWSER_CDP_PORT",
    "AVA_PERMISSIONS_HELPER_ENABLED",
    "AVA_PERMISSIONS_HELPER_PORT",
    "AVA_MILVUS_URI",
    "AVA_MILVUS_PORT",
    "AVA_MILVUS_DATA_DIR",
    "AVA_MEMORY_ROOT",
    "AVA_PROJECT_ROOT",
    "AVA_WATCHDOG_INTERVAL_SECONDS",
    "AVA_OPS_CONCURRENCY",
  ],
  "config-health": [
    "AVA_FRONTEND_HEALTHCHECK_URL",
    "AVA_GATEWAY_HEALTH_URL",
    "AVA_LABELER_HEALTH_URL",
    "AVA_HEARTBEAT_HEALTH_URL",
    "AVA_TASK_MAINTENANCE_HEALTH_URL",
    "AVA_EVENTS_MAINTENANCE_HEALTH_URL",
    "AVA_MEMORY_INDEXER_HEALTH_URL",
    "AVA_LABELER_HEALTH_PORT",
    "AVA_HEARTBEAT_HEALTH_PORT",
    "AVA_TASK_MAINTENANCE_HEALTH_PORT",
    "AVA_EVENTS_MAINTENANCE_HEALTH_PORT",
    "AVA_MEMORY_INDEXER_HEALTH_PORT",
    "AVA_OPS_HEALTH_PORT",
    "AVA_LABELER_PIDFILE",
    "AVA_HEARTBEAT_PIDFILE",
    "AVA_TASK_MAINTENANCE_PIDFILE",
    "AVA_EVENTS_MAINTENANCE_PIDFILE",
    "AVA_GATEWAY_PIDFILE",
    "AVA_GATEWAY_WATCHDOG_PIDFILE",
    "AVA_AGENT_RUNNER_WATCHDOG_PIDFILE",
    "AVA_MEMORY_INDEXER_PIDFILE",
    "AVA_OPS_PIDFILE",
  ],
};

// Fields deliberately absent from the panel even though the backend serves
// them. Admission criterion is editorial, not structural: a field belongs
// here only if a human should NOT reach for the panel to change it. Three
// buckets today:
//   - AVA_CLUSTER_SECRET: the cluster-wide pre-shared secret. Rotating it is a
//     multi-step out-of-band dance (every runner re-enrolled), never a panel
//     edit; hiding it also keeps it out of the write-only secret editor below.
//   - AVA_GATEWAY_MAX_RETRIES / AVA_GATEWAY_RETRY_DELAY_SECONDS: SDK→gateway
//     transport micro-tuning with no operator-facing consequence. For a genuine
//     need, ask the Ava Guide agent — it edits .env via the `ava` CLI.
//   - AVA_CONTAINER_EXEC / AVA_OUTPUT_DIR: eval-harness plumbing, set by the eval
//     driver (evals/driver_container.py, evals/__main__.py), never by an operator
//     — agent-scoped read-only env reads, not cluster config.
// Hiding a WRITABLE field is safe: it still rides in raw_overrides at its own
// current value (or the unchanged-sentinel for a secret), which the merge-patch
// PUT re-applies idempotently — hiding neither drops nor changes it.
// The hidden vars are NOT in GROUP_ENV_VARS either (no display-group home), so
// without this list they would surface through the backend-group fallback;
// membership is pinned by test — HIDDEN_ENV_VARS ∩ GROUP_ENV_VARS must stay
// empty so no field is doubly bookkept as "shown here, hidden there".
export const HIDDEN_ENV_VARS = new Set<string>([
  "AVA_CLUSTER_SECRET",
  "AVA_GATEWAY_MAX_RETRIES",
  "AVA_GATEWAY_RETRY_DELAY_SECONDS",
  "AVA_CONTAINER_EXEC",
  "AVA_OUTPUT_DIR",
]);

// A field not in the static map is grouped by its backend domain group. Every
// current backend group has a home; new backend groups land in "Other".
const BACKEND_GROUP_FALLBACK: Record<string, string> = {
  LLM: "config-llm",
  Sandbox: "config-exec",
  Agent: "config-exec",
  Web: "config-web",
  Gateway: "config-gateway",
  Daemon: "config-daemon-tasks",
  "Data plane": "config-dataplane",
  Services: "config-services",
  Observability: "config-observability",
  Telegram: "config-web",
  General: "config-general",
};

// (group id, label) in render order — labels come from _sections.ts so the nav
// links and the on-page headers can never disagree.
export const CONFIG_DISPLAY_GROUPS: { id: string; label: string }[] = [
  ...(CONTROL_SECTIONS.find((s) => s.id === "config")?.subs ?? []).filter(
    (s) => s.id !== PER_MODEL_GROUP_ID && s.id !== DEFAULT_MODEL_GROUP_ID,
  ),
  { id: OTHER_GROUP_ID, label: "Other" },
];

// env var → group id, inverted once from the per-group lists.
const ENV_TO_GROUP = new Map<string, string>(
  Object.entries(GROUP_ENV_VARS).flatMap(([gid, envs]) => envs.map((e) => [e, gid] as const)),
);

// env var → index within its group (the prototype's curated order).
const ENV_ORDER = new Map<string, number>(
  Object.values(GROUP_ENV_VARS).flatMap((envs) => envs.map((e, i) => [e, i] as const)),
);

/** Display group id for a field: static map > backend-group fallback > Other. */
export function displayGroupId(field: ConfigFieldView): string {
  const fallback = (BACKEND_GROUP_FALLBACK as Partial<Record<string, string>>)[field.group];
  return ENV_TO_GROUP.get(field.env_var) ?? fallback ?? OTHER_GROUP_ID;
}

/** Curated position of a field inside its display group; unmapped fields sort
 *  after every mapped one (then alphabetically, applied by the caller). */
export function displayOrder(field: ConfigFieldView): number {
  return ENV_ORDER.get(field.env_var) ?? Number.MAX_SAFE_INTEGER;
}

/** Display label from the env var: strip the AVA_ prefix and turn underscores
 *  into spaces, preserving the original case so abbreviations stay intact
 *  (AVA_MODEL → "MODEL", AVA_LLM_OVERRIDE → "LLM OVERRIDE", DEEPSEEK_API_KEY →
 *  "DEEPSEEK API KEY"). No case conversion — env vars are uppercase by
 *  convention, so labels read uppercase and never mangle LLM/SSE/URL/CIDR. */
export function fieldLabel(envVar: string): string {
  return envVar.replace(/^AVA_/, "").split("_").filter(Boolean).join(" ");
}

/** Whether individual agents can override this field via spawn/restart
 *  config overlay — drives the "per-agent" tag and the per-agent filter.
 *  Served by the backend as `ConfigFieldView.per_agent` (the same flag the
 *  per-model resolution view uses); no scope-approximation or extra-env table. */
export function isPerAgent(field: ConfigFieldView): boolean {
  return field.per_agent;
}

/** Whether the field is flagged deprecated (description convention). */
export function isDeprecated(field: ConfigFieldView): boolean {
  return field.description.startsWith("DEPRECATED");
}

/**
 * Render a config value for display — shared by the editable field rows and the
 * per-model resolution view so one value never reads two ways.
 *
 * Values arrive typed `object` on the wire (`shared/api_contracts/config.py`),
 * so a field the backend ever grows a structured (list/dict) value for lands
 * here as-is. `String()` on a plain object is always the content-free
 * "[object Object]"; stringify it instead so such a field still shows something
 * readable rather than that literal.
 */
export function formatConfigValue(v: unknown): string {
  if (v == null || v === "") return "(empty)";
  if (typeof v === "object") {
    try {
      return JSON.stringify(v);
    } catch {
      // Circular structure — no better fallback than the base toString.
      // eslint-disable-next-line @typescript-eslint/no-base-to-string
      return String(v);
    }
  }
  // Every remaining `unknown` case (string/number/boolean/bigint/etc.) is a
  // primitive whose toString is its own meaningful representation, not the
  // object default — the lint rule can't see that the `object` branch above
  // already returned.
  // eslint-disable-next-line @typescript-eslint/no-base-to-string
  return String(v);
}

// The tag-filter bar's options, in display order. "all" is the reset pseudo-tag.
export const CONFIG_FILTERS: { id: string; label: string }[] = [
  { id: "all", label: "All" },
  { id: "runtime", label: "Runtime" },
  { id: "cli-only", label: "CLI-only" },
  { id: "startup", label: "Startup" },
  { id: "per-agent", label: "Per-agent" },
  { id: "sensitive", label: "Sensitive" },
  { id: "cluster-pinned", label: "Cluster-pinned" },
  { id: "cluster-default", label: "Cluster-default" },
  { id: "host", label: "Host" },
  { id: "agent", label: "Agent" },
];

/** The filterable tag set of a field. `editable` is the row's effective
 *  editability under the current machine selection. A writable secret (an API
 *  key) is editable write-only, so it tags "runtime" + "sensitive"; a
 *  non-writable secret (db_url) tags "cli-only" + "sensitive". */
export function fieldFilterTags(field: ConfigFieldView, editable: boolean): Set<string> {
  const tags = new Set<string>([field.scope, editable ? "runtime" : "cli-only"]);
  if (field.restart_required) tags.add("startup");
  if (isPerAgent(field)) tags.add("per-agent");
  if (field.sensitive) tags.add("sensitive");
  return tags;
}
