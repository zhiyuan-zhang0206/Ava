"use client";

// The message connectors over the lanes. Each runs from the sender's lane at the time it sent to the
// receiver's lane at the time the receiver's claim step took it (`read_at`); an unclaimed message
// is a dashed vertical at its send time. Both ends are agent-level: nothing here says which
// message the receiver was handling or what woke it.

import { useTranslations } from "next-intl";
import { useId } from "react";

import type { Viewport } from "../run-timeline/timeline-model";
import { edgePath, edgeQueueSeconds, formatSeconds, timeX, type DrawnEdge } from "./cluster-model";

// The lane tracks start this far from the left edge of the lanes (label column and gap).
const TRACK_OFFSET_PX = 88;

export function ClusterEdges({
  edges,
  centers,
  colors,
  view,
  trackPx,
  open,
  onHint,
}: {
  edges: readonly DrawnEdge[];
  /** Each drawn lane's vertical centre in the lanes' own coordinates. */
  centers: ReadonlyMap<number, number>;
  colors: ReadonlyMap<number, string>;
  view: Viewport;
  trackPx: number;
  /** The open lane: its edges stay at full strength, the others recede. */
  open: number | null;
  onHint: (text: string | null) => void;
}) {
  const t = useTranslations("clusterView");
  const clipId = useId();
  return (
    <svg
      aria-hidden="true"
      data-testid="cluster-edges"
      className="pointer-events-none absolute inset-0 size-full"
    >
      <defs>
        <clipPath id={clipId}>
          <rect x={TRACK_OFFSET_PX} y={0} width={trackPx} height="100%" />
        </clipPath>
      </defs>
      {edges.map((drawn, index) => {
        const { edge } = drawn;
        const y0 = centers.get(drawn.from);
        const y1 = centers.get(drawn.to);
        if (y0 === undefined || y1 === undefined) return null;
        const sent = Date.parse(edge.sent_at);
        const read = edge.read_at === null ? null : Date.parse(edge.read_at);
        const x0 = TRACK_OFFSET_PX + timeX(sent, view, trackPx);
        const x1 = read === null ? x0 : TRACK_OFFSET_PX + timeX(read, view, trackPx);
        // Off the shown window on both ends: nothing to draw.
        if (Math.max(x0, x1) < TRACK_OFFSET_PX || Math.min(x0, x1) > TRACK_OFFSET_PX + trackPx) return null;
        const color = colors.get(drawn.from) ?? "var(--foreground)";
        const queue = edgeQueueSeconds(edge);
        const text = t("edgeHint", {
          sender: edge.sender,
          receiver: edge.receiver,
          queue: queue === null ? t("edgeUnread") : formatSeconds(queue),
          preview: edge.preview,
        });
        const touches = open === null || drawn.edge.sender === open || drawn.edge.receiver === open;
        const path = read === null ? `M ${x0} ${y0} L ${x0} ${y1}` : edgePath(x0, y0, x1, y1);
        return (
          <g
            key={`${edge.inbound_id ?? "x"}-${index}`}
            opacity={touches ? 0.9 : 0.2}
            clipPath={`url(#${clipId})`}
            data-testid="cluster-edge"
          >
            <path
              d={path}
              fill="none"
              stroke={color}
              strokeWidth={drawn.aggregated ? 2 : 1}
              strokeDasharray={read === null ? "3 3" : undefined}
            />
            <circle cx={x0} cy={y0} r={2} fill={color} />
            {read === null ? null : <circle cx={x1} cy={y1} r={2.5} fill="var(--background)" stroke={color} />}
            <path
              d={path}
              fill="none"
              stroke="transparent"
              strokeWidth={8}
              style={{ pointerEvents: "stroke" }}
              onMouseEnter={() => onHint(text)}
              onMouseLeave={() => onHint(null)}
            />
          </g>
        );
      })}
    </svg>
  );
}
