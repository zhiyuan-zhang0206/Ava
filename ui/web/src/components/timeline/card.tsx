"use client";

// Unified message-card system for the timeline. Every timeline item renders as
// a MessageCard: a colored left-border container with a clickable CardHeader
// (chevron + icon + title/summary + timestamp) and a collapsible body below it.
//
// messageCardConfig(item) is the single source of the per-kind visual mapping —
// icon, title, border/background color, which global toggle governs its default
// expanded state, and whether the header stamps a timestamp. It returns null for
// the ephemeral system markers (compact_done / cancelled / error / unrecognized),
// which render bare (no card, not collapsible) through their own renderers.
//
// Four kinds carry a rich, dynamic header summary instead of a static title —
// Thinking ("Thought for 8s"), Code (the SDK call histogram), Output ("ran in
// 1s"), and the System prompt line count. Those summaries own their own icon and
// live/error coloring, so the header renders the Summary component for them and a
// plain icon + title for every other kind.

import {
  Bot,
  ChevronDown,
  ChevronRight,
  Code2,
  FileText,
  Info,
  Paperclip,
  Settings,
  Sparkles,
  Terminal,
  User,
  type LucideIcon,
} from "lucide-react";
import { useTranslations } from "next-intl";
import { memo, type ReactNode } from "react";

import {
  formatDuration,
  formatTokensCompact,
  summarizeCode,
  summarizeOutput,
  type SdkCall,
} from "@/lib/item-summary";
import type { BackendTimelineItem } from "@/lib/types";
import { useUserSettings } from "@/lib/use-user-settings";
import { useThrottledStreaming } from "@/lib/use-throttled-streaming";
import { cn } from "@/lib/utils";

import { classifyMarker, markerVisual } from "./markers";
import { inboundKind } from "./runs";
import { isLiveCode, isLiveExecution, isLiveReasoning, useNow } from "./reasoning-clock";
import { ItemTimestamp } from "./timestamp";
import { FLEX, MIN_W_0 } from "@/lib/layout";

// Which of the four rich header summaries a card renders in place of a static
// title. null = a plain icon + title header.
export type SummaryKind = "reasoning" | "code" | "output" | "system_prompt";

export interface CardConfig {
  // Rich dynamic summary to render instead of icon + title (owns its own icon).
  readonly rich: SummaryKind | null;
  // Static header icon / title — used only when rich is null.
  readonly icon: LucideIcon | null;
  readonly title: string | null;
  // i18n key under the "timeline" namespace + interpolation values — the
  // header renders the translated title when titleKey is present, falling
  // back to `title` (English) otherwise. Keys are hand-kept in sync with
  // en.json (canonical English source).
  readonly titleKey?: string;
  readonly titleValues?: Record<string, string | number>;
  // Extra classes on the icon + title span (marker color + uppercase tracking).
  readonly titleClass?: string;
  readonly border: string;
  readonly bg: string;
  // Card-wide text color (lifecycle / memory markers tint their whole card).
  readonly cardText?: string;
  // Default expanded state — in All mode this is overridden to true,
  // in Last mode overridden based on position, in None mode overridden to false.
  readonly fixedDefault: boolean;
  // Whether the header stamps the item timestamp (left-aligned after the title).
  readonly headerTs: boolean;
}

function agentChatTitle(item: BackendTimelineItem): {
  title: string;
  titleKey: string;
  titleValues: Record<string, string>;
} {
  const suffixKey = item.interrupted
    ? "timeline.streamingInterruptedSuffix"
    : item.partial
      ? "timeline.streamingSuffix"
      : "";
  const suffix = item.interrupted
    ? " (streaming interrupted)"
    : item.partial
      ? " (streaming…)"
      : "";
  return {
    title: `Ava ▸${suffix}`,
    titleKey: "agentChat",
    titleValues: { suffixKey },
  };
}

