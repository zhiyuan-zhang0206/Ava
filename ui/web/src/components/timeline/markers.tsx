"use client";

import { GitFork, Info, NotebookText, PowerOff, RotateCw, type LucideIcon } from "lucide-react";
import { useTranslations } from "next-intl";

import { ChatMarkdown } from "@/components/content/markdown";
import { DEFAULT_TIMELINE_COLORS, type ColorSlotId, type TimelineColors } from "@/lib/timeline/timeline-colors";
import { cn } from "@/lib/format/utils";

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

// ava:ref ui/web/src/docs/frontend-components/timeline-view/markers.ava.okf.md#marker-dispatch-sets
export const MEMORY_SOURCES = new Set(["memory", "agent_memory", "inherited_memory"]);

export type MarkerClass =
  | { readonly kind: "lifecycle"; readonly tag: LifecycleTag }
  | { readonly kind: "memory" }
  | { readonly kind: "note" }
  | { readonly kind: "ephemeral" };

export const NOTE_SOURCES = new Set([
  "impersonation",
  "sdk_hint",
  "memory_write_check",
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
  "skill_recall",
]);

export function classifyMarker(source: string | null): MarkerClass {
  if (source !== null && isLifecycleTag(source)) return { kind: "lifecycle", tag: source };
  if (source !== null && MEMORY_SOURCES.has(source)) return { kind: "memory" };
  if (source !== null && NOTE_SOURCES.has(source)) return { kind: "note" };
  return { kind: "ephemeral" };
}

export interface MarkerVisual {
  readonly icon: LucideIcon;
  readonly title: string;
  readonly titleKey?: string;
  readonly titleClass: string;
  readonly border: string;
  readonly bg: string;
  readonly text: string;
}

const LABEL_CLS = "uppercase tracking-widest";

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
    case "ephemeral":
      throw new Error("markerVisual called on an ephemeral marker");
  }
}

export function MarkerBody({ payload, markdown = false }: { payload: string; markdown?: boolean }) {
  if (markdown) {
    return (
      <div className="text-[12px] leading-relaxed">
        <ChatMarkdown content={payload} />
      </div>
    );
  }
  return (
    <pre className="whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-[12px] leading-relaxed m-0">{payload}</pre>
  );
}

function UnknownMarkerChip({ source, payload }: { source: string | null; payload: string }) {
  const t = useTranslations("markers");
  console.warn("[timeline] unrecognized system_marker, frontend not adapted", { source, payload });
  return (
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

// ava:ref ui/web/src/docs/frontend-components/timeline-view/markers.ava.okf.md#ephemeral-marker-testids
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
