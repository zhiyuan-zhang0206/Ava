"use client";

// The details of the selected arrow between agents: what happened, from whom to whom, when, and (for
// an end that is not in the view) a button to bring that agent in.

import { useQuery } from "@tanstack/react-query";
import { Info } from "lucide-react";
import { useTranslations } from "next-intl";

import { Metric, Section } from "@/components/inspector/inspector-section";
import { buttonVariants } from "@/components/ui/button";
import { formatAbsolute } from "@/lib/format/time";
import { api } from "@/lib/transport/api";

import { useLinkKindLabels } from "./agent-view/agent-view-link-labels";
import type { ResolvedLink } from "./model/timeline-links";
import { PreviewMessage, TimelineMessages } from "./run-timeline-messages";

/** The text of an event with no block: read for the selected arrow only (the list carries none), then shown through the same card and row as any message. */
function LinkText({ link }: { link: ResolvedLink["link"] }) {
  const t = useTranslations("runTimeline");
  const ref = link.inbound_id !== null ? { inbound_id: link.inbound_id } : link.notice_id !== null ? { notice_id: link.notice_id } : null;
  const read = useQuery({
    queryKey: ["run-timeline-link-content", ref],
    queryFn: () => (ref === null ? Promise.reject(new Error("an event with no text to read")) : api.getRunTimelineLinkContent(ref)),
    enabled: ref !== null,
    staleTime: Infinity,
  });
  if (ref === null) return null;
  if (read.isError) {
    return (
      <p role="alert" className="text-xs text-destructive">
        {t("messagesFailed")}
      </p>
    );
  }
  if (read.data === undefined) return <p className="text-xs text-muted-foreground">{t("messagesLoading")}</p>;
  return (
    <>
      {read.data.title !== null ? (
        <div className="grid grid-cols-2 gap-1">
          <Metric className="col-span-2" label={t("linkTitle")} value={read.data.title} />
        </div>
      ) : null}
      <PreviewMessage
        text={read.data.content}
        source={link.sender === null ? "user" : `agent:${link.sender}`}
        ts={link.ts}
        kind={link.receiver === null ? "text" : "inbound"}
      />
    </>
  );
}

export function LinkDetail({ resolved, onAddAgent }: { resolved: ResolvedLink; onAddAgent: (agent: number) => void }) {
  const t = useTranslations("runTimeline");
  const labels = useLinkKindLabels();
  const { link, external } = resolved;
  const end = (agent: number | null) =>
    agent === null ? t("userGroup") : agent === external ? t("linkNotInView", { id: agent }) : `#${agent}`;
  return (
    <div className="space-y-4" data-testid="run-timeline-link-detail">
      <h2 className="text-sm font-semibold">{labels[resolved.kind]}</h2>
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
      ) : (
        <LinkText link={link} />
      )}
    </div>
  );
}
