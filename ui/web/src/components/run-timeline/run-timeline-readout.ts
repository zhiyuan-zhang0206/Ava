// The one-line readout above the run timeline: what the pointer is over.

import type { useTranslations } from "next-intl";

import { formatTokensCompact } from "@/lib/format/format-number";
import { formatShort } from "@/lib/format/time";
import type {
  RunTimelineNode,
  RunTimelineRequest,
  RunTimelineResponse,
  RunTimelineUnit,
} from "@/lib/contracts/types";

import { firstLine, type Hover } from "./timeline-model";

const PREVIEW_CHARS = 80;

type Translate = ReturnType<typeof useTranslations<"runTimeline">>;

export function requestReadout(t: Translate, request: RunTimelineRequest): string {
  return t("readoutRequest", {
    idx: request.idx,
    session: request.session + 1,
    time: formatShort(request.ts),
    tokens: formatTokensCompact(request.input_tokens),
  });
}

function nodeReadout(t: Translate, node: RunTimelineNode): string {
  return t("readoutNode", {
    level: node.level,
    from: formatShort(node.start),
    to: formatShort(node.end),
    start: node.span_start,
    end: node.span_end,
    summary: firstLine(node.summary, PREVIEW_CHARS),
    calls: node.usage.calls,
    input: formatTokensCompact(node.usage.input),
    output: formatTokensCompact(node.usage.output),
  });
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
  if (target.kind === "request") {
    const request = data.requests.find((candidate) => candidate.idx === target.idx);
    return request === undefined ? null : requestReadout(t, request);
  }
  if (target.kind === "node") {
    const node = data.nodes.find((candidate) => candidate.id === target.id);
    return node === undefined ? null : nodeReadout(t, node);
  }
  const unit = data.units.find(
    (candidate) => candidate.i0 === target.i0 && candidate.i1 === target.i1 && candidate.kind === target.unitKind,
  );
  if (unit === undefined) return null;
  return t("readoutUnit", {
    kind: ctx.unitLabel(unit),
    start: unit.i0,
    end: unit.i1,
    time: formatShort(unit.start),
    source: unit.source === null ? t("readoutNoSource") : ctx.sourceLabel(unit.source),
    preview: firstLine(unit.preview, PREVIEW_CHARS),
  });
}
