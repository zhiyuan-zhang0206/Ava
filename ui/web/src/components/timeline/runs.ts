// Turn-collapse grouping — the pure layer behind the timeline's aggregate
// turn blocks. Two independent concerns live here, both DOM-free so vitest
// drives them directly:
//
// 1. classifyItem — splits every timeline item into the human-readable backbone
//    (primary: kept always visible, breaks a turn) vs the secondary chatter the
//    agent emits during a turn (thinking / code / output / inter-agent /
//    system notes: foldable). The `bare` class is the ephemeral system_marker
//    that has no card (messageCardConfig → null); it never joins a turn and
//    renders as-is. Classification is derived from the SAME source model the
//    cards use (inboundKind for inbound_chat, classifyMarker for system_marker),
//    so a turn's membership and a card's identity can never disagree.
//
// 2. groupIntoTurns — folds each maximal stretch of adjacent secondary items
//    into one turn group (a work block under a single "Details" header); primary
//    / bare items pass through as single groups. Every secondary run — even a
//    single item — becomes a turn group so it can be collapsed. Streaming
//    secondary items are always groupable so the first streaming chunk
//    immediately lands inside a work block (no bare-then-wrap layout shift).

import { orderSdkCalls, type SdkCall } from "@/lib/item-summary";
import type { BackendTimelineItem } from "@/lib/types";
import enMessages from "../../../messages/en.json";

import { classifyMarker } from "./markers";
import { isLiveCode, isLiveExecution, isLiveReasoning } from "./reasoning-clock";

// The turn-label formatters are pure functions (unit-tested directly, no
// React context), so the translator is an optional parameter: callers with a
// next-intl translator (Timeline components) pass it for localized output;
// the standalone tests / en fallback omit it and get the canonical English
// strings from en.json — keeping the pure layer dependency-free.
export type TurnTranslator = (
  key: string,
  values?: Record<string, string | number>,
) => string;

const EN_TURN: TurnTranslator = (key, values) => {
  // The JSON import is typed structurally; `.timeline` is read via an index
  // cast so a hand-edited catalog degrades to the raw key instead of crashing.
  const dict: Record<string, string> = (
    enMessages as unknown as Record<string, Record<string, string>>
  ).timeline;
  let s = dict[key] ?? key;
  if (values) {
    for (const [k, v] of Object.entries(values)) {
      s = s.replaceAll(`{${k}}`, String(v));
    }
  }
  return s;
};

// Which side of the "is this an inbound message a human would read" line an
// inbound_chat source falls on. Mirrors the envelope source model
// (shared/agents/messages/envelope.py): `agent:N` is another agent talking; `user` / `ui:page:*`
// is a person (a page post "reads as User"); everything else — `system`,
// `system:*`, `watcher:N`, `shell:N`, `schedule:N`, `self:update` — is a
// framework-generated wake-up / notice. card.tsx branches on the same split.
export function inboundKind(source: string | null): "human" | "agent" | "system" {
  const s = source ?? "";
  if (s.startsWith("agent:")) return "agent";
  if (s === "user" || s.startsWith("ui:page:")) return "human";
  return "system";
}

// primary: the human-readable backbone — Ava's text replies and human inbound
//   messages. Always rendered; never folded into a turn.
// secondary: foldable chatter — thinking / code / output, inter-agent messages,
//   system inbound, compaction, the system prompt, and framework note markers.
// bare: an ephemeral system_marker (no card); rendered as-is, never in a turn.
export type ItemClass = "primary" | "secondary" | "bare";

export function classifyItem(item: BackendTimelineItem): ItemClass {
  switch (item.kind) {
    case "agent_chat":
      return "primary";
    case "inbound_chat":
      return inboundKind(item.source) === "human" ? "primary" : "secondary";
    case "attach":
      // Attached media folds into the turn's details block like any other
      // secondary item (user ruling 2026-08-30 — expand the details block
      // to see it; the previous "always visible" primary pin is dropped).
      return "secondary";
    case "agent_reasoning":
    case "agent_code":
    case "code_output":
    case "inbound_compact_summary":
    case "inbound_compact_request":
    case "system_prompt":
      return "secondary";
    case "system_marker":
      // The card-rendered marker families (lifecycle / memory / note) fold like
      // any note; the ephemeral markers have no card and stay bare.
      return classifyMarker(item.source).kind === "ephemeral" ? "bare" : "secondary";
  }
  // Exhaustiveness guard: TS narrows to never; a new kind lands here as bare.
  /* v8 ignore next 3 */
  const _exhaustive: never = item.kind;
  console.warn("[timeline] classifyItem: unknown item kind", _exhaustive);
  return "bare";
}

