import { parseItemIdParts } from "@/lib/timeline/timeline";
import type { BackendTimelineItem } from "@/lib/contracts/types";

import { groupIntoTurns, type TimelineGroup } from "./runs";

export interface RenderGroup {
  readonly group: TimelineGroup;
  readonly indexOffset: number;
  readonly rank: number;
}

function segmentKey(item: BackendTimelineItem, rank: number): string {
  const parts = parseItemIdParts(item.item_id);
  return rank > 0 ? `${rank}:${parts?.checkpointId ?? ""}` : "current";
}

/** Keep collapsible runs inside one compact segment without changing items. */
export function groupTimelineSegments(
  items: readonly BackendTimelineItem[],
  rankAt: (item: BackendTimelineItem, index: number) => number =
    (item) => parseItemIdParts(item.item_id)?.rank ?? 0,
): RenderGroup[] {
  const result: RenderGroup[] = [];
  let start = 0;
  while (start < items.length) {
    const rank = rankAt(items[start], start);
    const key = segmentKey(items[start], rank);
    let end = start + 1;
    while (end < items.length && segmentKey(items[end], rankAt(items[end], end)) === key) end += 1;
    const segment = items.slice(start, end);
    const summaryIndex = rank > 0
      ? segment.findIndex((item) => item.kind === "inbound_compact_summary")
      : -1;
    const prefixGroups = summaryIndex > 0
      ? groupIntoTurns(segment.slice(0, summaryIndex), {
          collapseTurns: true,
          liveIndex: null,
        })
      : [];
    const summaryGroups = summaryIndex >= 0
      ? groupIntoTurns(segment.slice(summaryIndex, summaryIndex + 1), {
          collapseTurns: true,
          liveIndex: null,
        })
      : [];
    const rawStart = summaryIndex >= 0 ? summaryIndex + 1 : 0;
    const rawGroups = groupIntoTurns(segment.slice(rawStart), {
      collapseTurns: true,
      liveIndex: null,
    });
    for (const group of prefixGroups) {
      result.push({ group, indexOffset: start, rank });
    }
    for (const group of summaryGroups) {
      result.push({ group, indexOffset: start + summaryIndex, rank });
    }
    for (const group of rawGroups) {
      result.push({ group, indexOffset: start + rawStart, rank });
    }
    start = end;
  }
  return result;
}
