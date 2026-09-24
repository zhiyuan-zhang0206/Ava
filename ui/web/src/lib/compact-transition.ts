import { parseItemIdParts } from "./fold/timeline";
import { isReattachedTimelineContext, standingHeadNoteIds } from "./timeline";
import type { BackendTimelineItem } from "./types";

/** A displayed row waiting for its post-compact, checkpoint-prefixed identity. */
export interface BufferedTimelineRow {
  item: BackendTimelineItem;
  /** Rank this row will have after the current compact. */
  rank: number;
  /** Partial and re-attached context rows are not coverage requirements. */
  needsCanonicalRow: boolean;
}

export interface CompactCoordinate {
  rank: number;
  msg: number;
  block: number;
}

export interface CompactTransitionBuffer {
  threadId: number;
  epoch: number;
  rows: readonly BufferedTimelineRow[];
  /** The oldest committed real item ID in each displayed segment. */
  oldestRealByRank: ReadonlyMap<number, string>;
  /** A no-before HTTP read has established whether history is pageable. */
  tailReady: boolean;
}

export interface BeforeCompactDisplayChange {
  items: readonly BackendTimelineItem[];
  buffer: CompactTransitionBuffer | null;
  rankShift: number;
}

const beforeDisplayChange = new Set<(change: BeforeCompactDisplayChange) => void>();

/** The store calls listeners before publishing a transition to React. */
export function subscribeBeforeCompactDisplayChange(
  listener: (change: BeforeCompactDisplayChange) => void,
): () => void {
  beforeDisplayChange.add(listener);
  return () => { beforeDisplayChange.delete(listener); };
}

export function announceBeforeCompactDisplayChange(change: BeforeCompactDisplayChange): void {
  for (const listener of beforeDisplayChange) listener(change);
}

function rowNeedsCanonicalItem(
  item: BackendTimelineItem,
  headNoteIds: ReadonlySet<string>,
): boolean {
  return !item.partial &&
    parseItemIdParts(item.item_id) !== null &&
    !isReattachedTimelineContext(item) &&
    !headNoteIds.has(item.item_id);
}

/** Capture exactly the old display, including an earlier unfinished transition. */
export function captureCompactTransition(
  items: readonly BackendTimelineItem[],
  previous: CompactTransitionBuffer | null,
  threadId: number,
  epoch: number,
  tailReady: boolean,
): CompactTransitionBuffer | null {
  const headNoteIds = standingHeadNoteIds(items);
  const rows: BufferedTimelineRow[] = previous
    ? previous.rows.map((row) => ({ ...row, rank: row.rank + 1 }))
    : [];
  for (const item of items) {
    const parts = parseItemIdParts(item.item_id);
    if (previous && parts?.rank !== 0) continue; // already hidden behind the previous buffer
    rows.push({
      item,
      rank: (parts?.rank ?? 0) + 1,
      needsCanonicalRow: rowNeedsCanonicalItem(item, headNoteIds),
    });
  }
  if (rows.length === 0) return null;
  const oldestRealByRank = new Map<number, string>();
  for (const row of rows) {
    if (row.needsCanonicalRow && !oldestRealByRank.has(row.rank)) {
      oldestRealByRank.set(row.rank, row.item.item_id);
    }
  }
  return { threadId, epoch, rows, oldestRealByRank, tailReady };
}

/** Historical reads omit messages[0] (the old SystemMessage), shifting a
 * former current segment's message coordinates by one. Older historical
 * segments were already rendered without that message. */
export function canonicalBufferCoordinate(row: BufferedTimelineRow): CompactCoordinate | null {
  const parts = parseItemIdParts(row.item.item_id);
  if (parts === null) return null;
  return { rank: row.rank, msg: parts.msg - (parts.rank === 0 ? 1 : 0), block: parts.block };
}

/** Match canonical rows to the actual coordinate produced by historical paging. */
export function canonicalCoversBuffer(
  buffer: CompactTransitionBuffer,
  canonical: readonly BackendTimelineItem[],
): boolean {
  const coordinates = new Map<string, BackendTimelineItem["kind"]>();
  for (const item of canonical) {
    const parts = parseItemIdParts(item.item_id);
    if (parts?.rank) coordinates.set(`${parts.rank}:${parts.msg}:${parts.block}`, item.kind);
  }
  return buffer.rows.every((row) => {
    if (!row.needsCanonicalRow) return true;
    const coordinate = canonicalBufferCoordinate(row);
    return coordinate === null ||
      coordinates.get(`${coordinate.rank}:${coordinate.msg}:${coordinate.block}`) === row.item.kind;
  });
}