// Pure per-item visual config. null → this item is an ephemeral system marker
// that renders bare (no card). Kept pure (no toggle state) so the timeline can
// memoize it over the items array alone.
export function messageCardConfig(item: BackendTimelineItem): CardConfig | null {
  switch (item.kind) {
    case "system_prompt":
      return {
        rich: "system_prompt",
        icon: Settings,
        title: null,
        border: "border-indigo-400/40",
        bg: "bg-indigo-50/40 dark:bg-indigo-950/15",
        fixedDefault: false,
        headerTs: false,
      };
    case "agent_reasoning":
      return {
        rich: "reasoning",
        icon: Sparkles,
        title: null,
        border: "border-slate-400/40",
        bg: "bg-slate-50/60 dark:bg-slate-900/20",
        fixedDefault: true,
        headerTs: true,
      };
    case "agent_code":
      return {
        rich: "code",
        icon: Code2,
        title: null,
        // Deeper border than agent_chat — the tool call is the load-bearing action.
        border: "border-emerald-500/70",
        bg: "bg-card",
        fixedDefault: true,
        headerTs: true,
      };
    case "code_output":
      return {
        rich: "output",
        icon: Terminal,
        title: null,
        border: "border-amber-500/50",
        bg: "bg-amber-50/40 dark:bg-amber-950/10",
        fixedDefault: true,
        headerTs: true,
      };
    case "agent_chat": {
      const chatTitle = agentChatTitle(item);
      return {
        rich: null,
        icon: Bot,
        title: chatTitle.title,
        titleKey: `timeline.${chatTitle.titleKey}`,
        titleValues: chatTitle.titleValues,
        border: "border-emerald-500/50",
        bg: "bg-card",
        fixedDefault: true,
        headerTs: true,
      };
    }
    case "attach":
      // Attached media (ava.self.attach) — always open so the image
      // thumbnails are visible without a click (user ruling 2026-08-26).
      return {
        rich: null,
        icon: Paperclip,
        title: null,
        titleKey: "timeline.attachedFiles",
        border: "border-sky-400/50",
        bg: "bg-sky-50/40 dark:bg-sky-950/10",
        fixedDefault: true,
        headerTs: true,
      };
    case "inbound_chat": {
      // Inter-agent + system inbound default collapsed (secondary chatter — the
      // header label stays, the body folds); only a human message stays open.
      const kind = inboundKind(item.source);
      if (kind === "agent") {
        return {
          rich: null,
          icon: Bot,
          title: `Agent ${(item.source ?? "").slice("agent:".length)}`,
          titleKey: "timeline.agent",
          titleValues: { id: (item.source ?? "").slice("agent:".length) },
          border: "border-violet-400/60",
          bg: "bg-violet-50 dark:bg-violet-950/20",
          fixedDefault: false,
          headerTs: true,
        };
      }
      if (kind === "human") {
        return {
          rich: null,
          icon: User,
          title: "Human",
          titleKey: "timeline.human",
          border: "border-gray-400/60",
          bg: "bg-gray-50 dark:bg-gray-900/30",
          fixedDefault: true,
          headerTs: true,
        };
      }
      return {
        rich: null,
        icon: Info,
        title: "System",
        titleKey: "timeline.system",
        border: "border-sky-400/60",
        bg: "bg-sky-50 dark:bg-sky-950/20",
        fixedDefault: false,
        headerTs: true,
      };
    }
    case "inbound_compact_summary":
      return {
        rich: null,
        icon: FileText,
        title: "Compact summary",
        titleKey: "timeline.compactSummary",
        border: "border-sky-400/60",
        bg: "bg-sky-50 dark:bg-sky-950/20",
        fixedDefault: false,
        headerTs: true,
      };
    case "inbound_compact_request":
      return {
        rich: null,
        icon: FileText,
        title: "Compact request",
        titleKey: "timeline.compactRequest",
        border: "border-sky-400/60",
        bg: "bg-sky-50 dark:bg-sky-950/20",
        fixedDefault: false,
        headerTs: true,
      };
    case "system_marker": {
      const cls = classifyMarker(item.source);
      if (cls.kind === "ephemeral") return null; // bare render path
      const v = markerVisual(cls);
      return {
        rich: null,
        icon: v.icon,
        title: v.title,
        titleKey: v.titleKey ? `markers.${v.titleKey}` : undefined,
        titleClass: v.titleClass,
        border: v.border,
        bg: v.bg,
        cardText: v.text,
        // Framework notes (lifecycle / memory / guidance) default collapsed —
        // the labeled header conveys the event; the payload folds.
        fixedDefault: false,
        // Events keep their timestamp; standing context / guidance notes drop it.
        headerTs: item.show_timestamp,
      };
    }
  }
  // Exhaustiveness guard: TS narrows to never; runtime fallback renders bare.
  /* v8 ignore next 3 */
  const _exhaustive: never = item.kind;
  console.warn("[timeline] messageCardConfig: unknown item kind", _exhaustive);
  return null;
}

// Cap how often a streaming item's header re-derives its summary. The code /
// output summaries scan the full accumulated payload (regex + line split), so at
// delta-commit rate the scan would restart from zero dozens of times a second; a
// glanceable summary only needs a few refreshes per second.
const SUMMARY_PARSE_INTERVAL_MS = 250;

