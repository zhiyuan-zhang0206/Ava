"use client";

// system_marker classification + rendering. A framework system note carries its
// category as `source` (a NoteTag): lifecycle transitions, memory recall, and
// guidance notes each map to a MessageCard (icon + title header + collapsible
// payload body) through classifyMarker + markerVisual. The payload-identified
// ephemeral markers (compact_done / cancelled / compact_request / error) and the
// unrecognized-marker red alarm render bare (no card) through EphemeralSystemMarker.
//
// LifecycleTag mirrors the backend agent/messages.py NoteTag lifecycle_* members.
// When the backend adds a new lifecycle tag without updating here, classifyMarker
// routes it to the ephemeral path and it surfaces as the red UnknownMarkerChip —
// fail-loud instead of a silent fallback.

import { GitFork, Info, NotebookText, PowerOff, RotateCw, type LucideIcon } from "lucide-react";
import { useTranslations } from "next-intl";

import { cn } from "@/lib/utils";

export const LIFECYCLE_TAGS = [
  "lifecycle_terminate",
  "lifecycle_restart",
  "lifecycle_resurrect",
  "lifecycle_fork",
] as const;

export type LifecycleTag = (typeof LIFECYCLE_TAGS)[number];

export function isLifecycleTag(s: string): s is LifecycleTag {
  return (LIFECYCLE_TAGS as readonly string[]).includes(s);
}

// Card-rendered marker families. Exported so the marker-contract test can
// iterate the exact dispatch sets the renderer uses — the backend NoteTag
// enum (shared/message_kwargs.py) is asserted to be a subset of
// LIFECYCLE_TAGS ∪ MEMORY_SOURCES ∪ NOTE_SOURCES by tests/test_lint_marker_contract.py.
export const MEMORY_SOURCES = new Set(["memory", "agent_memory"]);

// The three card-rendered marker families + the bare-rendered ephemeral catch-all.
export type MarkerClass =
  | { readonly kind: "lifecycle"; readonly tag: LifecycleTag }
  | { readonly kind: "memory" }
  | { readonly kind: "note" }
  | { readonly kind: "ephemeral" };

export const NOTE_SOURCES = new Set([
  "sdk_hint",
  "agent_reply",
  "task",
  "compact_reminder",
  "history_dump",
  "silent_idle_continue",
  "heartbeat",
  "heartbeat_pause",
  "security",
  "context",
  "agent_id",
  "exec_timeout",
  "timezone",
  "project_skills",
  "preloaded_skills",
  "new_skills",
]);

// Classify a system_marker by its source. The backend tag set is closed — a
// source matching none is treated as not-a-note (ephemeral), which then falls to
// the payload-based markers and finally the red alarm.
export function classifyMarker(source: string | null): MarkerClass {
  if (source !== null && isLifecycleTag(source)) return { kind: "lifecycle", tag: source };
  if (source !== null && MEMORY_SOURCES.has(source)) return { kind: "memory" };
  if (source !== null && NOTE_SOURCES.has(source)) return { kind: "note" };
  return { kind: "ephemeral" };
}

export interface MarkerVisual {
  readonly icon: LucideIcon;
  readonly title: string;
  /** i18n key under the "markers" namespace — the card header renders the
   *  translated title when present, falling back to `title` (English). */
  readonly titleKey?: string;
  // Header title color + style (uppercase tracking, to read as an event label).
  readonly titleClass: string;
  readonly border: string;
  readonly bg: string;
  // Card-wide text color the payload body inherits.
  readonly text: string;
}

const LABEL_CLS = "uppercase tracking-widest";

const LIFECYCLE_VISUAL: Record<LifecycleTag, MarkerVisual> = {
  lifecycle_terminate: {
    icon: PowerOff,
    title: "Terminated",
    titleKey: "terminated",
    titleClass: `text-rose-700 dark:text-rose-300 ${LABEL_CLS}`,
    border: "border-rose-400/60",
    bg: "bg-rose-50 dark:bg-rose-950/30",
    text: "text-rose-700 dark:text-rose-300",
  },
  lifecycle_restart: {
    icon: RotateCw,
    title: "Restarted",
    titleKey: "restarted",
    titleClass: `text-amber-700 dark:text-amber-300 ${LABEL_CLS}`,
    border: "border-amber-400/60",
    bg: "bg-amber-50 dark:bg-amber-950/30",
    text: "text-amber-700 dark:text-amber-300",
  },
  lifecycle_resurrect: {
    icon: RotateCw,
    title: "Resurrected",
    titleKey: "resurrected",
    titleClass: `text-emerald-700 dark:text-emerald-300 ${LABEL_CLS}`,
    border: "border-emerald-400/60",
    bg: "bg-emerald-50 dark:bg-emerald-950/30",
    text: "text-emerald-700 dark:text-emerald-300",
  },
  lifecycle_fork: {
    icon: GitFork,
    title: "Forked",
    titleKey: "forked",
    titleClass: `text-sky-700 dark:text-sky-300 ${LABEL_CLS}`,
    border: "border-sky-400/60",
    bg: "bg-sky-50 dark:bg-sky-950/30",
    text: "text-sky-700 dark:text-sky-300",
  },
};

