"use client";

import { useLayoutEffect, useRef, useState } from "react";

import {
  canonicalBufferCoordinate,
  subscribeBeforeCompactDisplayChange,
  type CompactCoordinate,
  type CompactTransitionBuffer,
} from "@/lib/compact-transition";
import type { StickyController } from "@/lib/sticky";
import { isReattachedTimelineContext, parseItemIdParts, standingHeadNoteIds } from "@/lib/timeline";
import { useTimelineStore } from "@/lib/timeline-store";

interface CompactAnchor {
  rank: number;
  msg: number;
  block: number;
  screenTop: number;
}

export type CompactPin = Pick<CompactAnchor, "rank" | "msg" | "block">;

function nodeCoordinates(
  node: HTMLElement,
  bufferedCoordinates: ReadonlyMap<string, CompactCoordinate>,
  includeMembers = false,
): (CompactCoordinate | null)[] {
  const ids = [node.dataset.itemId, ...(includeMembers ? node.dataset.turnMemberIds?.split(" ") ?? [] : [])];
  const rank = Number(node.dataset.displayRank);
  return ids.filter((id): id is string => !!id).map((id) =>
    node.dataset.timelineSource === "buffer"
      ? bufferedCoordinates.get(`${rank}:${id}`) ?? null
      : parseItemIdParts(id),
  );
}

function matchesAnchor(
  node: HTMLElement,
  anchor: CompactAnchor,
  bufferedCoordinates: ReadonlyMap<string, CompactCoordinate>,
  includeMembers = false,
): boolean {
  return Number(node.dataset.displayRank) === anchor.rank &&
    nodeCoordinates(node, bufferedCoordinates, includeMembers)
      .some((parts) => parts?.msg === anchor.msg && parts.block === anchor.block);
}

/** Resolve auto-sized rows before using a newly keyed anchor's position. */
function measureRenderedAnchor(viewport: HTMLElement, anchorNode: HTMLElement): number {
  let anchorTop = anchorNode.getBoundingClientRect().top;
  for (;;) {
    let forced = false;
    for (const node of viewport.querySelectorAll<HTMLElement>(".timeline-item")) {
      if (node === anchorNode || node.contains(anchorNode)) break;
      if (node.style.contentVisibility === "visible") continue;
      if (node.getBoundingClientRect().top >= anchorTop) break;
      node.style.contentVisibility = "visible";
      forced = true;
    }
    if (!forced) break;
    const nextTop = anchorNode.getBoundingClientRect().top;
    if (nextTop === anchorTop) break;
    anchorTop = nextTop;
  }
  void viewport.getBoundingClientRect();
  // These nodes must keep their measured height until reconciliation. Newly
  // keyed off-screen rows can lose their auto-size estimate again if restored
  // before a paint, moving the anchor out of view before the next page lands.
  return anchorNode.getBoundingClientRect().top;
}

function materializeViewportRows(viewport: HTMLElement): void {
  const bounds = viewport.getBoundingClientRect();
  for (const node of viewport.querySelectorAll<HTMLElement>(".timeline-item")) {
    const box = node.getBoundingClientRect();
    if (box.bottom > bounds.top && box.top < bounds.bottom) {
      node.style.contentVisibility = "visible";
    }
  }
  void viewport.getBoundingClientRect();
}

