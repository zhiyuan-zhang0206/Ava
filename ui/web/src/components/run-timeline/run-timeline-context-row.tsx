"use client";

// The context-size row: one bar per LLM request, as tall as its input tokens. A compaction starts a
// new session, so the bars rise and fall in a sawtooth whose drops are the compact boundaries;
// sessions alternate in color.

import type { MouseEventHandler } from "react";
import { useTranslations } from "next-intl";

import type { RunTimelineRequest } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";

import { RowShell } from "./run-timeline-row-shell";
import { maxInput, spanBox, type Hover, type TimelineWindow } from "./timeline-model";

const BAR_AREA_PX = 38;

export function ContextSizeRow({
  requests,
  visible,
  hover,
  hoverProps,
  describe,
}: {
  requests: readonly RunTimelineRequest[];
  visible: TimelineWindow;
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
  const top = maxInput(requests);
  return (
    <RowShell label={t("contextRow")} height="h-10" testId="run-timeline-row-context">
      {requests.map((request) => {
        const box = spanBox(request.ts, request.ts, visible);
        if (box === null || top <= 0) return null;
        const hovered = hover?.kind === "request" && hover.idx === request.idx;
        return (
          <span
            key={request.idx}
            role="img"
            aria-label={describe(request)}
            data-testid="run-timeline-request"
            data-request-idx={request.idx}
            data-session={request.session}
            data-input-tokens={request.input_tokens}
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
              )}
              style={{ height: `${(request.input_tokens / top) * BAR_AREA_PX}px` }}
            />
          </span>
        );
      })}
    </RowShell>
  );
}