const MEMORY_VISUAL: MarkerVisual = {
  icon: NotebookText,
  title: "Memory",
  titleKey: "memory",
  titleClass: `text-violet-700 dark:text-violet-300 ${LABEL_CLS}`,
  border: "border-violet-400/60",
  bg: "bg-violet-50 dark:bg-violet-950/30",
  text: "text-violet-700 dark:text-violet-300",
};

const NOTE_VISUAL: MarkerVisual = {
  icon: Info,
  title: "Note",
  titleKey: "note",
  titleClass: `text-muted-foreground ${LABEL_CLS}`,
  border: "border-muted-foreground/40",
  bg: "bg-muted/40",
  text: "text-muted-foreground",
};

// Visual mapping for a card-rendered marker. Only called for lifecycle / memory /
// note (the ephemeral class has no card and never reaches here).
export function markerVisual(cls: MarkerClass): MarkerVisual {
  switch (cls.kind) {
    case "lifecycle":
      return LIFECYCLE_VISUAL[cls.tag];
    case "memory":
      return MEMORY_VISUAL;
    case "note":
      return NOTE_VISUAL;
    /* v8 ignore next 2 */
    case "ephemeral":
      throw new Error("markerVisual called on an ephemeral marker");
  }
}

// The collapsible body of a card-rendered marker — the raw payload text. Color is
// inherited from the card (markerVisual.text).
export function MarkerBody({ payload }: { payload: string }) {
  return (
    <pre className="whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-xs leading-relaxed m-0">{payload}</pre>
  );
}

// Unrecognized system_marker source / payload renders as a visual alarm — red +
// "frontend has not been adapted" copy, replacing silent plain-text fallback with
// fail-loud. The raw source / payload are shown so the user can immediately see
// which case to add.
function UnknownMarkerChip({ source, payload }: { source: string | null; payload: string }) {
  const t = useTranslations("markers");
  // Also log to dev console so DevTools can capture it directly. No typeof window
  // guard — the timeline is "use client" CSR-only and window always exists.
  console.warn("[timeline] unrecognized system_marker, frontend not adapted", { source, payload });
  return (
    // data-testid="marker-unrecognized": the stable hook the panoramic e2e
    // cases assert against (tests/e2e/test_*_flow.py) — absence of this node
    // = the #1017 alarm class did not render.
    <div
      data-testid="marker-unrecognized"
      className="border-l-2 border-destructive/60 bg-destructive/10 text-destructive rounded-r-sm px-3 py-2"
    >
      <div className="text-xs font-sans uppercase tracking-widest mb-1">
        {t("unrecognized")}
      </div>
      <div className="text-xs font-mono opacity-70 mb-1">
        source = {source === null ? "null" : JSON.stringify(source)}
      </div>
      <pre className="whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-xs leading-relaxed m-0">{payload}</pre>
    </div>
  );
}

function EphemeralMarker({ label, isError = false }: { label: string; isError?: boolean }) {
  return (
    <div
      data-testid={isError ? "marker-error" : undefined}
      className={cn("font-sans text-xs", isError ? "text-destructive" : "text-muted-foreground")}
    >
      {label}
    </div>
  );
}

// The bare (no-card) render for an ephemeral system_marker — identified by
// payload string once classifyMarker has ruled out a card family:
//   "compact_done"                -> filtered upstream; render nothing
//   "compact_request:<content>"   -> filtered upstream; render nothing
//   "cancelled"                   -> silent (stop button has no UI noise)
//   "error:<content>"             -> [error] <content>
//   anything else                 -> UnknownMarkerChip red alarm
// compact_done / compact_request are normally filtered at the SSE event layer
// (applySystemEvent), so these branches only catch stale rows.
export function EphemeralSystemMarker({
  source,
  payload,
}: {
  source: string | null;
  payload: string;
}) {
  const t = useTranslations("markers");
  if (payload === "compact_done") return null;
  if (payload === "cancelled") return null;
  if (payload.startsWith("compact_request:")) return null;
  if (payload.startsWith("error:")) {
    const content = payload.slice("error:".length);
    return <EphemeralMarker label={t("errorPrefix", { content })} isError />;
  }
  return <UnknownMarkerChip source={source} payload={payload} />;
}
