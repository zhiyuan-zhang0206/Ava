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

import type { ConfigFieldView } from "@/lib/contracts/types";

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
    "MEIVA_MODEL",
    "MEIVA_LABELER_MODEL",
    "MEIVA_HIERARCHY_MODEL",
    "MEIVA_REASONING_EFFORT",
    "MEIVA_DASHSCOPE_BASE_URL",
    "MEIVA_UNDERSTAND_TEXT_MODEL",
    "MEIVA_UNDERSTAND_MEDIA_MODEL",
    "MEIVA_UNDERSTAND_MEDIA_RESOLUTION",
    "MEIVA_UNDERSTAND_MEDIA_THINKING_LEVEL",
    "MEIVA_UNDERSTAND_MEDIA_BASE_URL",
    "MEIVA_LLM_STREAM_TTFT_TIMEOUT_SECONDS",
    "MEIVA_LLM_STREAM_INTER_CHUNK_TIMEOUT_SECONDS",
    "MEIVA_LLM_RETRY_MAX_ATTEMPTS",
    "MEIVA_LLM_RETRY_INITIAL_INTERVAL_SECONDS",
    "MEIVA_LLM_RETRY_MAX_INTERVAL_SECONDS",
    "MEIVA_LLM_FATAL_PROVIDER_ERROR_TYPES",
    "MEIVA_LLM_OVERRIDE",
  ],
  "config-prompts": [
    "MEIVA_SYSTEM_PROMPT_EXTRA",
    "MEIVA_SYSTEM_PROMPT_SDK_OVERVIEW",
    "MEIVA_SYSTEM_PROMPT_PREFER_SDK",
    "MEIVA_SYSTEM_PROMPT_CODEACT",
    "MEIVA_SYSTEM_PROMPT_SEQUENTIAL_TOOL_CALLS",
    "MEIVA_AGENT_COMMUNICATION_STYLE",
    "MEIVA_SYSTEM_PROMPT_MEMORY",
    "MEIVA_SYSTEM_PROMPT_CONCISENESS",
    "MEIVA_SYSTEM_PROMPT_REPORTING",
    "MEIVA_SYSTEM_PROMPT_CAUTION",
    "MEIVA_SYSTEM_PROMPT_ALIGN",
    "MEIVA_SYSTEM_PROMPT_DELEGATION_CHECK",
    "MEIVA_SYSTEM_PROMPT_CROSS_MACHINE_DELEGATION",
    "MEIVA_SYSTEM_PROMPT_FILE_DRIVEN_WORK",
    "MEIVA_SYSTEM_PROMPT_TEMPORAL",
    "MEIVA_SYSTEM_PROMPT_INVEST_FUTURE",
  ],
  "config-exec": [
    "MEIVA_EXEC_TIMEOUT_SECONDS",
    "MEIVA_EXEC_NODE_TIMEOUT_SECONDS",
    "MEIVA_EXEC_OUTPUT_MAX_CHARS",
    "MEIVA_EXEC_OUTPUT_ACCUMULATION_MAX_CHARS",
    "MEIVA_SYNTAX_FIX_RUFF_FORMAT",
    "MEIVA_MCP_CONNECT_TIMEOUT_SECONDS",
    "MEIVA_SECURITY_SCAN_ENABLED",
  ],
  "config-memory": [
    "MEIVA_AUTO_COMPACT_FRACTION",
    "MEIVA_COMPACT_REMINDER_FRACTION",
    "MEIVA_MEMORY_INDEX_INJECT",
    "MEIVA_MEMORY_PER_AGENT_INJECT",
    "MEIVA_MEMORY_PER_AGENT_INDEX_MAX_LINES",
    "MEIVA_PASSIVE_MEMORY_RECALL",
    "MEIVA_AGENT_REPLY_REMINDER_CADENCE",
  ],
  "config-sdk": [
    "MEIVA_SDK_DISABLE",
    "MEIVA_SDK_EXPAND",
    "MEIVA_SKILLS_TO_INJECT_INTO_SYSTEM_PROMPT",
    "MEIVA_SKILLS_TO_EXPAND_AT_START",
    "MEIVA_WORKSPACE_IN_SYSTEM_PROMPT",
    "MEIVA_COMMANDS_ENABLED",
  ],
  "config-agent-infra": [
    "MEIVA_DB_NOTIFY_WAIT_TIMEOUT_SECONDS",
    "MEIVA_NODE_STALL_DUMP_SECONDS",
    "MEIVA_DB_POOL_ACQUIRE_TIMEOUT_SECONDS",
  ],
  "config-daemon-heartbeat": [
    "MEIVA_HEARTBEAT_ENABLED",
    "MEIVA_HEARTBEAT_INTERVAL_SECONDS",
    "MEIVA_HEARTBEAT_IDLE_THRESHOLD_SECONDS",
  ],
  "config-daemon-tasks": [
    "MEIVA_EVENTS_MAINTENANCE_INTERVAL_SECONDS",
  ],
  "config-gateway": [
    "MEIVA_GATEWAY_HTTP_TIMEOUT_SECONDS",
    "MEIVA_SSE_DISCONNECT_POLL_SECONDS",
    "MEIVA_SSE_THROTTLE_RATE",
    "MEIVA_GATEWAY_RELOAD",
  ],
  "config-connection": [
    "MEIVA_GATEWAY_PORT",
    "MEIVA_GATEWAY_URL",
    "MEIVA_MACHINE_HOST",
    "MEIVA_MACHINE_SERVE_GATEWAY",
    "MEIVA_MACHINE_SERVE_AGENT_RUNNER",
    "MEIVA_TRUSTED_CIDRS",
  ],
  "config-dataplane": [
    "MEIVA_PGBOUNCER_ENABLED",
    "MEIVA_EVENTS_CHANNEL",
    "MEIVA_DB_SSLMODE",
    "MEIVA_DB_POOL_MIN_SIZE",
    "MEIVA_DB_POOL_MAX_SIZE",
  ],
  "config-security": [
    "MEIVA_DB_URL",
    "MEIVA_REDIS_URL",
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
    "MEIVA_TIMEZONE",
    "MEIVA_MESSAGE_TIMESTAMPS",
    "MEIVA_MESSAGE_TIMESTAMP_WEEKDAY",
    "MEIVA_TIMELINE_COMPACT_HISTORY",
    "MEIVA_TRACK_BRANCH",
    "MEIVA_MACHINE_NAME",
    "MEIVA_MACHINE_DESCRIPTION",
    "MEIVA_MEMORY_REMOTE",
    "MEIVA_MEMORY_KEEP_LOCAL",
    "MEIVA_CROSS_MACHINE_TRANSFER_BACKEND",
    "MEIVA_REQUIRE_GITHUB_PR",
  ],
  "config-observability": [
    "MEIVA_TRACE_ENABLED",
    "MEIVA_TRACE_RETENTION_DAYS",
    "MEIVA_TELEMETRY_OTLP_ENABLED",
    "MEIVA_TELEMETRY_OTLP_ENDPOINT",
    "MEIVA_TELEMETRY_OTLP_PORT",
  ],
  "config-web": [
    "MEIVA_WEB_SEARCH_PROVIDER",
    "MEIVA_WEB_SEARCH_TIMEOUT_SECONDS",
    "MEIVA_WEB_MAX_RESULTS",
    "MEIVA_WEB_BRAVE_SEARCH_ENDPOINT",
    "BRAVE_API_KEY",
    "MEIVA_WEB_DEEPSEEK_SEARCH_ENDPOINT",
    "MEIVA_WEB_DEEPSEEK_MAX_RESULTS",
    "MEIVA_WEB_DEEPSEEK_QPS",
    "MEIVA_WEB_FETCH_READER",
    "MEIVA_WEB_FETCH_TIMEOUT_SECONDS",
    "MEIVA_WEB_JINA_BASE_URL",
    "JINA_API_KEY",
    "MEIVA_WEB_DEEPSEEK_FETCH_ENDPOINT",
    "MEIVA_TELEGRAM_BOT_TOKEN",
    "MEIVA_TELEGRAM_OWNER_ID",
  ],
  "config-services": [
    "MEIVA_BROWSER_ENABLED",
    "MEIVA_CHROME_BINARY",
    "MEIVA_BROWSER_CDP_PORT",
    "MEIVA_PERMISSIONS_HELPER_ENABLED",
    "MEIVA_PERMISSIONS_HELPER_PORT",
    "MEIVA_MEMORY_ROOT",
    "MEIVA_PROJECT_ROOT",
    "MEIVA_OPS_CONCURRENCY",
  ],
  "config-health": [
    "MEIVA_FRONTEND_HEALTHCHECK_URL",
    "MEIVA_GATEWAY_HEALTH_URL",
    "MEIVA_LABELER_HEALTH_PORT",
    "MEIVA_HEARTBEAT_HEALTH_PORT",
    "MEIVA_TASK_MAINTENANCE_HEALTH_PORT",
    "MEIVA_EVENTS_MAINTENANCE_HEALTH_PORT",
    "MEIVA_MEMORY_INDEXER_HEALTH_PORT",
    "MEIVA_OPS_HEALTH_PORT",
    "MEIVA_GATEWAY_PIDFILE",
  ],
};