/** Transfer a visible row across a compact re-key before the next paint. */
export function useCompactTransitionAnchor(options: {
  controller: StickyController;
  viewportRef: { current: HTMLElement | null };
  prependAnchorRef: { current: { id: string; frontId: string | null; docTop: number } | null };
  compactBuffer: CompactTransitionBuffer | null;
}): CompactPin | null {
  const { controller, viewportRef, prependAnchorRef, compactBuffer } = options;
  const anchorRef = useRef<CompactAnchor | null>(null);
  const [pin, setPin] = useState<CompactPin | null>(null);
  const compactReplaceSeq = useTimelineStore((s) => s.compactReplaceSeq);

  // The store announces the transition before publishing it to React.
  // Ordinary Zustand subscribers and layout cleanup can run after mutation.
  useLayoutEffect(() => subscribeBeforeCompactDisplayChange((change) => {
    if (anchorRef.current) return; // multiple store writes can batch into one render
    prependAnchorRef.current = null;
    if (controller.isSticky()) return;
    const viewport = viewportRef.current;
    if (!viewport) return;
    const rect = viewport.getBoundingClientRect();
    const oldHeadNotes = standingHeadNoteIds(change.items);
    for (const node of viewport.querySelectorAll<HTMLElement>(
      ".timeline-item[data-item-id][data-display-rank], [data-turn-expanded='false'][data-item-id][data-display-rank]",
    )) {
      const box = node.getBoundingClientRect();
      if (box.bottom < rect.top || box.top > rect.bottom) continue;
      const id = node.dataset.itemId;
      const parts = id ? parseItemIdParts(id) : null;
      if (!id || !parts) continue;
      const rank = Number(node.dataset.displayRank);
      const bufferedRow = node.dataset.timelineSource === "buffer"
        ? change.buffer?.rows.find((row) => row.rank === rank && row.item.item_id === id)
        : null;
      const real = node.dataset.timelineSource === "buffer"
        ? bufferedRow?.needsCanonicalRow
        : change.items.some((item) => item.item_id === id && !item.partial &&
            !isReattachedTimelineContext(item) && !oldHeadNotes.has(id));
      if (!real) continue;
      const canonicalCoordinate = bufferedRow ? canonicalBufferCoordinate(bufferedRow) : null;
      anchorRef.current = {
        rank: rank + change.rankShift,
        msg: canonicalCoordinate?.msg ?? parts.msg - (rank === 0 && change.rankShift > 0 ? 1 : 0),
        block: parts.block,
        screenTop: box.top,
      };
      setPin({ rank: anchorRef.current.rank, msg: anchorRef.current.msg, block: anchorRef.current.block });
      return;
    }
  }), [controller, viewportRef, prependAnchorRef]);

  useLayoutEffect(() => {
    const anchor = anchorRef.current;
    if (!anchor) return;
    anchorRef.current = null;
    const viewport = viewportRef.current;
    if (!viewport) return;
    const bufferedCoordinates = new Map<string, CompactCoordinate>();
    for (const row of compactBuffer?.rows ?? []) {
      const coordinate = canonicalBufferCoordinate(row);
      if (!coordinate) continue;
      bufferedCoordinates.set(`${row.rank}:${row.item.item_id}`, coordinate);
    }
    const nodes = [...viewport.querySelectorAll<HTMLElement>(
      ".timeline-item[data-item-id][data-display-rank], [data-turn-expanded='false'][data-item-id][data-display-rank]",
    )];
    let target = nodes.find((node) => matchesAnchor(node, anchor, bufferedCoordinates)) ??
      nodes.find((node) => matchesAnchor(node, anchor, bufferedCoordinates, true));
    if (!target) {
      // A finite retention budget may exclude the anchor. Use the nearest
      // surviving coordinate in the same or adjacent segment.
      let nearestDistance: [number, number, number] | null = null;
      for (const node of nodes) {
        const parts = nodeCoordinates(node, bufferedCoordinates).find((value) => value !== null);
        if (!parts) continue;
        const distance: [number, number, number] = [
          Math.abs(Number(node.dataset.displayRank) - anchor.rank),
          Math.abs(parts.msg - anchor.msg),
          Math.abs(parts.block - anchor.block),
        ];
        if (
          nearestDistance === null ||
          distance[0] < nearestDistance[0] ||
          (distance[0] === nearestDistance[0] && distance[1] < nearestDistance[1]) ||
          (distance[0] === nearestDistance[0] && distance[1] === nearestDistance[1] &&
            distance[2] < nearestDistance[2])
        ) {
          target = node;
          nearestDistance = distance;
        }
      }
    }
    if (target) {
      viewport.scrollTop += measureRenderedAnchor(viewport, target) - anchor.screenTop;
      materializeViewportRows(viewport);
      viewport.scrollTop += target.getBoundingClientRect().top - anchor.screenTop;
    }
    setPin(null);
  }, [compactBuffer, compactReplaceSeq, viewportRef]);
  return pin;
}
