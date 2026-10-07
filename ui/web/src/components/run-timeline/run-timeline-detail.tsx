"use client";

// The details of the selected node or message unit: its time and message span,
// summary (rendered as Markdown), the two deterministic costs (the agent's own over the span, and
// the understanding calls that wrote the node), and the raw messages it covers.

import { useInfiniteQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { ChatMarkdown } from "@/components/content/markdown";
import { buttonVariants } from "@/components/ui/button";
import { api } from "@/lib/transport/api";
import { formatTokensCompact } from "@/lib/format/format-number";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";
import { formatAbsolute } from "@/lib/format/time";
import type {
  RunTimelineGeneration,
  RunTimelineMessagePart,
  RunTimelineNode,
  RunTimelineUnit,
  RunTimelineUsage,
} from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";

import { blockClass, cacheHitRate, firstLine, partsForUnit } from "./timeline-model";

const CHIP_SUMMARY_CHARS = 36;

const RAW_PAGE = 50;

function Stat({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div className="rounded border border-border px-2 py-1" title={title}>
      <dt className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="font-mono text-xs tabular-nums">{value}</dd>
    </div>
  );
}

function Tokens({ usage, seconds }: { usage: RunTimelineUsage | RunTimelineGeneration; seconds?: number }) {
  const t = useTranslations("runTimeline");
  const hit = cacheHitRate(usage);
  return (
    <dl className="grid grid-cols-2 gap-1.5 sm:grid-cols-3">
      <Stat label={t("calls")} value={String(usage.calls)} />
      <Stat label={t("input")} value={formatTokensCompact(usage.input)} title={String(usage.input)} />
      <Stat
        label={t("cacheRead")}
        value={formatTokensCompact(usage.cache_read)}
        title={hit === null ? undefined : t("cacheHit", { percent: Math.round(hit * 100) })}
      />
      <Stat label={t("output")} value={formatTokensCompact(usage.output)} title={String(usage.output)} />
      {seconds !== undefined ? <Stat label={t("seconds")} value={`${seconds.toFixed(1)}s`} /> : null}
    </dl>
  );
}

/** The raw messages of an index range, a page at a time; `kind` narrows each message to the unit's own parts. */
function RawMessages({
  agentId,
  start,
  end,
  unitKind,
}: {
  agentId: number;
  start: number;
  end: number;
  unitKind: RunTimelineUnit["kind"] | null;
}) {
  const t = useTranslations("runTimeline");
  const [full, setFull] = useState(false);
  const query = useInfiniteQuery({
    queryKey: ["run-timeline-messages", agentId, start, end, full],
    queryFn: ({ pageParam }) =>
      api.getRunTimelineMessages(agentId, { start: pageParam, end, limit: RAW_PAGE, full }),
    initialPageParam: start,
    getNextPageParam: (last) => last.next_start ?? undefined,
  });
  const messages = query.data?.pages.flatMap((page) => page.messages) ?? [];
  const kindLabel: Record<RunTimelineMessagePart["kind"], string> = {
    think: t("partThink"),
    text: t("partText"),
    call: t("partCall"),
    out: t("partOut"),
    note: t("partNote"),
    compact: t("partCompact"),
    inbound: t("partInbound"),
    attach: t("partAttach"),
    prompt: t("partPrompt"),
  };

  return (
    <section className="space-y-2" data-testid="run-timeline-raw">
      <Heading>{t("rawHeading", { count: end - start + 1 })}</Heading>
      <div className="space-y-2">
        {query.isPending ? <p className="text-xs text-muted-foreground">{t("rawLoading")}</p> : null}
        {query.isError ? (
          <p role="alert" className="text-xs text-destructive">
            {t("rawFailed")}
          </p>
        ) : null}
        <label className={cn(FLEX, "items-center gap-1.5 text-xs text-muted-foreground")}>
          <input type="checkbox" checked={full} onChange={(event) => setFull(event.target.checked)} />
          {t("rawFull")}
        </label>
        {messages.map((message) => {
          const parts = unitKind === null ? message.parts : partsForUnit(unitKind, message.parts);
          if (parts.length === 0) return null;
          return (
            <article key={message.idx} className="space-y-1 rounded border border-border p-2">
              <header className="font-mono text-[10px] text-muted-foreground">
                #{message.idx}
                {message.ts ? ` · ${formatAbsolute(message.ts)}` : ""}
                {message.source ? ` · ${message.source}` : ""}
              </header>
              {parts.map((part, index) => (
                <div key={`${message.idx}-${index}`}>
                  <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                    {kindLabel[part.kind]} · {t("chars", { chars: part.chars })}
                    {part.text_truncated ? ` · ${t("rawTruncated")}` : ""}
                  </div>
                  <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-muted/50 p-1.5 text-[11px] leading-4">
                    {part.text}
                  </pre>
                </div>
              ))}
            </article>
          );
        })}
        {query.hasNextPage ? (
          <button
            type="button"
            disabled={query.isFetchingNextPage}
            onClick={() => void query.fetchNextPage()}
            className={buttonVariants({ size: "sm", variant: "ghost" })}
          >
            {t("rawMore")}
          </button>
        ) : null}
      </div>
    </section>
  );
}

function Heading({ children }: { children: React.ReactNode }) {
  return <h3 className="text-xs font-semibold text-muted-foreground">{children}</h3>;
}

function SpanFacts({
  start,
  end,
  i0,
  i1,
}: {
  start: string;
  end: string;
  i0: number;
  i1: number;
}) {
  const t = useTranslations("runTimeline");
  return (
    <dl className="space-y-0.5 text-xs">
      <div className={cn(FLEX, "gap-2")}>
        <dt className="w-20 shrink-0 text-muted-foreground">{t("timeSpan")}</dt>
        <dd className={cn(MIN_W_0, "font-mono")}>
          {formatAbsolute(start)} – {formatAbsolute(end)}
        </dd>
      </div>
      <div className={cn(FLEX, "gap-2")}>
        <dt className="w-20 shrink-0 text-muted-foreground">{t("messageSpan")}</dt>
        <dd className="font-mono">{t("messageSpanValue", { start: i0, end: i1, count: i1 - i0 + 1 })}</dd>
      </div>
    </dl>
  );
}

/** A node as a button: choosing it selects that node. */
function NodeChip({ node, onSelect }: { node: RunTimelineNode; onSelect: (id: string) => void }) {
  const t = useTranslations("runTimeline");
  return (
    <button
      type="button"
      data-testid="run-timeline-chip"
      data-node-id={node.id}
      onClick={() => onSelect(node.id)}
      className="max-w-full truncate rounded-full border border-border px-2 py-0.5 text-left text-[11px] hover:bg-muted"
    >
      {t("chipLabel", { level: node.level, summary: firstLine(node.summary, CHIP_SUMMARY_CHARS) })}
    </button>
  );
}

function Chips({ heading, nodes, onSelect }: { heading: string; nodes: RunTimelineNode[]; onSelect: (id: string) => void }) {
  if (nodes.length === 0) return null;
  return (
    <section className="space-y-1" data-testid="run-timeline-chips">
      <Heading>{heading}</Heading>
      <div className={cn(FLEX, "flex-wrap gap-1")}>
        {nodes.map((node) => (
          <NodeChip key={node.id} node={node} onSelect={onSelect} />
        ))}
      </div>
    </section>
  );
}

export function NodeDetail({
  agentId,
  node,
  ancestors = [],
  childNodes = [],
  onSelectNode = () => undefined,
  onDrill,
}: {
  agentId: number;
  node: RunTimelineNode;
  /** The node's loaded ancestors, nearest first. */
  ancestors?: RunTimelineNode[];
  /** The loaded nodes one level down that this node groups. */
  childNodes?: RunTimelineNode[];
  onSelectNode?: (id: string) => void;
  onDrill: () => void;
}) {
  const t = useTranslations("runTimeline");
  return (
    <div className="space-y-3" data-testid="run-timeline-node-detail">
      <div className={cn(FLEX, "items-center justify-between gap-2")}>
        <h2 className="text-sm font-semibold">{t("nodeTitle", { level: node.level })}</h2>
        <button type="button" onClick={onDrill} className={buttonVariants({ size: "sm" })}>
          {t("drill")}
        </button>
      </div>
      <SpanFacts start={node.start} end={node.end} i0={node.span_start} i1={node.span_end} />
      <Chips heading={t("ancestorsHeading")} nodes={ancestors} onSelect={onSelectNode} />
      <Chips heading={t("childrenHeading")} nodes={childNodes} onSelect={onSelectNode} />
      <Heading>{t("summaryHeading")}</Heading>
      <div
        className="text-sm leading-5 [&_.chat-md_h1]:text-sm [&_.chat-md_h2]:text-[13px] [&_.chat-md_h3]:text-xs [&_.chat-md_h3]:font-semibold"
        data-testid="run-timeline-summary"
      >
        <ChatMarkdown content={node.summary} />
      </div>
      <Heading>{t("agentCost")}</Heading>
      <Tokens usage={node.usage} />
      <Heading>{t("generationCost")}</Heading>
      {node.generation ? (
        <Tokens usage={node.generation} seconds={node.generation.seconds} />
      ) : (
        <p className="text-xs text-muted-foreground">{t("generationNone")}</p>
      )}
      <RawMessages agentId={agentId} start={node.span_start} end={node.span_end} unitKind={null} />
    </div>
  );
}

export function UnitDetail({
  agentId,
  unit,
  parent = null,
  onSelectNode = () => undefined,
  onDrill,
}: {
  agentId: number;
  unit: RunTimelineUnit;
  /** The level-1 node that covers this block, when it is loaded. */
  parent?: RunTimelineNode | null;
  onSelectNode?: (id: string) => void;
  onDrill: () => void;
}) {
  const t = useTranslations("runTimeline");
  const kind = {
    human: t("blockHuman"),
    agent: t("blockAgent"),
    text: t("blockText"),
    thinking: t("blockThinking"),
    call: t("blockCall"),
    output: t("blockOutput"),
    note: t("blockNote"),
  }[blockClass(unit)];
  return (
    <div className="space-y-3" data-testid="run-timeline-unit-detail">
      <div className={cn(FLEX, "items-center justify-between gap-2")}>
        <h2 className="text-sm font-semibold">{t("unitTitle", { kind })}</h2>
        <button type="button" onClick={onDrill} className={buttonVariants({ size: "sm" })}>
          {t("drill")}
        </button>
      </div>
      <SpanFacts start={unit.start} end={unit.end} i0={unit.i0} i1={unit.i1} />
      {unit.source ? (
        <p className="text-xs">
          <span className="text-muted-foreground">{t("source")}: </span>
          <span className="font-mono">{unit.source}</span>
        </p>
      ) : null}
      {parent !== null ? (
        <Chips heading={t("coveredByHeading")} nodes={[parent]} onSelect={onSelectNode} />
      ) : (
        <p className="text-xs text-muted-foreground" data-testid="run-timeline-uncovered">
          {t("coveredByNone")}
        </p>
      )}
      <RawMessages agentId={agentId} start={unit.i0} end={unit.i1} unitKind={unit.kind} />
    </div>
  );
}
