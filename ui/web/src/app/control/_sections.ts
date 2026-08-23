// The section model shared by the two vertical anchor-nav pages — Control
// (the write/manage surface) and Insights (the read-only observability
// surface). Each list is the single ordered source for both a page's left
// anchor-nav and its section wrappers on the right. `id` is the element id
// (URL hash + scroll target); `subs` are second-level anchors within a section.
//
// Config / Display carry static sub-anchors (each sub id must have a
// matching element id in that section's page). Presets' subs are dynamic — one
// per preset, derived from the presets query cache in _nav.tsx via
// `presetAnchorId` — so they are NOT listed here.
//
// The two lists never overlap: Status + Metrics live only in Insights, the
// remaining sections only in Control — see CONTROL_SECTIONS / INSIGHTS_SECTIONS
// below.

// Element ids of each page's scroll container — the scroll-spy attaches its
// listener here and measures anchor positions relative to it. Distinct ids so
// the (identical) nav component targets the right container per page.
export const CONTROL_SCROLL_ID = "control-scroll";
export const INSIGHTS_SCROLL_ID = "insights-scroll";

// Query key of the presets list — shared by the Presets section (which fetches
// it) and the nav (which only reads the cache to build Presets' sub-links).
export const PRESETS_QUERY_KEY = ["presets"] as const;

export interface ControlSubSection {
  id: string;
  label: string;
  /** i18n key under the "control.sections" namespace — the nav/page render
   *  the translated label when present, falling back to `label` (English). */
  labelKey?: string;
}

export interface ControlSectionDef {
  id: string;
  label: string;
  /** i18n key under the "control.sections" namespace (same fallback rule). */
  labelKey?: string;
  subs?: ControlSubSection[];
}

/** Anchor id for one preset's card — shared by the nav's dynamic sub-links and
 *  the card elements on the Presets page so they can never drift apart. */
export function presetAnchorId(name: string): string {
  return `preset-${name}`;
}

// Render + nav order for Control (the write/manage surface). Guide leads —
// "ask Ava to run the cluster" — then the config/policy surfaces, then the
// per-host extension inventories (Plugins / MCP / Skills) and Schedules.
// Status + Ops are NOT here; they live on Insights (INSIGHTS_SECTIONS).
export const CONTROL_SECTIONS: ControlSectionDef[] = [
  { id: "guide", label: "Guide", labelKey: "guide" },
  {
    id: "config",
    label: "Config",
    labelKey: "config",
    subs: [
      // Neither of these is a field bucket (see _config_groups.ts): the default
      // model edits a DB row rather than a `.env` field, and the per-model view is
      // read-only. Both render their own element above the editable groups.
      { id: "config-default-model", label: "Default model", labelKey: "config-default-model" },
      { id: "config-per-model", label: "Per-model values", labelKey: "config-per-model" },
      { id: "config-llm", label: "LLM settings", labelKey: "config-llm" },
      { id: "config-prompts", label: "System prompts", labelKey: "config-prompts" },
      { id: "config-exec", label: "Agent execution", labelKey: "config-exec" },
      { id: "config-memory", label: "Agent memory & compact", labelKey: "config-memory" },
      { id: "config-sdk", label: "Agent SDK", labelKey: "config-sdk" },
      { id: "config-agent-infra", label: "Agent DB & infra", labelKey: "config-agent-infra" },
      { id: "config-daemon-heartbeat", label: "Daemon: heartbeat & hibernate", labelKey: "config-daemon-heartbeat" },
      { id: "config-daemon-tasks", label: "Daemon: tasks & events", labelKey: "config-daemon-tasks" },
      { id: "config-gateway", label: "Gateway", labelKey: "config-gateway" },
      { id: "config-connection", label: "Connection", labelKey: "config-connection" },
      { id: "config-dataplane", label: "Data plane", labelKey: "config-dataplane" },
      { id: "config-security", label: "Security & secrets", labelKey: "config-security" },
      { id: "config-general", label: "Display & general", labelKey: "config-general" },
      { id: "config-observability", label: "Observability", labelKey: "config-observability" },
      { id: "config-web", label: "Web & Telegram", labelKey: "config-web" },
      { id: "config-services", label: "Services", labelKey: "config-services" },
      { id: "config-health", label: "Health probes", labelKey: "config-health" },
    ],
  },
  { id: "presets", label: "Presets", labelKey: "presets" },
  {
    id: "display",
    label: "Display",
    labelKey: "display",
    subs: [
      { id: "display-agent-list", label: "Agent list display", labelKey: "display-agent-list" },
      { id: "display-timeline", label: "Timeline", labelKey: "display-timeline" },
      { id: "display-context-bar", label: "Context usage bar", labelKey: "display-context-bar" },
      { id: "display-notifications", label: "Notifications", labelKey: "display-notifications" },
      { id: "display-confirmations", label: "Confirmations", labelKey: "display-confirmations" },
      { id: "display-model-picker", label: "Model picker", labelKey: "display-model-picker" },
    ],
  },
  { id: "plugins", label: "Plugins", labelKey: "plugins" },
  { id: "mcp", label: "MCP", labelKey: "mcp" },
  { id: "skills", label: "Skills", labelKey: "skills" },
  { id: "schedules", label: "Schedules", labelKey: "schedules" },
  { id: "okf-graph", label: "OKF graph", labelKey: "okf-graph" },
];

// Render + nav order for Insights (the read-only observability surface). Status
// leads — "how is the cluster right now" is the most common reason to open it —
// followed by Ops (the Grafana dashboard link). The standalone Metrics
// section was retired 2026-08-04 (user ruling): its panels are replaced by
// Grafana, and old Metrics deep links forward to the Ops section — see
// RETIRED_INSIGHTS_ANCHORS below.
// The cluster Restart / Update actions ride the Status section header (see
// status/page.tsx), so "watch health → act" stays one glance.
export const INSIGHTS_SECTIONS: ControlSectionDef[] = [
  {
    id: "status",
    label: "Status",
    labelKey: "status",
    subs: [
      { id: "status-services", label: "Services", labelKey: "status-services" },
      { id: "status-resources", label: "Resources", labelKey: "status-resources" },
      { id: "status-gateway-daemons", label: "Gateway daemons", labelKey: "status-gateway-daemons" },
    ],
  },
  {
    id: "ops",
    label: "Ops",
    labelKey: "ops",
    subs: [
      { id: "ops-metrics", label: "Metrics (Grafana)", labelKey: "ops-metrics" },
    ],
  },
  {
    id: "alerts",
    label: "Alerts",
    labelKey: "alerts",
  },
];

// Anchor ids of the retired Metrics section (2026-08-04). Old
// /control#metrics / #metrics-* and /insights#metrics-* deep links are
// forwarded to the Ops section — the Grafana dashboard link that replaced
// them. Shared by the Control page's forward effect and the Insights page's
// initial-scroll effect so the two pages can never drift apart.
export const RETIRED_INSIGHTS_ANCHORS = new Set([
  "metrics",
  "metrics-syntax-fix",
  "metrics-exec",
  "metrics-llm-turns",
  "metrics-agent-activity",
  "metrics-sdk-usage",
  "metrics-per-agents",
]);

/** Document-ordered anchor ids (sections and their subs interleaved) for a
 *  (possibly dynamically-augmented) section list — drives scroll-spy's "which
 *  anchor is active" pick. The nav calls this on CONTROL_SECTIONS with
 *  Presets' dynamic subs merged in. */
export function controlAnchorIds(sections: readonly ControlSectionDef[]): string[] {
  return sections.flatMap((s) => [s.id, ...(s.subs?.map((sub) => sub.id) ?? [])]);
}
