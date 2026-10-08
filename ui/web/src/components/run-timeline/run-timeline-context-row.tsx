"use client";

// The context-size row: one bar per LLM request, as tall as its input tokens. A compaction starts a
// new session, so the bars rise and fall in a sawtooth whose drops are the compact boundaries;
// sessions alternate in color.

import type { MouseEventHandler } from "react";
import { useTranslations } from "next-intl";

import type { RunTimelineRequest } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";

import { RowShell } from "./run-timeline-row-shell";
import { axisBox, maxAdded, maxInput, type AxisMap, type Hover, type Viewport } from "./timeline-model";

const BAR_AREA_PX = 38;

export function ContextSizeRow({
  requests,
  metric = "input",
  axis,
  viewU,
  hover,
  hoverProps,
  describe,
}: {
  requests: readonly RunTimelineRequest[];
  /** What a bar's height is: the request's whole input (absolute) or what it newly added (relative). Each scales to its own largest. */
  metric?: "input" | "added";
  axis: AxisMap;
  /** The viewport in the axis's coordinates. */
  viewU: Viewport;
  hover: Hover | null;
  hoverProps: (target: Hover) => {
    onMouseEnter: MouseEventHandler;
    onMouseLeave: MouseEventHandler;
    onFocus: () => void;
    onBlur: () => void;
  };
  /** The text of one request, for its accessible name. */
  describe: (request: RunTimelineRequest) => string;
}) {
  const t = useTranslations("runTimeline");
  const added = metric === "added";
  // Additions are mostly small next to the few big ones, so their height is the square root of the count.
  const value = (request: RunTimelineRequest) => (added ? Math.sqrt(request.added_tokens) : request.input_tokens);
  const top = added ? Math.sqrt(maxAdded(requests)) : maxInput(requests);
  return (
    <RowShell
      label={t(added ? "addedContextRow" : "contextRow")}
      height="h-10"
      testId={added ? "run-timeline-row-added" : "run-timeline-row-context"}
    >
      {requests.map((request) => {
        const box = axisBox(axis, request.ts, request.ts, viewU);
        if (box === null || top <= 0) return null;
        const hovered = hover?.kind === "request" && hover.idx === request.idx;
        return (
          <span
            key={request.idx}
            role="img"
            aria-label={describe(request)}
            data-testid={added ? "run-timeline-added" : "run-timeline-request"}
            data-request-idx={request.idx}
            data-session={request.session}
            data-input-tokens={request.input_tokens}
            data-added-tokens={request.added_tokens}
            data-estimated={added && request.added_estimated ? "" : undefined}
            {...hoverProps({ kind: "request", idx: request.idx })}
            className="absolute bottom-0 h-full -translate-x-1/2 px-px"
            style={{ left: `${box.left}%` }}
          >
            <span
              aria-hidden="true"
              className={cn(
                "absolute bottom-0 left-1/2 block w-[2px] -translate-x-1/2 rounded-t-[1px]",
                request.session % 2 === 0 ? "bg-blue-500/70" : "bg-amber-500/80",
                hovered && "bg-foreground",
                added && request.added_estimated && "opacity-60",
              )}
              style={{ height: `${(value(request) / top) * BAR_AREA_PX}px` }}
            />
          </span>
        );
      })}
    </RowShell>
  );
}
