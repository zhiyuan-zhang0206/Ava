"use client";

// Message detail panel for the raw-context strip (P4-2, task #4023): the
// message's meta, part split, covering summary chain, its actions, and the
// on-demand text fetch (`GET .../run-timeline/message`). Text clipped by the
// read budget offers an explicit full-text refetch (the 3187 review
// condition); the body scrolls instead of truncating the card.

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "@/lib/api";
import { FLEX, MIN_W_0 } from "@/lib/layout";
import type { RunTimelineMessage, RunTimelineMessageDetails, RunTimelineResponse } from "@/lib/types";
import { cn } from "@/lib/utils";

import type { TimelineContextView } from "./context-view";
import { DetailMetric, layerFocusLabel, timestampLabel, type RunTimelineChartLabels } from "./run-timeline-details";
import type { TimelineWindowOverride } from "./request-level";
import { stripMessageClass, stripPartClass, type StripColorClass } from "./strip-categories";

type LayerNode = NonNullable<RunTimelineResponse["layers"]>[number];

/** P4-2b (#4023): the panel's focus action in whichever x domain is active —
 *  a time window, or a char range on the context axis. The chart builds the
 *  target and routes it back through the page. */
export type MessageFocusTarget =
  | { kind: "time"; window: TimelineWindowOverride }
  | { kind: "context"; view: TimelineContextView };

function PartBody({
  part,
  source,
  labels,
}: {
  part: RunTimelineMessageDetails["parts"][number];
  source: string | null;
  labels: RunTimelineChartLabels;
}) {
  const colorClass = stripPartClass(part.kind, source);
  const summary =
    labels.messagePart(labels.stripPartLabels[colorClass], part.chars.toLocaleString()) +
    (part.text_truncated ? ` · ${labels.messagePartTruncated}` : "");
  // Thinking and tool-call bodies fold by default; every other part reads as
  // body text. Both keep the scrollable panel body honest — nothing is cut.
  if (part.kind === "think" || part.kind === "call") {
    return (
      <details className="rounded-[10px] border border-border bg-muted/40 px-3 py-2">
        <summary className="cursor-pointer font-mono text-[11px] text-muted-foreground">{summary}</summary>
        <pre className="mt-2 whitespace-pre-wrap break-words font-mono text-[11px] leading-5">
          {part.text}
        </pre>
      </details>
    );
  }
  return (
    <div className="rounded-[10px] border border-border bg-muted/40 px-3 py-2">
      <p className="font-mono text-[10px] text-muted-foreground">{summary}</p>
      <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-[11px] leading-5">
        {part.text}
      </pre>
    </div>
  );
}