export interface TurnSummary {
  // Total item count in the turn (all secondary items, including actions).
  readonly total: number;
  // Action-item counts — thinking / code / output items. These are the
  // "specific metrics" shown in the turn header line alongside "worked for X".
  readonly thinking: number;
  readonly code: number;
  readonly output: number;
  // Model work rounds (task #3313): one round emits its reasoning, code, and
  // output items together, so the three action counters agree on every
  // complete round — `turns` is their max. The header shows this single count
  // ("5 turns") in place of the former per-kind labels.
  readonly turns: number;
  // Non-work item counts. All are still counted; the compact-summary count is
  // the one that renders in the collapsed line — the system-prompt count is
  // kept as data but no longer emitted (user ruling 2026-09-15, task #3557).
  readonly systemPrompts: number;
  readonly compactSummaries: number;
  readonly memories: number;
  readonly agentMessages: number;
  // Catch-all for the remaining secondary chatter: card-family system_marker
  // notes that are not memories (lifecycle events, guidance notes) and
  // system-sourced inbound messages (wake-ups / framework notices). Every
  // secondary kind lands in SOME count, so a turn still reads as a non-empty
  // line ("1 system note") instead of a blank header — with one deliberate
  // exception: a turn of only system prompts renders blank, because the
  // system-prompt fragment is not emitted (task #3557).
  readonly systemNotes: number;
  // Aggregate wall-clock, both backend-authoritative and both zero-able (a turn
  // with no reasoning items has thinkingMs 0; no execution has execMs 0):
  // - thinkingMs: sum of each `agent_reasoning` item's committed `reasoning_ms`,
  //   falling back to `reasoningElapsedMs` (the frozen frontend clock — set
  //   when a stream disconnects before the backend commits the block).
  // - codeMs: sum of each `agent_code` item's committed `code_elapsed_ms`
  //   (backend wall-clock, persisted with the message), falling back to
  //   `codeElapsedMs` (the frozen frontend clock — set when code writing
  //   finishes, before the snapshot commits the backend value).
  // - execMs: sum of each `code_output` item's committed `exec_ms` — the real
  //   wall-clock the code ran (agent/graph/_exec.py).
  readonly thinkingMs: number;
  readonly codeMs: number;
  readonly execMs: number;
  // SDK calls aggregated across the turn's `agent_code` items, grouped by
  // namespace alphabetically, then descending by count and method name. Read
  // directly off each item's backend-populated `sdk_calls` — the call tally
  // recorded by the run that executed the code
  // and projected onto the item. This aggregation renders ONLY recorded
  // calls: `it.sdk_calls` present (even `[]`) is trusted as-is; absent (no
  // committed field yet, e.g. streaming) contributes nothing. No text
  // scanning, here or in the per-block chip (`summarizeCode`): scanning the
  // payload could surface a phantom method from a comment or string like
  // `# see ava.files.read(...)` in a header the user may never expand to
  // check.
  // How long the agent actually WORKED in this turn: thinkingMs + codeMs +
  // execMs. Not wall-clock across the turn — a turn is a maximal run of
  // secondary items, so it can span a restart marker or a wake-up and the idle
  // gap before the agent picked the work back up; charging that gap to the
  // agent read as hours of work that never happened. Items with no duration of
  // their own (notes, markers) contribute 0 via itemOwnMs. Zero when nothing in
  // the turn is timed. Used for the "Worked for X" header line.
  // Used for BOTH header timer states — the live "Working for X" adds the
  // in-flight block's elapsed to it, so the number does not jump when the turn
  // goes quiet and the label settles to "Worked for X". No wall-clock anchor
  // for the turn is kept: the turn's start time measures the gap as well as
  // the work, which is exactly what this field exists to exclude.
  readonly workedMs: number;
  // Live-timing info for the collapsed turn header clock. When the last
  // item in the turn is still streaming (not yet committed), these fields
  // record its kind and start timestamp so TurnBlock can add the live delta
  // to the committed aggregate durations — without them the sub-block timing
  // line ("Thought for Xs · Wrote code for Xs · Ran for Xs") stays frozen
  // at the committed values while the "Working for Xs" clock ticks.
  readonly lastLiveKind: "reasoning" | "code" | "output" | null;
  readonly lastLiveStartedAt: number;
  readonly sdkCalls: readonly SdkCall[];
}

