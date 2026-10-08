"use client";

// The context-size row: one bar per LLM request, as tall as its input tokens. A compaction starts a
// new session, so the bars rise and fall in a sawtooth whose drops are the compact boundaries;
// sessions alternate in color.

import type { MouseEventHandler } from "react";
import { useTranslations } from "next-intl";

import type { RunTimelineRequest, RunTimelineUnit } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";

import { RowShell } from "./run-timeline-row-shell";
import {
  barBox,
  maxAdded,
  maxInput,
  requestLit,
  requestSpan,
  type AxisMap,
  type Hover,
  type Selection,
  type Viewport,
} from "./timeline-model";

const BAR_AREA_PX = 38;

export function ContextSizeRow({
  requests,
  metric = "input",
  axis,
  viewU,
  trackPx,
  units,
  selection,
  onSelect,
  onDrill,
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
  /** Width of the track in pixels: bar widths follow the spacing of the requests on it. */
  trackPx: number;
  units: readonly RunTimelineUnit[];
  selection: Selection | null;
  onSelect: (selection: Selection) => void;
  onDrill: (selection: Selection) => void;
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
  const span = viewU.to - viewU.from;
  return (
    <RowShell
      label={t(added ? "addedContextRow" : "contextRow")}
      height="h-10"
      testId={added ? "run-timeline-row-added" : "run-timeline-row-context"}
    >
      {requests.map((request) => {
        if (top <= 0) return null;
        if (!(span > 0)) return null;
        const box = barBox(requestSpan(request, units, axis), viewU, trackPx);
        if (box.left + box.width < 0 || box.left > trackPx) return null;
        const { selected, hovered } = requestLit(request, selection, hover);
        const target: Selection = { kind: "request", idx: request.idx };
        return (
          <button
            key={request.idx}
            type="button"
            aria-label={describe(request)}
            aria-pressed={selected}
            data-testid={added ? "run-timeline-added" : "run-timeline-request"}
            data-request-idx={request.idx}
            data-session={request.session}
            data-input-tokens={request.input_tokens}
            data-added-tokens={request.added_tokens}
            data-estimated={added && request.added_estimated ? "" : undefined}
            data-selected={selected ? "" : undefined}
            onClick={() => onSelect(target)}
            onDoubleClick={() => onDrill(target)}
            {...hoverProps({ kind: "request", idx: request.idx })}
            className="absolute bottom-0 h-full outline-none focus-visible:ring-2 focus-visible:ring-foreground"
            style={{ left: box.left, width: box.width }}
          >
            <span
              aria-hidden="true"
              className={cn(
                "absolute inset-x-0 bottom-0 block rounded-t-[1px]",
                request.session % 2 === 0 ? "bg-blue-500/70" : "bg-amber-500/80",
                (hovered || selected) && (request.session % 2 === 0 ? "bg-blue-500" : "bg-amber-500"),
                hovered && !selected && "ring-1 ring-foreground/40",
                selected && "ring-2 ring-foreground",
                added && request.added_estimated && !selected && "opacity-60",
              )}
              style={{ height: `${(value(request) / top) * BAR_AREA_PX}px` }}
            />
          </button>
        );
      })}
    </RowShell>
  );
}
