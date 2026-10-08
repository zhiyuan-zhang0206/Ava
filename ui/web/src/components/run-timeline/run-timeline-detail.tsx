"use client";

// The details of the selected node or message unit: one Details section (time, message span,
// context, and for a node the agent's own usage and cost over the span), the node's summary
// (Markdown), its place in the tree, and the messages it covers.

import { FileText, GitBranch, Info } from "lucide-react";
import { useTranslations } from "next-intl";

import { Metric, Section } from "@/components/inspector/inspector-section";
import { formatTokensCompact } from "@/lib/format/format-number";
import { FLEX } from "@/lib/layout/layout";
import { formatAbsolute } from "@/lib/format/time";
import type { RunTimelineNode, RunTimelineUnit, RunTimelineUsage } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";

import { TimelineMarkdown } from "./run-timeline-markdown";
import { TimelineMessages, type MessageTokens } from "./run-timeline-messages";
import { blockClass, firstLine } from "./timeline-model";

const CHIP_SUMMARY_CHARS = 36;

/** The unit kinds that are one part of an AIMessage's turn: the message as a whole holds the others. */
const TURN_PART_KINDS: readonly RunTimelineUnit["kind"][] = ["thinking", "text", "call"];

/** The agent's own calls over a span, priced from what each call recorded. */
function UsageMetrics({ usage }: { usage: RunTimelineUsage }) {
  const t = useTranslations("runTimeline");
  return (
    <>
      <Metric label={t("calls")} value={String(usage.calls)} />
      <Metric label={t("input")} value={formatTokensCompact(usage.input)} />
      <Metric label={t("cacheRead")} value={formatTokensCompact(usage.cache_read)} />
      <Metric label={t("cacheWrite")} value={formatTokensCompact(usage.cache_write)} />
      <Metric label={t("output")} value={formatTokensCompact(usage.output)} />
      <Metric
        label={t("cost")}
        value={usage.cost_calls === 0 ? t("costUnknown") : `$${usage.cost_usd.toFixed(4)}`}
        sub={
          usage.cost_calls > 0 && usage.cost_calls < usage.calls
            ? t("costPartial", { priced: usage.cost_calls, calls: usage.calls })
            : undefined
        }
      />
    </>
  );
}

function Details({
  start,
  end,
  i0,
  i1,
  tokens,
  estimated,
  source = null,
  usage = null,
}: {
  start: string;
  end: string;
  i0: number;
  i1: number;
  /** What the span occupies in the context, when a request has read it. */
  tokens: number | null;
  estimated: boolean | null;
  source?: string | null;
  usage?: RunTimelineUsage | null;
}) {
  const t = useTranslations("runTimeline");
  return (
    <Section icon={<Info className="size-3" />} title={t("detailsHeading")}>
      <div className="grid grid-cols-2 gap-1" data-testid="run-timeline-details">
        <Metric
          className="col-span-2"
          label={t("timeSpan")}
          value={`${formatAbsolute(start)} – ${formatAbsolute(end)}`}
        />
        <Metric label={t("messageSpan")} value={t("messageSpanValue", { start: i0, end: i1, count: i1 - i0 + 1 })} />
        {tokens !== null ? (
          <Metric
            label={t("contextTokens")}
            value={`${t("tokensValue", { tokens: formatTokensCompact(tokens) })}${estimated === true ? ` ${t("estimatedSuffix")}` : ""}`}
            valueTestId="run-timeline-detail-tokens"
          />
        ) : null}
        {source ? <Metric className="col-span-2" label={t("source")} value={source} /> : null}
        {usage ? <UsageMetrics usage={usage} /> : null}
      </div>
    </Section>
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
    <div data-testid="run-timeline-chips">
      <Section icon={<GitBranch className="size-3" />} title={heading}>
        <div className={cn(FLEX, "flex-wrap gap-1")}>
          {nodes.map((node) => (
            <NodeChip key={node.id} node={node} onSelect={onSelect} />
          ))}
        </div>
      </Section>
    </div>
  );
}

export function NodeDetail({
  agentId,
  node,
  ancestors = [],
  childNodes = [],
  onSelectNode = () => undefined,
}: {
  agentId: number;
  node: RunTimelineNode;
  /** The node's loaded ancestors, nearest first. */
  ancestors?: RunTimelineNode[];
  /** The loaded nodes one level down that this node groups. */
  childNodes?: RunTimelineNode[];
  onSelectNode?: (id: string) => void;
}) {
  const t = useTranslations("runTimeline");
  return (
    <div className="space-y-4" data-testid="run-timeline-node-detail">
      <h2 className="text-sm font-semibold">{t("nodeTitle", { level: node.level })}</h2>
      <Details
        start={node.start}
        end={node.end}
        i0={node.span_start}
        i1={node.span_end}
        tokens={node.context_tokens}
        estimated={node.estimated}
        usage={node.usage}
      />
      <Chips heading={t("ancestorsHeading")} nodes={ancestors} onSelect={onSelectNode} />
      <Chips heading={t("childrenHeading")} nodes={childNodes} onSelect={onSelectNode} />
      <Section icon={<FileText className="size-3" />} title={t("summaryHeading")}>
        <TimelineMarkdown content={node.summary} testId="run-timeline-summary" />
      </Section>
      <TimelineMessages agentId={agentId} start={node.span_start} end={node.span_end} unitKind={null} />
    </div>
  );
}

export function UnitDetail({
  agentId,
  unit,
  parent = null,
  onSelectNode = () => undefined,
}: {
  agentId: number;
  unit: RunTimelineUnit;
  /** The level-1 node that covers this block, when it is loaded. */
  parent?: RunTimelineNode | null;
  onSelectNode?: (id: string) => void;
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
  const partTokens: MessageTokens | null =
    TURN_PART_KINDS.includes(unit.kind) && unit.context_tokens !== null && unit.estimated !== null
      ? { tokens: unit.context_tokens, estimated: unit.estimated }
      : null;
  return (
    <div className="space-y-4" data-testid="run-timeline-unit-detail">
      <h2 className="text-sm font-semibold">{t("unitTitle", { kind })}</h2>
      <Details
        start={unit.start}
        end={unit.end}
        i0={unit.i0}
        i1={unit.i1}
        tokens={unit.context_tokens}
        estimated={unit.estimated}
        source={unit.source}
      />
      {parent !== null ? (
        <Chips heading={t("coveredByHeading")} nodes={[parent]} onSelect={onSelectNode} />
      ) : (
        <p className="text-xs text-muted-foreground" data-testid="run-timeline-uncovered">
          {t("coveredByNone")}
        </p>
      )}
      <TimelineMessages agentId={agentId} start={unit.i0} end={unit.i1} unitKind={unit.kind} partTokens={partTokens} />
    </div>
  );
}