// An item's own committed/frozen duration — how long the block itself took,
// independent of neighbor timestamps. Zero when the item carries no timing
// (chat, markers, never-committed historical rows).
function itemOwnMs(it: BackendTimelineItem): number {
  switch (it.kind) {
    case "agent_reasoning":
      return it.reasoning_ms ?? it.reasoningElapsedMs ?? 0;
    case "agent_code":
      return it.code_elapsed_ms ?? it.codeElapsedMs ?? 0;
    case "code_output":
      return it.exec_ms ?? 0;
    default:
      return 0;
  }
}

export function summarizeTurn(items: readonly BackendTimelineItem[]): TurnSummary {
  let thinking = 0;
  let code = 0;
  let output = 0;
  let thinkingMs = 0;
  let codeMs = 0;
  let execMs = 0;
  let systemPrompts = 0;
  let compactSummaries = 0;
  let memories = 0;
  let agentMessages = 0;
  let systemNotes = 0;
  const sdkCounts = new Map<string, number>();
  for (const it of items) {
    if (it.kind === "agent_reasoning") {
      thinking += 1;
      thinkingMs += itemOwnMs(it);
    } else if (it.kind === "agent_code") {
      code += 1;
      codeMs += itemOwnMs(it);
      if (it.sdk_calls) {
        for (const c of it.sdk_calls) {
          sdkCounts.set(c.method, (sdkCounts.get(c.method) ?? 0) + c.count);
        }
      }
    } else if (it.kind === "code_output") {
      output += 1;
      execMs += itemOwnMs(it);
    } else if (it.kind === "system_prompt") {
      systemPrompts += 1;
    } else if (it.kind === "inbound_compact_summary" || it.kind === "inbound_compact_request") {
      compactSummaries += 1;
    } else if (it.kind === "inbound_chat") {
      // Human inbound is primary (never in a turn); agent vs system split here.
      if (inboundKind(it.source) === "agent") agentMessages += 1;
      else systemNotes += 1;
    } else if (it.kind === "system_marker") {
      if (classifyMarker(it.source).kind === "memory") memories += 1;
      else systemNotes += 1; // lifecycle events + guidance notes
    } else {
      // attach (secondary since 2026-08-30) and the defensive agent_chat
      // (primary, never reaches a turn) land here so the "every member is
      // counted somewhere" invariant holds regardless.
      systemNotes += 1;
    }
  }
  const sdkCalls = orderSdkCalls(
    [...sdkCounts.entries()].map(([method, count]) => ({ method, count })),
  );
  // Agent-work duration: sum of each work block's backend-measured time
  // (thinkingMs + codeMs + execMs), not wall-clock between items.
  // This naturally excludes system notes and restart markers — their
  // itemOwnMs() returns 0 — and correctly measures work even when the
  // first item lacks created_at (e.g. a streaming block).
  const workedMs = thinkingMs + codeMs + execMs;
  // Live timing for the collapsed turn header: which kind of block is
  // currently streaming and when it started. Only the last item can be
  // live (all prior items are committed). Uses the same live-detection
  // predicates the individual-card headers use, so the collapsed view
  // keeps the same clock as the expanded individual block would.
  let lastLiveKind: "reasoning" | "code" | "output" | null = null;
  let lastLiveStartedAt = 0;
  if (items.length > 0) {
    const last = items[items.length - 1];
    if (isLiveReasoning(last)) {
      lastLiveKind = "reasoning";
      lastLiveStartedAt = last.reasoningStartedAt ?? 0;
    } else if (isLiveCode(last)) {
      lastLiveKind = "code";
      lastLiveStartedAt = last.codeStartedAt ?? 0;
    } else if (isLiveExecution(last)) {
      lastLiveKind = "output";
      lastLiveStartedAt = last.execStartedAt ?? 0;
    }
  }
  return { total: items.length, thinking, code, output, turns: Math.max(thinking, code, output), systemPrompts, compactSummaries, memories, agentMessages, systemNotes, thinkingMs, codeMs, execMs, sdkCalls, workedMs, lastLiveKind, lastLiveStartedAt };
}