// Fields deliberately absent from the panel even though the backend serves
// them. Admission criterion is editorial, not structural: a field belongs
// here only if a human should NOT reach for the panel to change it. Two
// buckets today:
//   - MEIVA_CLUSTER_SECRET: the gateway's human bearer. The backend serves it
//     read-only (no config write may choose it); it rotates only through
//     scripts/data_plane_ops/rotate_cluster_secret.py, which re-issues every remote unit's
//     bundle, so the panel has nothing to show for it.
//   - MEIVA_GATEWAY_MAX_RETRIES / MEIVA_GATEWAY_RETRY_DELAY_SECONDS: SDK→gateway
//     transport micro-tuning with no operator-facing consequence. For a genuine
//     need, ask the Meiva Guide agent — it edits .env via the `meiva` CLI.
// Hiding a WRITABLE field is safe: it still rides in raw_overrides at its own
// current value (or the unchanged-sentinel for a secret), which the merge-patch
// PUT re-applies idempotently — hiding neither drops nor changes it.
// The hidden vars are NOT in GROUP_ENV_VARS either (no display-group home), so
// without this list they would surface through the backend-group fallback;
// membership is pinned by test — HIDDEN_ENV_VARS ∩ GROUP_ENV_VARS must stay
// empty so no field is doubly bookkept as "shown here, hidden there".
export const HIDDEN_ENV_VARS = new Set<string>([
  "MEIVA_CLUSTER_SECRET",
  "MEIVA_GATEWAY_MAX_RETRIES",
  "MEIVA_GATEWAY_RETRY_DELAY_SECONDS",
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
  // Display windows/limits (backend domain "Display", task #3696) — the
  // "Display & general" bucket is their home.
  Display: "config-general",
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

/** Display label from the env var: strip the MEIVA_ prefix and turn underscores
 *  into spaces, preserving the original case so abbreviations stay intact
 *  (MEIVA_MODEL → "MODEL", MEIVA_LLM_OVERRIDE → "LLM OVERRIDE", DEEPSEEK_API_KEY →
 *  "DEEPSEEK API KEY"). No case conversion — env vars are uppercase by
 *  convention, so labels read uppercase and never mangle LLM/SSE/URL/CIDR. */
export function fieldLabel(envVar: string): string {
  return envVar.replace(/^MEIVA_/, "").split("_").filter(Boolean).join(" ");
}

/** Whether individual agents can override this field via spawn/restart
 *  config overlay — drives the "per-agent" tag and the per-agent filter.
 *  Served by the backend as `ConfigFieldView.per_agent` (the same flag the
 *  per-model resolution view uses); no scope-approximation or extra-env table. */
export function isPerAgent(field: ConfigFieldView): boolean {
  return field.per_agent;
}

/**
 * Render a config value for display — shared by the editable field rows and the
 * per-model resolution view so one value never reads two ways.
 *
 * Values arrive typed `object` on the wire (`base/api_contracts/config.py`),
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
