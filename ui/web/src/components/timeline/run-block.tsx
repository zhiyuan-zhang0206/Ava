"use client";

// TurnBlock — the aggregate collapse for a turn of adjacent secondary items. It is
// a presentational shell around the same clickable summary-header pattern the
// cards use (HEADER_CLS). Its first line leads with SDK actions (or action/context
// counts), delivered attachments, and failure state; its second line carries one
// merged work duration. Expanded children are flat chronological rows with no
// nested disclosure. TimelineView owns expansion and mounts children only while
// open, so collapsed turns still avoid their markdown/code rendering cost.

import { ChevronDown, ChevronRight, Layers } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, useEffect, useState } from "react";

import { formatDuration } from "@/lib/item-summary";
import { cn } from "@/lib/utils";

import { HEADER_CLS } from "./card";
import { formatTurnSummary, type TurnSummary } from "./runs";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0, OVERFLOW_HIDDEN } from "@/lib/layout";

const LIVE_CLOCK_INTERVAL_MS = 100;

export function TurnBlock({
  id,
  memberIds,
  summary,
  expanded,
  onToggle,
  turnActive,
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
  // the turn is idle or its last item is already committed.
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
    ? t("workingFor", { duration: formatDuration(summary.workedMs + liveDelta) })
    : hasWork && summary.workedMs > 0
      ? t("workedFor", { duration: formatDuration(summary.workedMs) })
      : null;

  const actionSummary = formatTurnSummary(summary, (key, values) =>
    t(key as Parameters<typeof t>[0], values),
  );

  return (
    <div
      data-item-id={id}
      data-turn-member-ids={memberIds.join(" ")}
      aria-live="off"
      className={cn(
        "border-l-2 border-dashed border-border/70 rounded-r-sm",
        !expanded && "bg-muted/30",
      )}
    >
      <button
        type="button"
        onClick={onToggle}
        className={cn(HEADER_CLS, "items-start")}
        aria-expanded={expanded}
        data-testid="turn-toggle"
        data-expanded={expanded}
      >
        {expanded ? (
          <ChevronDown className="size-3 shrink-0 opacity-60 mt-0.5" />
        ) : (
          <ChevronRight className="size-3 shrink-0 opacity-60 mt-0.5" />
        )}
        <span className={cn("gap-0.5", FLEX, FLEX_COL, MIN_W_0, FLEX_1)}>
          <span
            data-testid="turn-summary-line"
            className={cn("items-center gap-1.5", FLEX, MIN_W_0)}
          >
            <Layers className="size-3.5 shrink-0" />
            <span className="break-words tabular-nums">
              {summary.failedOutput ? (
                <>
                  <span className="text-destructive">{t("executionFailed")}</span>
                  {actionSummary ? <span className="opacity-50"> · </span> : null}
                </>
              ) : null}
              {actionSummary}
            </span>
          </span>
          {workedLabel ? (
            <span
              data-testid="turn-summary-line"
              className="pl-5 text-muted-foreground tabular-nums"
            >
              {workedLabel}
            </span>
          ) : null}
        </span>
      </button>
      <div
        className={cn(
          "grid transition-[grid-template-rows] duration-200",
          expanded ? "grid-rows-[1fr]" : "grid-rows-[0fr]",
        )}
      >
        <div className={cn(OVERFLOW_HIDDEN, MIN_H_0)}>
          <div className="px-3 pb-2 pt-0.5 space-y-2">{children}</div>
        </div>
      </div>
    </div>
  );
}