export function formatTurnSummary(summary: TurnSummary, t: TurnTranslator = EN_TURN): string {
  const parts: string[] = [];
  // The model's work rounds first ("5 turns") — one round emits its reasoning,
  // code, and output items together, so the former per-kind action counts
  // ("2 thinking · 1 code · 1 output") always agreed and collapse into this
  // single number (task #3313). Then non-work items (context + chatter).
  // Sentence case throughout — these fragments join into one status line
  // ("2 turns · 1 memory · 2 system notes · 2 agent messages"), so every label
  // stays lowercase like the work-item labels below (no mid-line Title Case).
  if (summary.turns > 0) {
    parts.push(
      t("turns", {
        count: summary.turns,
        unit: t(summary.turns === 1 ? "turnSingular" : "turnPlural"),
      }),
    );
  }
  // The "system prompt" fragment is deliberately NOT emitted (user ruling
  // 2026-09-15, task #3557): the prompt is present in every turn, so the
  // label carries no information in the collapsed line. The count stays in
  // summarizeTurn — display-layer change only.
  // Singular compact summaries drop the count prefix ("compact summary …"),
  // mirroring the original English copy — the singular label stands alone.
  if (summary.compactSummaries > 0) {
    parts.push(
      summary.compactSummaries === 1
        ? t("compactSummarySingular")
        : t("compactSummaries", {
            count: summary.compactSummaries,
            unit: t("compactSummariesPlural"),
          }),
    );
  }
  if (summary.memories > 0) {
    parts.push(
      t("memories", {
        count: summary.memories,
        unit: t(summary.memories === 1 ? "memory" : "memories_plural"),
      }),
    );
  }
  if (summary.systemNotes > 0) {
    parts.push(
      t("systemNotes", {
        count: summary.systemNotes,
        unit: t(summary.systemNotes === 1 ? "systemNoteSingular" : "systemNotesPlural"),
      }),
    );
  }
  if (summary.agentMessages > 0) {
    parts.push(
      t("agentMessages", {
        count: summary.agentMessages,
        unit: t(summary.agentMessages === 1 ? "agentMessageSingular" : "agentMessagesPlural"),
      }),
    );
  }
  return parts.join(" · ");
}

export type TimelineGroup =
  // A primary item or a bare marker — rendered on its own.
  | { readonly kind: "single"; readonly item: BackendTimelineItem; readonly index: number }
  // One or more adjacent secondary items folded into one collapsible work block
  // under a "Details" header. Every non-empty secondary run becomes a turn,
  // even a single item — always collapsible, never bare.
  | {
      readonly kind: "turn";
      readonly items: readonly BackendTimelineItem[];
      readonly startIndex: number;
      readonly summary: TurnSummary;
    };

/**
 * Fold adjacent secondary items into turn groups. Order-preserving and pure.
 *
 * - `collapseTurns` false → every item is its own single group (grouping off).
 * - All secondary items are always groupable — including streaming / in-progress
 *   items — so the first streaming chunk immediately lands inside a work block
 *   (no bare-then-wrap layout shift). Primary items and bare markers break the
 *   run as before.
 * - Every non-empty secondary run becomes a turn group (even a single item).
 *   Context-only items (system_prompt, compaction events, lifecycle markers)
 *   fold like any other secondary item — including at the very front of the
 *   timeline (the initial context lands inside the first detail block).
 */
export function groupIntoTurns(
  items: readonly BackendTimelineItem[],
  opts: { readonly collapseTurns: boolean; readonly liveIndex: number | null },
): TimelineGroup[] {
  if (!opts.collapseTurns) {
    return items.map((item, index) => ({ kind: "single", item, index }));
  }

  const groups: TimelineGroup[] = [];
  let run: BackendTimelineItem[] = [];
  let runStart = 0;

  const flush = () => {
    if (run.length === 0) return;
    groups.push({ kind: "turn", items: run, startIndex: runStart, summary: summarizeTurn(run) });
    run = [];
  };

  items.forEach((item, index) => {
    const cls = classifyItem(item);
    const groupable = cls === "secondary";

    if (groupable) {
      if (run.length === 0) runStart = index;
      run.push(item);
    } else {
      flush();
      groups.push({ kind: "single", item, index });
    }
  });
  flush();
  return groups;
}
