"use client";

// The last group of the agent view: the agents that are not in it. One row of events, each the end of
// an arrow whose other end is an agent in the view, at the time of the event. Adding the peer to the
// view (from the event's details) moves the arrow to that agent's own group.

import { useTranslations } from "next-intl";

import { FLEX } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

import { LINK_COLORS, type ResolvedLink } from "../model/timeline-links";
import { projectBox, type AxisMap, type Viewport } from "../model/timeline-model";
import { RowShell } from "../run-timeline-row-shell";
import { useLinkKindLabels } from "./agent-view-link-labels";

export function OtherAgentsGroup({
  links,
  axis,
  viewU,
  selectedKey,
  onHover,
  onSelect,
}: {
  /** The links whose other end is not in the view, left to right. */
  links: readonly ResolvedLink[];
  axis: AxisMap;
  viewU: Viewport;
  selectedKey: string | null;
  onHover: (key: string | null) => void;
  onSelect: (key: string) => void;
}) {
  const t = useTranslations("runTimeline");
  const labels = useLinkKindLabels();
  return (
    <section aria-label={t("otherAgents")} data-testid="agent-view-other-agents" className="space-y-1.5">
      <div className={cn(FLEX, "items-center gap-2 pl-[88px] text-xs")}>
        <span className="truncate font-mono" data-testid="agent-view-other-title">
          {t("otherAgents")}
        </span>
      </div>
      <RowShell label={t("otherRow")} height="h-5" testId="run-timeline-row-other">
        {links.map((l) => {
          const box = projectBox(axis.toU(l.from.ms), axis.toU(l.from.ms), viewU);
          if (box === null) return null;
          const selected = l.key === selectedKey;
          return (
            <button
              key={l.key}
              type="button"
              tabIndex={-1}
              data-testid="run-timeline-other-event"
              aria-pressed={selected}
              aria-label={t("linkAria", { kind: labels[l.link.kind], from: l.link.sender, to: l.link.receiver })}
              onPointerEnter={() => onHover(l.key)}
              onPointerLeave={() => onHover(null)}
              onClick={(event) => {
                event.stopPropagation();
                onSelect(l.key);
              }}
              className={cn("absolute top-1 h-3 w-1.5 -translate-x-1/2 rounded-sm", selected && "ring-2 ring-foreground")}
              style={{ left: `${box.left}%`, background: LINK_COLORS[l.link.kind] }}
            />
          );
        })}
      </RowShell>
    </section>
  );
}
