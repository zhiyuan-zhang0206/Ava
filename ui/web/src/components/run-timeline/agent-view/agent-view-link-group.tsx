"use client";

// The last groups of the agent view, each a heading and one row of events with no tree, Messages or
// Context rows. "Other agents" holds the events whose other end is an agent not in the view; "User"
// holds the events with the user, who is no agent. Each event is a colored tick at its time and the
// end of an arrow whose other end is an agent in the view. Neither group can be removed.

import { X } from "lucide-react";
import { useTranslations } from "next-intl";

import { buttonVariants } from "@/components/ui/button";

import { FLEX } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

import { endIn, LINK_COLORS, type ResolvedLink } from "../model/timeline-links";
import { projectBox, type AxisMap, type Viewport } from "../model/timeline-model";
import { RowShell } from "../run-timeline-row-shell";
import { useLinkEndLabel, useLinkKindLabels } from "./agent-view-link-labels";

export function LinkRowGroup({
  variant,
  links,
  axis,
  viewU,
  selectedKey,
  onHover,
  onSelect,
  onClose,
}: {
  variant: "user" | "other";
  /** The links with an end in this group, left to right. */
  links: readonly ResolvedLink[];
  axis: AxisMap;
  viewU: Viewport;
  selectedKey: string | null;
  onHover: (key: string | null) => void;
  onSelect: (key: string) => void;
  /** Hides the group and its arrows: the same state as its switch in the toolbar. */
  onClose: () => void;
}) {
  const t = useTranslations("runTimeline");
  const labels = useLinkKindLabels();
  const endLabel = useLinkEndLabel();
  const title = variant === "user" ? t("userGroup") : t("otherAgents");
  return (
    <section aria-label={title} data-testid={`agent-view-${variant}`} className="space-y-1.5">
      <div className={cn(FLEX, "items-center gap-2 pl-[88px] text-xs")}>
        <span className="truncate font-mono" data-testid={`agent-view-${variant}-title`}>
          {title}
        </span>
        <button
          type="button"
          aria-label={t("closeGroup", { title })}
          title={t("closeGroup", { title })}
          data-testid={`agent-view-${variant}-close`}
          onClick={onClose}
          className={cn(buttonVariants({ size: "sm", variant: "ghost" }), "h-5 px-1")}
        >
          <X className="size-3.5" aria-hidden />
        </button>
      </div>
      <RowShell label={t("otherRow")} height="h-5" testId={`run-timeline-row-${variant}`}>
        {links.map((l) => {
          const ms = endIn(l, variant)?.ms;
          const box = ms === undefined ? null : projectBox(axis.toU(ms), axis.toU(ms), viewU);
          if (box === null) return null;
          const selected = l.key === selectedKey;
          return (
            <button
              key={l.key}
              type="button"
              tabIndex={-1}
              data-testid="run-timeline-link-tick"
              aria-pressed={selected}
              aria-label={t("linkAria", { kind: labels[l.kind], from: endLabel(l.link.sender), to: endLabel(l.link.receiver) })}
              onPointerEnter={() => onHover(l.key)}
              onPointerLeave={() => onHover(null)}
              onClick={(event) => {
                event.stopPropagation();
                onSelect(l.key);
              }}
              className={cn("absolute top-1 h-3 w-1.5 -translate-x-1/2 rounded-sm", selected && "ring-2 ring-foreground")}
              style={{ left: `${box.left}%`, background: LINK_COLORS[l.kind] }}
            />
          );
        })}
      </RowShell>
    </section>
  );
}
