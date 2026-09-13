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

import { DEFAULT_TIMELINE_COLORS, type ColorSlotId, type TimelineColors } from "@/lib/timeline-colors";
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
export const MEMORY_SOURCES = new Set(["memory", "agent_memory", "inherited_memory"]);

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

// Icon/title metadata per marker family — the color classes come from the
// resolved timeline-colors slot (Display-settings configurable), composed at
// call time so one family swap restyles the whole marker.
const LIFECYCLE_META: Record<
  LifecycleTag,
  { icon: LucideIcon; title: string; titleKey: string; slot: ColorSlotId }
> = {
  lifecycle_terminate: { icon: PowerOff, title: "Terminated", titleKey: "terminated", slot: "lifecycle_terminate" },
  lifecycle_restart: { icon: RotateCw, title: "Restarted", titleKey: "restarted", slot: "lifecycle_restart" },
  lifecycle_resurrect: { icon: RotateCw, title: "Resurrected", titleKey: "resurrected", slot: "lifecycle_resurrect" },
  lifecycle_fork: { icon: GitFork, title: "Forked", titleKey: "forked", slot: "lifecycle_fork" },
};

const MEMORY_META = { icon: NotebookText, title: "Memory", titleKey: "memory", slot: "memory" } as const;
const NOTE_META = { icon: Info, title: "Note", titleKey: "note", slot: "note" } as const;

function slotVisual(
  meta: { icon: LucideIcon; title: string; titleKey: string; slot: ColorSlotId },
  colors: TimelineColors,
): MarkerVisual {
  const c = colors[meta.slot];
  return {
    icon: meta.icon,
    title: meta.title,
    titleKey: meta.titleKey,
    titleClass: `${c.text ?? ""} ${LABEL_CLS}`.trim(),
    border: c.border,
    bg: c.bg ?? "",
    text: c.text ?? "",
  };
}

// Visual mapping for a card-rendered marker. Only called for lifecycle / memory /
// note (the ephemeral class has no card and never reaches here).
export function markerVisual(
  cls: MarkerClass,
  colors: TimelineColors = DEFAULT_TIMELINE_COLORS,
): MarkerVisual {
  switch (cls.kind) {
    case "lifecycle":
      return slotVisual(LIFECYCLE_META[cls.tag], colors);
    case "memory":
      return slotVisual(MEMORY_META, colors);
    case "note":
      return slotVisual(NOTE_META, colors);
    /* v8 ignore next 2 */
    case "ephemeral":
      throw new Error("markerVisual called on an ephemeral marker");
  }
}

// The collapsible body of a card-rendered marker — the raw payload text. Color is
// inherited from the card (markerVisual.text).
export function MarkerBody({ payload }: { payload: string }) {
  return (
    <pre className="whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-[12px] leading-relaxed m-0">{payload}</pre>
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
      <div className="text-[11px] font-mono uppercase tracking-widest mb-1">
        {t("unrecognized")}
      </div>
      <div className="text-[11px] font-mono opacity-70 mb-1">
        source = {source === null ? "null" : JSON.stringify(source)}
      </div>
      <pre className="whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-[12px] leading-relaxed m-0">{payload}</pre>
    </div>
  );
}

function EphemeralMarker({ label, isError = false }: { label: string; isError?: boolean }) {
  return (
    <div
      data-testid={isError ? "marker-error" : undefined}
      className={cn("font-mono text-xs", isError ? "text-destructive" : "text-muted-foreground")}
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