// Shared by the TurnBlock aggregate header so a collapsed turn reads as the same
// clickable summary row as a card header.
export const HEADER_CLS =
  "w-full flex items-center gap-1.5 px-3 py-1.5 text-xs font-sans text-left " +
  "text-muted-foreground/80 hover:text-foreground hover:bg-accent/30 " +
  "transition-colors select-none rounded-tr-sm";

// The colored left-border container. cardText tints the whole card (markers).
// `actions` (copy / fork) is an optional overlay pinned to the block's
// bottom-right corner, revealed on hover — a null actions prop (collapsed /
// partial / not-yet-eligible rows) mounts nothing, so those rows carry zero
// extra DOM or listeners. The overlay uses a NAMED group (group/msgcard) so
// it doesn't cross-talk with the unnamed `group`/`group-hover` pattern
// already used inside item content (e.g. the code-block copy icon in
// ./item's EnvelopeContent, python-code.tsx, markdown.tsx) — hovering the
// card must not also reveal those unrelated inner hover affordances.
// The outer positioning strip spans the full card width (`inset-x-0`) but
// stays `pointer-events-none`; only the pill itself is ever interactive, and
// even the pill stays `pointer-events-none` while invisible (opacity: 0 does
// NOT disable hit-testing on its own) — without that, a tap in the card's
// bottom-right corner on a fine-pointer device would silently hit an unseen
// copy/fork button before the user ever saw it. `pointer-events-auto` only
// switches on together with the same hover/focus-within variant that raises
// opacity. A touch/coarse-pointer device has no real `:hover` at all, so
// `pointer-coarse:` unconditionally opts it out of the hover gate — the pill
// is always visible and tappable there, matching how touch UIs generally
// forgo hover-to-discover chrome. Mouse/trackpad (fine pointer) keeps the
// gated reveal.
export function MessageCard({
  config,
  actions,
  children,
}: {
  config: CardConfig;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div
      className={cn(
        "border-l-2 rounded-r-sm relative",
        actions ? "group/msgcard" : null,
        config.border,
        config.bg,
        config.cardText,
      )}
    >
      {children}
      {actions ? (
        <div className={cn("pointer-events-none absolute inset-x-0 bottom-0 z-10 justify-end p-1", FLEX)}>
          <div
            className={cn(
              "pointer-events-none items-center gap-0.5 rounded-md px-0.5 py-0.5",
              "opacity-0 transition-opacity",
              "group-hover/msgcard:opacity-100 group-hover/msgcard:pointer-events-auto",
              "group-hover/msgcard:bg-background/70 group-hover/msgcard:border group-hover/msgcard:border-border/40",
              "focus-within:opacity-100 focus-within:pointer-events-auto",
              "focus-within:bg-background/70 focus-within:border focus-within:border-border/40",
              "pointer-coarse:opacity-100 pointer-coarse:pointer-events-auto pointer-coarse:bg-transparent pointer-coarse:border-0",
              FLEX
            )}
          >
            {actions}
          </div>
        </div>
      ) : null}
    </div>
  );
}

// The clickable card header — always visible, toggles the body below it. Renders
// the rich Summary for the four summary kinds, else a plain icon + title.
// Render a card's static header title: translated via titleKey when present
// (falling back to the English `title`). titleKey is a full dotted key
// ("timeline.agentChat", "markers.terminated", …) resolved through the
// root translator; the agent-chat suffix rides inside titleValues as its own
// dotted key, resolved here.
function cardTitle(config: CardConfig, t: ReturnType<typeof useTranslations>): string {
  if (!config.titleKey) return config.title ?? "";
  const suffixKey = config.titleValues?.suffixKey;
  // Always pass `suffix` to the message: the agent-chat title is "Ava ▸{suffix}",
  // and next-intl renders the raw key when an interpolation variable is
  // missing (MissingValueError → key fallback), which leaked "timeline.agentChat"
  // for plain (non-interrupted, non-partial) chat messages. Empty suffix is a
  // valid value — it renders the bare "Ava ▸".
  const values = suffixKey
    ? { ...config.titleValues, suffix: t(suffixKey as Parameters<typeof t>[0]) }
    : { ...config.titleValues, suffix: "" };
  return t(config.titleKey as Parameters<typeof t>[0], values);
}

