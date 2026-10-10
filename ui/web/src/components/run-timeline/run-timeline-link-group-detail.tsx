"use client";

// The details of a merged arrow: every link it stands for, each a button that selects that link alone
// (the page zooms to it, which also pulls the merged arrow apart).

import { Info } from "lucide-react";
import { useTranslations } from "next-intl";

import { Section } from "@/components/inspector/inspector-section";
import { formatAbsolute } from "@/lib/format/time";
import { cn } from "@/lib/format/utils";
import { FLEX } from "@/lib/layout/layout";

import { useLinkEndLabel, useLinkKindLabels } from "./agent-view/agent-view-link-labels";
import type { ResolvedLink } from "./model/timeline-links";

export function LinkGroupDetail({ links, onSelect }: { links: readonly ResolvedLink[]; onSelect: (key: string) => void }) {
  const t = useTranslations("runTimeline");
  const labels = useLinkKindLabels();
  const endLabel = useLinkEndLabel();
  const sorted = [...links].sort((a, b) => Date.parse(a.link.ts) - Date.parse(b.link.ts));
  const first = sorted[0];
  return (
    <div className="space-y-4" data-testid="run-timeline-link-group-detail">
      <h2 className="text-sm font-semibold">{t("linkGroupTitle", { kind: labels[first.kind], count: sorted.length })}</h2>
      <Section icon={<Info className="size-3" />} title={t("linkGroupHeading")}>
        <ul className="space-y-1">
          {sorted.map((l) => (
            <li key={l.key}>
              <button
                type="button"
                data-testid="run-timeline-link-group-item"
                onClick={() => onSelect(l.key)}
                className={cn(FLEX, "w-full justify-between gap-2 rounded px-2 py-1 text-left font-mono text-[11px] hover:bg-muted")}
              >
                <span>{formatAbsolute(l.link.ts)}</span>
                <span className="text-muted-foreground">{`${endLabel(l.link.sender)} → ${endLabel(l.link.receiver)}`}</span>
              </button>
            </li>
          ))}
        </ul>
      </Section>
    </div>
  );
}