export function MessageDetailPanel({
  agentId,
  message,
  chain,
  labels,
  focusTarget,
  onFocus,
  onClose,
  onSelectLayer,
  fullText,
}: {
  agentId: number;
  message: RunTimelineMessage;
  /** Covering summary chain, ancestors first — each chip selects its node. */
  chain: { index: number; node: LayerNode }[];
  labels: RunTimelineChartLabels;
  focusTarget: MessageFocusTarget | null;
  onFocus: (target: MessageFocusTarget, label: string) => void;
  onClose: () => void;
  onSelectLayer: (index: number) => void;
  /** Chart-owned read choice survives moving between inline and desktop reader. */
  fullText?: { expanded: boolean; onExpand: () => void };
}) {
  const [localFull, setLocalFull] = useState(false);
  const full = fullText?.expanded ?? localFull;
  const details = useQuery({
    queryKey: ["run-timeline-message", agentId, message.key, full],
    queryFn: () => api.getRunTimelineMessage(agentId, message.key, { full }),
    retry: false,
    // The full-text refetch keeps the clipped text visible while it loads.
    placeholderData: keepPreviousData,
  });

  const colorClass = stripMessageClass(message);
  const mergedParts = message.parts.reduce<{ colorClass: StripColorClass; chars: number }[]>(
    (accumulator, part) => {
      const partClass = stripPartClass(part.kind, message.source);
      const last = accumulator.at(-1);
      if (last?.colorClass === partClass) {
        last.chars += part.chars;
      } else {
        accumulator.push({ colorClass: partClass, chars: part.chars });
      }
      return accumulator;
    },
    [],
  );

  return (
    <aside
      role="region"
      aria-label={labels.messageDetails}
      className="relative z-10 h-fit space-y-3 rounded-[10px] border border-border bg-card p-4 text-foreground"
    >
      <header className={cn(FLEX, "items-start justify-between gap-3")}>
        <div className={MIN_W_0}>
          <p className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
            {labels.messageDetails}
          </p>
          <h3 className={cn(FLEX, "items-center gap-2 text-sm font-semibold")}>
            <span
              aria-hidden="true"
              className="inline-block size-2.5 shrink-0 rounded-[3px]"
              style={{ background: `var(--strip-${colorClass})` }}
            />
            <span className="truncate">
              {labels.messageLabel(message.idx)} · {labels.stripPartLabels[colorClass]}
            </span>
          </h3>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label={labels.closeDetails}
          className="rounded-md px-1.5 py-0.5 text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          ×
        </button>
      </header>

      <dl className="grid grid-cols-2 gap-2">
        <div className="col-span-2">
          <DetailMetric
            label={labels.timestamp}
            value={message.ts === null ? labels.none : timestampLabel(message.ts)}
          />
        </div>
        <DetailMetric label={labels.kind} value={labels.stripPartLabels[colorClass]} />
        <DetailMetric label={labels.messageCharsLabel} value={labels.messageChars(message.chars.toLocaleString())} />
        <div className="col-span-2">
          <DetailMetric label={labels.messageSource} value={message.source ?? labels.none} />
        </div>
      </dl>

      {mergedParts.length > 0 ? (
        <div className={cn(FLEX, "flex-wrap gap-1.5")}>
          {mergedParts.map((part, index) => (
            <span
              key={`${part.colorClass}-${index}`}
              className="rounded-md border border-border bg-muted px-2 py-1 font-mono text-[10px]"
            >
              {labels.messagePart(labels.stripPartLabels[part.colorClass], part.chars.toLocaleString())}
            </span>
          ))}
        </div>
      ) : null}

      {chain.length > 0 ? (
        <section className="space-y-1">
          <h4 className="text-xs font-semibold">{labels.messageChain}</h4>
          <div className={cn(FLEX, "flex-wrap gap-1.5")}>
            {chain.map(({ index, node }) => (
              <button
                key={node.id}
                type="button"
                onClick={() => onSelectLayer(index)}
                className="rounded-md border border-border bg-muted px-2 py-1 font-mono text-[10px] hover:bg-background"
              >
                {layerFocusLabel(node)}
              </button>
            ))}
          </div>
        </section>
      ) : null}

      <div className={cn(FLEX, "flex-wrap gap-2")}>
        <button
          type="button"
          disabled={focusTarget === null}
          onClick={() => {
            if (focusTarget) onFocus(focusTarget, labels.messageLabel(message.idx));
          }}
          className="rounded border border-border px-2 py-1 font-mono text-xs hover:bg-muted disabled:opacity-50"
        >
          {labels.messageFocus}
        </button>
        {chain.length > 0 ? (
          <button
            type="button"
            onClick={() => onSelectLayer(chain[chain.length - 1].index)}
            className="rounded border border-border px-2 py-1 font-mono text-xs hover:bg-muted"
          >
            {labels.messageShowSummary}
          </button>
        ) : null}
      </div>

      <section className="space-y-2">
        <h4 className="text-xs font-semibold">{labels.messageBody}</h4>
        {details.isError ? (
          <div className="space-y-2 text-xs text-destructive">
            <p>{labels.messageLoadFailed}</p>
            <button
              type="button"
              onClick={() => void details.refetch()}
              className="rounded border border-border px-2 py-1 font-mono text-xs text-foreground hover:bg-muted"
            >
              {labels.retry}
            </button>
          </div>
        ) : details.isPending ? (
          <p className="text-xs text-muted-foreground">{labels.messageLoading}</p>
        ) : (
          <div className="max-h-[420px] space-y-2 overflow-y-auto pr-1">
            {details.data.parts.length === 0 ? (
              <p className="text-xs text-muted-foreground">{labels.messageNoText}</p>
            ) : (
              details.data.parts.map((part, index) => (
                <PartBody key={`${part.kind}-${index}`} part={part} source={message.source} labels={labels} />
              ))
            )}
            {details.data.content_truncated && !full ? (
              <button
                type="button"
                onClick={() => fullText ? fullText.onExpand() : setLocalFull(true)}
                className="rounded border border-border px-2 py-1 font-mono text-xs hover:bg-muted"
              >
                {labels.messageExpandFull}
              </button>
            ) : null}
          </div>
        )}
      </section>
    </aside>
  );
}
