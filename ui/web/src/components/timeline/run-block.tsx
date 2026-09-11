"use client";

// TurnBlock — the aggregate collapse for a turn of adjacent secondary items. It is
// a presentational shell around the same clickable summary-header pattern the
// cards use (HEADER_CLS): collapsed it shows "worked for X" plus the action
// counts ("1 thinking · 1 code · 1 output") and, when the turn has anything worth
// surfacing, a second wrapping line of aggregate wall-clock ("thought 15m · ran
// 3m") and SDK call names × counts ("files.read × 3 · shell.run × 2") — enough
// for "did the agent think a lot, run a lot of commands, or mostly talk to other
// agents" at a glance without expanding. Expanded it reveals its children — the
// individual TimelineRows, each still its own collapsible card. The parent
// (TimelineView) owns the expanded state and builds the children only when
// expanded, so a collapsed turn never mounts its inner rows (and never re-parses
// their markdown / code on a streaming chunk).
//
// Sticky header behavior (shared with the message cards — see card.tsx
// CardHeader): when an expanded block's top scrolls past the viewport top
// (accounting for the floating HeaderBar offset, BAR_HEIGHT_PX), its header
// sticks at top-11 with backdrop-blur and elevation. The full detail rows
// (thinking/code/output) continue scrolling naturally underneath the stuck
// header. A top-level message card (CardHeader with stickyHeader) pins on the
// same line; findClosestStuckHeaderId resolves which of the two, and only one
// can be pinned at a time (sibling blocks never overlap). When collapsed while
// stuck, the content collapses in place and the viewport scroll position is
// preserved (no jump back to block top).

import { ChevronDown, ChevronRight, Layers } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, useEffect, useState } from "react";

import { formatDuration, type SdkCall } from "@/lib/item-summary";
import { cn } from "@/lib/utils";

import { CallBadge, HEADER_CLS, STICKY_HEADER_CLS, STUCK_HEADER_CLS, UNSTUCK_HEADER_CLS } from "./card";
import { formatTurnSummary, formatTurnTiming, type TurnSummary } from "./runs";
import { BAR_HEIGHT_PX, FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0, OVERFLOW_HIDDEN } from "@/lib/layout";

const LIVE_CLOCK_INTERVAL_MS = 100;

/**
 * Identify which expanded header (if any) is currently stuck at the top of the
 * viewport. Two surfaces share this one sticky line: a work block's header
 * (`data-turn-expanded="true"` on the turn element) and a top-level message
 * card's header (`data-card-sticky="true"` on the row). Card rows inside an
 * expanded work block are deliberately NOT markers — their block's own header
 * is the sticky one, so two headers never pin at the same line.
 *
 * Sibling blocks never overlap in flow, so at most one candidate is stuck at a
 * time; when multiple cross the line (a mid-scroll intermediate state), returns
 * the latest/closest one that crossed the viewport top (highest top position
 * that is <= stickyLine).
 */
export function findClosestStuckHeaderId(
  viewport: HTMLElement,
  topOffset: number = BAR_HEIGHT_PX,
): string | null {
  const elements = viewport.querySelectorAll<HTMLElement>(
    '[data-turn-expanded="true"], [data-card-sticky="true"]',
  );
  if (elements.length === 0) return null;

  const vpRect = viewport.getBoundingClientRect();
  const stickyLine = vpRect.top + topOffset;

  let closestId: string | null = null;
  let closestTop = -Infinity;

  for (const el of elements) {
    const id = el.getAttribute("data-turn-id") ?? el.getAttribute("data-item-id");
    if (!id) continue;

    const r = el.getBoundingClientRect();
    // A header is stuck when its block's top has reached or passed stickyLine,
    // and its bottom has not completely scrolled past stickyLine (with header buffer).
    if (r.top <= stickyLine + 1 && r.bottom > stickyLine + 20) {
      if (r.top >= closestTop) {
        closestTop = r.top;
        closestId = id;
      }
    }
  }

  return closestId;
}

