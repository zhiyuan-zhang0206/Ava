"use client";

// The messages an index range covers, a page at a time, each rendered the way the conversation
// renders it (`ItemView`, the conversation's own renderer) with
// the context tokens it occupies; `unitKind` narrows each message to the unit's own parts.

import { useInfiniteQuery } from "@tanstack/react-query";
import { MessagesSquare } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Section } from "@/components/inspector/inspector-section";
import { cardConfigFor, TimelineRow } from "@/components/timeline/row";
import { buttonVariants } from "@/components/ui/button";
import { useTimelineColors } from "@/lib/timeline/use-timeline-colors";
import { api } from "@/lib/transport/api";
import { formatTokensCompact } from "@/lib/format/format-number";
import { FLEX } from "@/lib/layout/layout";
import type { BackendTimelineItem, RunTimelineMessage, RunTimelineMessagePart, RunTimelineUnit } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";

import { partsForUnit } from "./model/timeline-model";

const PAGE = 50;

/** What a message occupies in the context; `estimated` says it is a share, not the provider's number. */
export interface MessageTokens {
  tokens: number;
  estimated: boolean;
}

const ITEM_KIND: Record<RunTimelineMessagePart["kind"], BackendTimelineItem["kind"]> = {
  think: "agent_reasoning",
  text: "agent_chat",
  call: "agent_code",
  out: "code_output",
  note: "system_marker",
  compact: "inbound_compact_summary",
  inbound: "inbound_chat",
  attach: "attach",
  prompt: "system_prompt",
};

/** A part as the conversation's own item, so the conversation's own row renders it: the same
 *  card, header, components and type scale as the main page. */
function partItem(message: RunTimelineMessage, part: RunTimelineMessagePart, index: number): BackendTimelineItem {
  return {
    item_id: `${message.idx}.${index}`,
    kind: ITEM_KIND[part.kind],
    source: message.source,
    payload: part.text,
    created_at: message.ts,
    inbound_id: null,
    show_timestamp: true,
  };
}

/** One part as the conversation's row, open and collapsible like there. */
function PartRow({ item }: { item: BackendTimelineItem }) {
  const [expanded, setExpanded] = useState(true);
  const config = cardConfigFor(item, useTimelineColors());
  return (
    <TimelineRow
      item={item}
      config={config}
      streaming={false}
      expanded={expanded}
      showActions
      isStuck={false}
      onToggle={() => setExpanded((open) => !open)}
      onFork={null}
      forkPending={false}
    />
  );
}

function MessageCard({
  message,
  parts,
  tokens,
}: {
  message: RunTimelineMessage;
  parts: readonly RunTimelineMessagePart[];
  tokens: MessageTokens | null;
}) {
  const t = useTranslations("runTimeline");
  return (
    <article className="space-y-1.5 rounded border border-border p-2.5" data-testid="run-timeline-message">
      <header className={cn("items-baseline gap-2 font-mono text-[10px] text-muted-foreground", FLEX)}>
        <span>{message.source ?? ""}</span>
        {tokens !== null ? (
          <span className="ml-auto shrink-0 tabular-nums" data-testid="run-timeline-message-tokens">
            {t("tokensValue", { tokens: formatTokensCompact(tokens.tokens) })}
            {tokens.estimated ? ` ${t("estimatedSuffix")}` : ""}
          </span>
        ) : null}
      </header>
      {/* The conversation column's own type scale (timeline/index.tsx). */}
      <div className="space-y-1 text-[13px] leading-relaxed">
        {parts.map((part, index) => (
          <PartRow key={`${message.idx}-${index}`} item={partItem(message, part, index)} />
        ))}
      </div>
    </article>
  );
}

export function TimelineMessages({
  agentId,
  start,
  end,
  unitKind,
  partTokens = null,
}: {
  agentId: number;
  start: number;
  end: number;
  unitKind: RunTimelineUnit["kind"] | null;
  /** The tokens of the one part a thinking / text / tool-call unit shows; the message as a whole holds the others. */
  partTokens?: MessageTokens | null;
}) {
  const t = useTranslations("runTimeline");
  const query = useInfiniteQuery({
    queryKey: ["run-timeline-messages", agentId, start, end],
    queryFn: ({ pageParam }) => api.getRunTimelineMessages(agentId, { start: pageParam, end, limit: PAGE, full: true }),
    initialPageParam: start,
    getNextPageParam: (last) => last.next_start ?? undefined,
  });
  const messages = query.data?.pages.flatMap((page) => page.messages) ?? [];

  return (
    <Section icon={<MessagesSquare className="size-3" />} title={t("messagesHeading", { count: end - start + 1 })}>
      <div className="space-y-2" data-testid="run-timeline-messages">
        {query.isPending ? <p className="text-xs text-muted-foreground">{t("messagesLoading")}</p> : null}
        {query.isError ? (
          <p role="alert" className="text-xs text-destructive">
            {t("messagesFailed")}
          </p>
        ) : null}
        {messages.map((message) => {
          const parts = unitKind === null ? message.parts : partsForUnit(unitKind, message.parts);
          if (parts.length === 0) return null;
          const whole =
            message.context_tokens === null || message.estimated === null
              ? null
              : { tokens: message.context_tokens, estimated: message.estimated };
          return (
            <MessageCard
              key={message.idx}
              message={message}
              parts={parts}
              tokens={partTokens ?? whole}
            />
          );
        })}
        {query.hasNextPage ? (
          <button
            type="button"
            disabled={query.isFetchingNextPage}
            onClick={() => void query.fetchNextPage()}
            className={buttonVariants({ size: "sm", variant: "ghost" })}
          >
            {t("messagesMore")}
          </button>
        ) : null}
      </div>
    </Section>
  );
}