export const CardHeader = memo(function CardHeader({
  item,
  config,
  expanded,
  onToggle,
}: {
  item: BackendTimelineItem;
  config: CardConfig;
  expanded: boolean;
  onToggle: () => void;
}) {
  const t = useTranslations();
  const live = isLiveReasoning(item) || isLiveCode(item) || isLiveExecution(item);
  const now = useNow(live);
  const { settings } = useUserSettings();
  const showWeekday = settings["display.show_timestamp_weekday"] as boolean;
  const summaryPayload = useThrottledStreaming(
    item.payload,
    !!item.partial,
    SUMMARY_PARSE_INTERVAL_MS,
  );
  const Icon = config.icon;

  return (
    // No title attribute — deliberately no hover tooltip on this button; the
    // chevron + click affordance is enough for sighted mouse users.
    // aria-expanded carries the same collapsed/expanded state to assistive
    // tech (the disclosure-widget ARIA pattern) — title was never a reliable
    // way to expose that anyway. data-testid/data-expanded are the test hook
    // (tests used to select via the tooltip text).
    <button
      type="button"
      onClick={onToggle}
      className={HEADER_CLS}
      aria-expanded={expanded}
      data-testid="card-toggle"
      data-expanded={expanded}
    >
      {expanded ? (
        <ChevronDown className="size-3 shrink-0 opacity-60" />
      ) : (
        <ChevronRight className="size-3 shrink-0 opacity-60" />
      )}
      {config.rich ? (
        <Summary item={item} payload={summaryPayload} now={now} live={live} />
      ) : (
        <span className={cn("items-center gap-1.5", config.titleClass, FLEX, MIN_W_0)}>
          {Icon ? <Icon className="size-3.5 shrink-0" /> : null}
          <span className="truncate">{cardTitle(config, t)}</span>
        </span>
      )}
      {config.headerTs ? (
        <span className="hidden sm:block shrink-0 pl-2">
          <ItemTimestamp iso={item.created_at ?? ""} showWeekday={showWeekday} />
        </span>
      ) : null}
    </button>
  );
});

function Summary({
  item,
  payload,
  now,
  live,
}: {
  item: BackendTimelineItem;
  // item.payload throttled while the item streams — the code / output summaries
  // scan it in full, so they read this instead of item.payload.
  payload: string;
  now: number;
  live: boolean;
}) {
  switch (item.kind) {
    case "agent_reasoning":
      return <ThinkingSummary item={item} now={now} live={live} />;
    case "agent_code":
      return <CodeSummary item={item} payload={payload} now={now} />;
    case "code_output":
      return <OutputSummary item={item} payload={payload} now={now} />;
    case "system_prompt":
      return <SystemPromptSummary payload={payload} />;
    /* v8 ignore next 3 */
    default:
      // Only the four summary kinds render a Summary; other kinds never reach here.
      return null;
  }
}

function SystemPromptSummary({ payload }: { payload: string }) {
  const t = useTranslations("timeline");
  // Trailing newline is a terminator, not an extra line.
  const body = payload.endsWith("\n") ? payload.slice(0, -1) : payload;
  const lines = body === "" ? 0 : body.split("\n").length;
  return (
    <span className={cn("items-center gap-1.5", FLEX, MIN_W_0)}>
      <Settings className="size-3.5 shrink-0" />
      <span className="truncate">
        {t("systemPrompt")}
        <span className="opacity-60">
          {` · ${t("lineCount", { count: lines, unit: t(lines === 1 ? "line" : "lines") })}`}
        </span>
      </span>
    </span>
  );
}

function ThinkingSummary({
  item,
  now,
  live,
}: {
  item: BackendTimelineItem;
  now: number;
  live: boolean;
}) {
  const t = useTranslations("timeline");
  const tokens =
    item.reasoning_tokens != null
      ? t("tokens", { count: formatTokensCompact(item.reasoning_tokens) })
      : null;

  let label: string;
  if (live && item.reasoningStartedAt != null) {
    label = t("thinkingFor", { duration: formatDuration(now - item.reasoningStartedAt) });
  } else if (item.reasoning_ms != null) {
    // Authoritative backend duration (committed via snapshot).
    label = t("thoughtFor", { duration: formatDuration(item.reasoning_ms) });
  } else if (item.reasoningElapsedMs != null) {
    // Frontend-frozen elapsed — shown after the clock stops but before the
    // snapshot commits the backend reasoning_ms, so the duration never flickers
    // back to a bare "Thinking".
    label = t("thoughtFor", { duration: formatDuration(item.reasoningElapsedMs) });
  } else {
    // Historical item from before durations were persisted, or a block whose
    // timing was not recorded — still label it as thinking.
    label = t("thinking");
  }

  return (
    <span className={cn("items-center gap-1.5", FLEX, MIN_W_0)}>
      <Sparkles className={cn("size-3.5 shrink-0", live && "text-sky-500")} />
      {/* tabular-nums: the live clock rewrites the digits ~10x/s; fixed-width
          numerals keep the header from wobbling as digits change. */}
      <span className="truncate tabular-nums">
        {label}
        {tokens ? <span className="opacity-60"> · {tokens}</span> : null}
      </span>
    </span>
  );
}

