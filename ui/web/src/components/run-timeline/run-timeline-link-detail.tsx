"use client";

// The details of the selected arrow between agents: what happened, from whom to whom, when, and (for
// an end that is not in the view) a button to bring that agent in.

import { Info } from "lucide-react";
import { useTranslations } from "next-intl";

import { Metric, Section } from "@/components/inspector/inspector-section";
import { buttonVariants } from "@/components/ui/button";
import { formatAbsolute } from "@/lib/format/time";

import { useLinkKindLabels } from "./agent-view/agent-view-link-labels";
import type { ResolvedLink } from "./model/timeline-links";
import { PreviewMessage, TimelineMessages } from "./run-timeline-messages";

export function LinkDetail({ resolved, onAddAgent }: { resolved: ResolvedLink; onAddAgent: (agent: number) => void }) {
  const t = useTranslations("runTimeline");
  const labels = useLinkKindLabels();
  const { link, external } = resolved;
  const end = (agent: number | null) =>
    agent === null ? t("userGroup") : agent === external ? t("linkNotInView", { id: agent }) : `#${agent}`;
  return (
    <div className="space-y-4" data-testid="run-timeline-link-detail">
      <h2 className="text-sm font-semibold">{labels[link.kind]}</h2>
      <Section icon={<Info className="size-3" />} title={t("detailsHeading")}>
        <div className="grid grid-cols-2 gap-1">
          <Metric className="col-span-2" label={t("linkFrom")} value={end(link.sender)} />
          <Metric className="col-span-2" label={t("linkTo")} value={end(link.receiver)} />
          <Metric className="col-span-2" label={t("timeSpan")} value={formatAbsolute(link.ts)} />
          {resolved.userSource !== null ? (
            <Metric className="col-span-2" label={t("source")} value={resolved.userSource} />
          ) : null}
          {link.fork_from !== null ? (
            <Metric className="col-span-2" label={t("linkForkedFrom")} value={`#${link.fork_from}`} />
          ) : null}
          {link.inbound_id !== null ? <Metric label={t("linkInbound")} value={String(link.inbound_id)} /> : null}
        </div>
      </Section>
      {resolved.unmatched ? (
        <p className="text-xs text-muted-foreground" data-testid="run-timeline-link-unmatched">
          {t("linkUnmatched")}
        </p>
      ) : null}
      {external !== null ? (
        <button
          type="button"
          data-testid="run-timeline-link-add-agent"
          onClick={() => onAddAgent(external)}
          className={buttonVariants({ size: "sm" })}
        >
          {t("linkAddAgent", { id: external })}
        </button>
      ) : null}
      {resolved.block !== null ? (
        // The block the arrow ends on: its message, exactly as the details of that block show it.
        <TimelineMessages agentId={resolved.to.agent} start={resolved.block.i0} end={resolved.block.i1} unitKind={resolved.block.kind} />
      ) : link.preview !== null ? (
        <PreviewMessage
          text={link.preview}
          source={link.sender === null ? "user" : `agent:${link.sender}`}
          ts={link.ts}
          kind={link.receiver === null ? "text" : "inbound"}
        />
      ) : null}
    </div>
  );
}
