// The one-line readout above the run timeline: what the pointer is over.

import type { useTranslations } from "next-intl";

import { approx, formatTokensCompact } from "@/lib/format/format-number";
import { formatShort } from "@/lib/format/time";
import type {
  RunTimelineNode,
  RunTimelineResponse,
  RunTimelineUnit,
} from "@/lib/contracts/types";

import { firstLine, type Hover } from "./timeline-model";

const PREVIEW_CHARS = 80;

type Translate = ReturnType<typeof useTranslations<"runTimeline">>;

function nodeReadout(t: Translate, node: RunTimelineNode): string {
  const line = t("readoutNode", {
    level: node.level,
    from: formatShort(node.start),
    to: formatShort(node.end),
    count: node.span_end - node.span_start + 1,
    summary: firstLine(node.summary, PREVIEW_CHARS),
    calls: node.usage.calls,
    input: formatTokensCompact(node.usage.input),
    output: formatTokensCompact(node.usage.output),
  });
  if (node.context_tokens === null) return line;
  return `${line} · ${t("readoutUnitTokens", { tokens: `${approx(node.estimated)}${formatTokensCompact(node.context_tokens)}` })}`;
}

/** The readout of what is hovered, or null when nothing is (or it is no longer in the data). */
export function readoutText(
  target: Hover | null,
  ctx: {
    data: RunTimelineResponse;
    t: Translate;
    unitLabel: (unit: RunTimelineUnit) => string;
    sourceLabel: (source: string) => string;
  },
): string | null {
  const { data, t } = ctx;
  if (target === null) return null;
  if (target.kind === "node") {
    const node = data.nodes.find((candidate) => candidate.id === target.id);
    return node === undefined ? null : nodeReadout(t, node);
  }
  const unit = data.units.find(
    (candidate) => candidate.i0 === target.i0 && candidate.i1 === target.i1 && candidate.kind === target.unitKind,
  );
  if (unit === undefined) return null;
  const line = t("readoutUnit", {
    kind: ctx.unitLabel(unit),
    count: unit.i1 - unit.i0 + 1,
    time: formatShort(unit.start),
    source: unit.source === null ? t("readoutNoSource") : ctx.sourceLabel(unit.source),
    preview: firstLine(unit.preview, PREVIEW_CHARS),
  });
  if (unit.context_tokens === null) return line;
  const tokens = t("readoutUnitTokens", { tokens: `${approx(unit.estimated)}${formatTokensCompact(unit.context_tokens)}` });
  const context = unit.context_total === null ? "" : ` · ${t("readoutUnitContext", { total: formatTokensCompact(unit.context_total) })}`;
  return `${line} · ${tokens}${context}`;
}