function CodeSummary({
  item,
  payload,
  now,
}: {
  item: BackendTimelineItem;
  payload: string;
  now: number;
}) {
  const { calls, lines } = summarizeCode(payload, item.sdk_calls);

  // Two states, mirroring ThinkingSummary / OutputSummary:
  // - LIVE (codeStartedAt set, still streaming): "Writing code for Xs".
  // - DONE: "Wrote code for Xs" from the committed backend code_elapsed_ms,
  //   falling back to the frozen frontend codeElapsedMs. Shown whenever the
  //   value is PRESENT — deliberately NOT gated on > 0. A single-chunk tool
  //   call (the provider delivers the whole args payload in one delta, so the
  //   backend's first==last code-fragment timestamp yields code_elapsed_ms 0 —
  //   agent/graph/_callbacks.py) is still a real, completed code block;
  //   formatDuration floors it at 0.1s, exactly as "Thought for" / "Ran in"
  //   already do for a 0 reasoning_ms / exec_ms. The old > 0 guard silently
  //   dropped the whole label for those blocks, so the inner code card showed
  //   no duration at all while thinking/output showed theirs.
  let timing: string | null = null;
  const liveCode = isLiveCode(item);
  const codeElapsed = item.code_elapsed_ms ?? item.codeElapsedMs;
  if (liveCode && item.codeStartedAt != null) {
    timing = `Writing code for ${formatDuration(now - item.codeStartedAt)}`;
  } else if (codeElapsed != null) {
    timing = `Wrote code for ${formatDuration(codeElapsed)}`;
  }

  return (
    <span className={cn("items-center gap-1.5", FLEX, MIN_W_0)}>
      <Code2 className={cn("size-3.5 shrink-0", liveCode && "text-sky-500")} />
      <span className="truncate tabular-nums">
        {timing ? <>{timing}<span className="opacity-60">{" · "}</span></> : null}
        {calls.length > 0 ? (
          calls.map((c, i) => (
            <span key={c.method}>
              {i > 0 ? <span className="opacity-40"> · </span> : null}
              <CallBadge call={c} />
            </span>
          ))
        ) : (
          <span className="opacity-80">{`${lines} ${lines === 1 ? "line" : "lines"} of code`}</span>
        )}
      </span>
    </span>
  );
}

// Exported for TurnBlock — the aggregate turn header reuses the same
// `method × count` chip style for its SDK-call summary line.
export function CallBadge({ call }: { call: SdkCall }) {
  return (
    <span>
      <span className="text-foreground/80">{call.method}</span>
      <span className="opacity-60">{` × ${call.count}`}</span>
    </span>
  );
}

function OutputSummary({ item, payload, now }: { item: BackendTimelineItem; payload: string; now: number }) {
  const t = useTranslations("timeline");
  const { lines, hasError } = summarizeOutput(payload);
  // exec_ms is the real wall-clock the code ran (backend); lead with it since
  // "how long did it take" is the primary signal. Absent on historical rows
  // from before durations were persisted — then just show the line count. While
  // the execution streams (execStartedAt set, exec_ms not yet committed), show a
  // live "Running for Xs" clock.
  const liveExec = isLiveExecution(item);
  let ran: string | null = null;
  if (item.exec_ms != null) {
    ran = t("ranIn", { duration: formatDuration(item.exec_ms) });
  } else if (liveExec && item.execStartedAt != null) {
    ran = t("runningFor", { duration: formatDuration(now - item.execStartedAt) });
  }
  return (
    <span className={cn("items-center gap-1.5", FLEX, MIN_W_0)}>
      <Terminal className={cn("size-3.5 shrink-0", hasError && "text-destructive", liveExec && "text-sky-500")} />
      <span className="truncate tabular-nums">
        {hasError ? <span className="text-destructive">{t("error")} · </span> : null}
        {ran ? <>{ran}<span className="opacity-60">{" · "}</span></> : null}
        {t("lineCount", { count: lines, unit: t(lines === 1 ? "line" : "lines") })}
      </span>
    </span>
  );
}