export function TurnBlock({
  id,
  memberIds,
  summary,
  expanded,
  onToggle,
  turnActive,
  isStuck,
  children,
}: {
  // The turn's first member item_id, stamped as data-item-id so the load-older
  // scroll anchor has a stable node even when the topmost content is a collapsed
  // turn (whose inner rows — and their own data-item-id — are not mounted).
  id: string;
  // Every member's item_id, space-joined onto data-turn-member-ids. A turn's
  // FIRST member changes across a load-older prepend whenever the fetched
  // older window extends the turn's front (a new older secondary item joins
  // ahead of the current first) — data-item-id alone then no longer matches
  // an anchor captured before the prepend, even though that item is still
  // right there, just no longer first. The scroll-anchor lookup in
  // TimelineView matches on this attribute too so it finds the turn
  // regardless of which member id was captured.
  memberIds: readonly string[];
  summary: TurnSummary;
  expanded: boolean;
  onToggle: () => void;
  // Whether the agent is mid-turn — drives the live "working for X" / "worked for X" clock.
  turnActive?: boolean;
  // Whether this turn block is currently actively stuck at the top of the viewport.
  isStuck?: boolean;
  // The inner rows — passed only when expanded (null when collapsed).
  children?: ReactNode;
}) {
  // Live clock: while a block of this turn is streaming, tick every
  // LIVE_CLOCK_INTERVAL_MS so the displayed elapsed time advances in real
  // time. The streaming block is the ONLY thing that moves the display —
  // every other input is a committed number that changes when a new summary
  // arrives — so an active turn sitting between blocks does not re-render 10x
  // a second. The `liveNow` state is updated only inside the setInterval
  // callback (never synchronously in the effect body); the lazy initialiser
  // seeds it with Date.now().
  const t = useTranslations("timeline");
  const [liveNow, setLiveNow] = useState(() => Date.now());
  const liveBlockStartedAt =
    turnActive && summary.lastLiveKind != null && summary.lastLiveStartedAt > 0
      ? summary.lastLiveStartedAt
      : 0;

  useEffect(() => {
    if (liveBlockStartedAt <= 0) return;
    const id = setInterval(() => {
      setLiveNow(Date.now());
    }, LIVE_CLOCK_INTERVAL_MS);
    return () => clearInterval(id);
  }, [liveBlockStartedAt]);

  // How long the block that is streaming right now has been running. Zero when
  // the turn is idle or its last item is already committed. One value, read by
  // both the header clock and the sub-block timing line below, so the two
  // cannot disagree about the block in flight.
  const liveDelta = liveBlockStartedAt > 0 ? Math.max(liveNow - liveBlockStartedAt, 0) : 0;

  // The header timer, two states over ONE basis — the sum of the turn's block
  // durations. A turn is a maximal run of secondary items, so it can span a
  // restart marker or a wake-up and the idle gap before the agent picked the
  // work back up; wall-clock across the turn charges that gap as work.
  // - LIVE (turnActive): "Working for Xs" = the committed workedMs plus the
  //   in-flight block's elapsed. Unconditional — it does not wait for a work
  //   item to land, so the clock is present from the first moment of the turn
  //   (reading zero while the LLM is still silent after a system wake-up).
  // - DONE: "Worked for Xs" = workedMs alone. The block that was in flight has
  //   committed its duration into it, so the number does not jump at the
  //   handover. Shown only when the turn contains actual agent work — a turn of
  //   only system notes shows its counts ("1 system note") with no timer (the
  //   notes are instantaneous inserts, not work).
  const hasWork = summary.thinking > 0 || summary.code > 0 || summary.output > 0;
  const workedLabel = turnActive
    ? `Working for ${formatDuration(summary.workedMs + liveDelta)}`
    : hasWork && summary.workedMs > 0
      ? `Worked for ${formatDuration(summary.workedMs)}`
      : null;

  const actionSummary = formatTurnSummary(summary);

  // Build the header line: "Worked for Xs · 1 thinking · 1 code · 1 output".
  // actionSummary is non-empty for every non-empty turn (summarizeTurn counts
  // every member kind), so the header never renders blank.
  const headerParts: string[] = [];
  if (workedLabel) headerParts.push(workedLabel);
  if (actionSummary) headerParts.push(actionSummary);

  // Live sub-block timing: while a block is streaming, add the same live delta
  // the header clock uses to the committed aggregate, so the "Thought for Xs ·
  // Wrote code for Xs · Ran for Xs" line ticks in sync with "Working for Xs" —
  // even while collapsed. With no block in flight, fall back to the static
  // committed values (same as formatTurnTiming).
  const timing = (() => {
    if (liveBlockStartedAt > 0) {
      const parts: string[] = [];
      const thinkingMs = summary.thinkingMs + (summary.lastLiveKind === "reasoning" ? liveDelta : 0);
      const codeMs = summary.codeMs + (summary.lastLiveKind === "code" ? liveDelta : 0);
      const execMs = summary.execMs + (summary.lastLiveKind === "output" ? liveDelta : 0);
      if (thinkingMs > 0) parts.push(`Thought for ${formatDuration(thinkingMs)}`);
      if (codeMs > 0) parts.push(`Wrote code for ${formatDuration(codeMs)}`);
      if (execMs > 0) parts.push(`Ran for ${formatDuration(execMs)}`);
      return parts.length > 0 ? parts.join(" · ") : null;
    }
    return formatTurnTiming(summary, (key, values) =>
      t(key as Parameters<typeof t>[0], values),
    );
  })();
  const hasSdkCalls = summary.sdkCalls.length > 0;

  return (
    <div
      data-item-id={id}
      data-turn-id={id}
      data-turn-expanded={expanded}
      data-turn-member-ids={memberIds.join(" ")}
      aria-live="off"
      className={cn(
        "relative border-l-2 border-dashed border-border/70 rounded-r-sm",
        !expanded && "bg-muted/30",
      )}
    >
      <button
        type="button"
        onClick={onToggle}
        className={cn(
          HEADER_CLS,
          "items-start",
          // STICKY_HEADER_CLS pins below the floating HeaderBar (top-11 =
          // BAR_HEIGHT_PX / BAR_HEIGHT_CLASS in @/lib/layout); the stuck variant
          // masks the detail rows scrolling beneath it. Shared with CardHeader.
          expanded && STICKY_HEADER_CLS,
          expanded && (isStuck ? STUCK_HEADER_CLS : UNSTUCK_HEADER_CLS),
        )}
        aria-expanded={expanded}
        data-testid="turn-toggle"
        data-expanded={expanded}
        data-stuck={expanded && Boolean(isStuck) ? "true" : "false"}
      >
        {expanded ? (
          <ChevronDown className="size-3 shrink-0 opacity-60 mt-0.5" />
        ) : (
          <ChevronRight className="size-3 shrink-0 opacity-60 mt-0.5" />
        )}
        <span className={cn("gap-0.5", FLEX, FLEX_COL, MIN_W_0, FLEX_1)}>
          {/* Line 1: Worked for Xs · N thinking · M code · K output · ...
              tabular-nums: the live clock rewrites the digits ~10x/s; fixed-
              width numerals keep the line from wobbling as digits change. */}
          <span className={cn("items-center gap-1.5", FLEX, MIN_W_0)}>
            <Layers className="size-3.5 shrink-0" />
            <span className="break-all tabular-nums">{headerParts.join(" · ") || ""}</span>
          </span>
          {/* Line 2: Thought for Xs · Wrote code for Xs · Ran for Xs */}
          {timing ? (
            <span className="pl-5 opacity-70 break-all tabular-nums">{timing}</span>
          ) : null}
          {/* Line 3: SDK calls — full list, natural word wrap */}
          {hasSdkCalls ? (
            <span className={cn("flex-wrap items-center gap-x-1.5 gap-y-0.5 pl-5 opacity-70", FLEX)}>
              {summary.sdkCalls.map((c, i) => (
                <TurnCallChip key={c.method} call={c} showDot={i > 0} />
              ))}
            </span>
          ) : null}
        </span>
      </button>
      <div
        className={cn(
          "grid transition-[grid-template-rows] duration-200 ease-out motion-reduce:transition-none",
          expanded ? "grid-rows-[1fr]" : "grid-rows-[0fr]",
        )}
      >
        <div className={cn(OVERFLOW_HIDDEN, MIN_H_0)}>
          <div className="px-2 pb-2 pt-0.5 space-y-3">{children}</div>
        </div>
      </div>
    </div>
  );
}

function TurnCallChip({ call, showDot }: { call: SdkCall; showDot: boolean }) {
  return (
    <span>
      {showDot ? <span className="opacity-50">{"· "}</span> : null}
      <CallBadge call={call} />
    </span>
  );
}
