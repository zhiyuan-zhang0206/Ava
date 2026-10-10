"use client";

import { useCallback, useDeferredValue, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import type { SavedScroll } from "@/lib/layout/scroll-memory";
import { parseItemIdParts } from "@/lib/timeline/timeline";
import type { BackendTimelineItem } from "@/lib/contracts/types";
import { useDisplayLimit } from "@/lib/format/display-limits";

const GAP = 12; // space-y-3 on the timeline and expanded turn body
// KEEP (task #3696 exception inventory): task #4709 fixes one viewport of buffer on each side as window geometry, not an operator limit.
const BUFFER_VIEWPORTS = 1;
// KEEP (task #3696 exception inventory): 24 is roughly three 8-row screens before geometry exists; it caps initial DOM mounts, not a user window.
const BOOTSTRAP_WINDOW_ROWS = 24;
const BOOTSTRAP_PIN_LEAD_ROWS = BOOTSTRAP_WINDOW_ROWS / 2;
// KEEP (task #3696 exception inventory): 680px estimates one screen only until the real viewport is measured; steady behavior uses that measurement.
const UNMEASURED_VIEWPORT_HEIGHT_PX = 680;
// KEEP (task #3696 exception inventory): 500 keys cover twice the default 250-item follower slice before cache sweeping; this is housekeeping.
const HEIGHT_CACHE_SWEEP_FLOOR = 500;
const ACTIVATION_ROWS_FALLBACK = 100;
const TURN_ROWS_FALLBACK = 100;
const MEASURE_ROWS_FALLBACK = 75;

export function useTimelineWindowLimits() {
  const configuredActivationRows = useDisplayLimit("AVA_TIMELINE_WINDOW_ACTIVATION_ROWS", ACTIVATION_ROWS_FALLBACK);
  const activationRows = useDeferredValue(configuredActivationRows);
  const turnRows = useDisplayLimit("AVA_TIMELINE_WINDOW_TURN_ROWS", TURN_ROWS_FALLBACK);
  // A lower runtime threshold first renders with measurement active, then switches
  // the DOM window on the next commit so the parked row can be pinned.
  const measureRows = Math.min(
    useDisplayLimit("AVA_TIMELINE_WINDOW_MEASURE_ROWS", MEASURE_ROWS_FALLBACK),
    configuredActivationRows - 1, activationRows - 1,
  );
  return { activationRows, turnRows, measureRows };
}

/** Locate a saved reading item, including a child whose turn is now collapsed. */
export function resolveSavedTimelineAnchor(
  viewport: HTMLElement,
  anchor: NonNullable<SavedScroll["anchor"]>,
  compactBuffer: CompactTransitionBuffer | null,
  canonicalItems: readonly BackendTimelineItem[],
) {
  const id = CSS.escape(anchor.itemId);
  const rank = anchor.rank;
  const exact = viewport.querySelector<HTMLElement>(
    `.timeline-item[data-item-id="${id}"][data-display-rank="${rank}"], [data-turn-expanded="false"][data-item-id="${id}"][data-display-rank="${rank}"]`,
  );
  const collapsed = exact ? null : viewport.querySelector<HTMLElement>(
    `[data-turn-expanded="false"][data-turn-member-ids~="${id}"][data-display-rank="${rank}"]`,
  );
  const buffered = compactBuffer?.rows.some((row) => row.rank === rank && row.item.item_id === anchor.itemId) === true;
  const canonical = canonicalItems.some((item) => item.item_id === anchor.itemId &&
    (parseItemIdParts(item.item_id)?.rank ?? 0) === rank);
  return {
    node: exact ?? collapsed,
    present: buffered || canonical,
    viewportTop: collapsed ? Math.max(52, anchor.viewportTop) : anchor.viewportTop,
  };
}

/** Restore only when this layout can hold the reading position. */
export function applySavedTimelineScroll(
  viewport: HTMLElement,
  saved: SavedScroll,
  compactBuffer: CompactTransitionBuffer | null,
  canonicalItems: readonly BackendTimelineItem[],
): boolean {
  const max = viewport.scrollHeight - viewport.clientHeight;
  if (max <= 0) return false;
  const target = saved.followBottom || !saved.anchor ? null
    : resolveSavedTimelineAnchor(viewport, saved.anchor, compactBuffer, canonicalItems);
  if (target?.present && !target.node) return false;
  const top = saved.followBottom ? max : target?.node
    ? viewport.scrollTop + target.node.getBoundingClientRect().top - viewport.getBoundingClientRect().top - target.viewportTop
    : saved.scrollTop;
  if (top > max) return false;
  viewport.scrollTop = Math.max(0, top);
  return true;
}

export interface WindowGroup {
  key: string;
  rank: number;
  itemIds: readonly string[];
  estimatedHeight: number;
  expandedRows: readonly string[] | null;
}

export interface WindowRange {
  start: number;
  end: number;
  before: number;
  after: number;
}

function visibleRow(content: HTMLElement, rect: DOMRect): HTMLElement | null {
  const intersects = (node: HTMLElement) => {
    const box = node.getBoundingClientRect();
    return box.bottom > rect.top && box.top < rect.bottom;
  };
  return [...content.querySelectorAll<HTMLElement>(".timeline-item")].find(intersects) ??
    [...content.querySelectorAll<HTMLElement>("[data-turn-id]")].find(intersects) ?? null;
}

function keepFocusInTimeline(content: HTMLElement, rect: DOMRect): void {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement) || !content.contains(active)) return;
  const row = active.closest<HTMLElement>(".timeline-item, [data-turn-id]");
  if (!row) return;
  const box = row.getBoundingClientRect();
  if (box.bottom >= rect.top - rect.height && box.top <= rect.bottom + rect.height) return;
  content.tabIndex = -1;
  content.focus({ preventScroll: true });
}

function span(sizes: readonly number[], start: number, end: number): number {
  if (end <= start) return 0;
  let height = 0;
  for (let index = start; index < end; index++) height += sizes[index] + GAP;
  return height - GAP;
}

/** The remote rows become two spacers; only the viewport and one viewport on each side mount. */
export function windowForSizes(
  sizes: readonly number[],
  top: number,
  height: number,
  origin = 0,
): WindowRange {
  const min = Math.max(0, top - height * BUFFER_VIEWPORTS - origin);
  const max = Math.max(min, top + height * (1 + BUFFER_VIEWPORTS) - origin);
  let cursor = 0;
  let start = 0;
  while (start < sizes.length - 1 && cursor + sizes[start] + GAP < min) {
    cursor += sizes[start] + GAP;
    start++;
  }
  let end = start;
  while (end < sizes.length && (cursor < max || end === start)) {
    cursor += sizes[end] + GAP;
    end++;
  }
  return {
    start,
    end,
    before: span(sizes, 0, start),
    after: span(sizes, end, sizes.length),
  };
}

interface Options {
  groups: readonly WindowGroup[];
  enabled: boolean;
  turnRows: number;
  measureRows: number;
  identity: string | null;
  viewportRef: { current: HTMLElement | null };
  contentRef: { current: HTMLElement | null };
  isFollowing: () => boolean;
  pendingAnchor: { id: string; rank: number } | null;
  restoreTop: number | null;
}

export function useTimelineWindow({
  groups,
  enabled,
  turnRows,
  measureRows,
  identity,
  viewportRef,
  contentRef,
  isFollowing,
  pendingAnchor,
  restoreTop,
}: Options) {
  const [heights, setHeights] = useState(() => new Map<string, number>());
  const [visiblePin, setVisiblePin] = useState<{ id: string; rank: number; identity: string | null } | null>(null);
  if (isFollowing() && visiblePin !== null) setVisiblePin(null);
  const previousIdentityRef = useRef(identity);
  const [view, setView] = useState({ top: restoreTop ?? 0, height: 0, origin: 52 });
  const viewRef = useRef(view);
  const readingRef = useRef<{ node: HTMLElement; top: number; id: string; rank: number; identity: string | null } | null>(null);
  const preserveRef = useRef<typeof readingRef.current>(null);
  const tracking = enabled || groups.reduce((count, group) =>
    count + (group.expandedRows?.length ?? 1), 0) >= measureRows;
  const rememberVisible = useCallback(() => {
    // Bottom commands hand position ownership back to the sticky controller.
    // A parked row must neither cancel its smooth scroll nor pin the old window.
    if (isFollowing()) {
      readingRef.current = null;
      preserveRef.current = null;
      return;
    }
    const content = contentRef.current;
    const viewport = viewportRef.current;
    const node = content && viewport ? visibleRow(content, viewport.getBoundingClientRect()) : null;
    const id = node?.dataset.itemId;
    if (!node || !id) return;
    const rank = Number(node.dataset.displayRank ?? 0);
    readingRef.current = { node, top: node.getBoundingClientRect().top, id, rank, identity };
    setVisiblePin((previous) => previous?.id === id && previous.rank === rank && previous.identity === identity
      ? previous : { id, rank, identity });
  }, [contentRef, identity, isFollowing, viewportRef]);
  useLayoutEffect(() => { viewRef.current = view; }, [view]);
  // Layout cleanup runs before React replaces rows. It covers window activation
  // and a Details-mode change even when neither fires a scroll event.
  useLayoutEffect(() => () => {
    if (!tracking || isFollowing() || preserveRef.current) return;
    preserveRef.current = readingRef.current;
    if (!preserveRef.current) {
      rememberVisible();
      preserveRef.current = readingRef.current;
    }
  });
  useEffect(() => {
    if (previousIdentityRef.current === identity) return;
    previousIdentityRef.current = identity;
    setHeights(new Map());
  }, [identity]);

  // Live mirrors for the subscriptions below. The scroll listener and both
  // ResizeObservers are installed once per tracking session and read the
  // latest groups / heights / enabled here; keying them on those values
  // re-subscribed (and re-observed every row) on each streamed commit.
  const groupsRef = useRef(groups);
  const heightsRef = useRef(heights);
  const enabledRef = useRef(enabled);
  useLayoutEffect(() => {
    groupsRef.current = groups;
    heightsRef.current = heights;
    enabledRef.current = enabled;
  });

  const measureView = useCallback(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const rect = viewport.getBoundingClientRect();
    const content = contentRef.current;
    if (content && enabledRef.current) keepFocusInTimeline(content, rect);
    const groups = groupsRef.current;
    const heights = heightsRef.current;
    const first = content?.querySelector<HTMLElement>("[data-virtual-group]");
    const firstIndex = first ? groups.findIndex((group) => group.key === first.dataset.virtualGroup) : -1;
    let origin = viewRef.current.origin;
    if (first && firstIndex >= 0 && rect.height > 0 && first.getBoundingClientRect().height > 0) {
      origin = first.getBoundingClientRect().top - rect.top + viewport.scrollTop;
      for (let index = 0; index < firstIndex; index++) {
        origin -= (heights.get(groups[index].key) ?? groups[index].estimatedHeight) + GAP;
      }
    }
    const next = { top: viewport.scrollTop, height: viewport.clientHeight, origin };
    const previous = viewRef.current;
    const changed = Math.abs(previous.top - next.top) >= 1 || previous.height !== next.height ||
      Math.abs(previous.origin - next.origin) >= 1;
    if (changed) {
      rememberVisible();
      preserveRef.current = readingRef.current;
      viewRef.current = next;
      setView(next);
    }
  }, [contentRef, rememberVisible, viewportRef]);

  // A compact handoff, send pin or restore moves scrollTop in an earlier
  // layout effect. Sampling every commit puts that position into the same
  // pre-paint render that clears its pin. The read is cheap on a streamed
  // commit: TimelineView's stuck-header sync has already flushed layout, and
  // measureView only renders again when the view actually moved.
  useLayoutEffect(() => {
    if (tracking) measureView();
  });

  useEffect(() => {
    if (!tracking) return;
    const viewport = viewportRef.current;
    if (!viewport) return;
    measureView();
    viewport.addEventListener("scroll", measureView, { passive: true });
    const observer = new ResizeObserver(measureView);
    observer.observe(viewport);
    return () => {
      viewport.removeEventListener("scroll", measureView);
      observer.disconnect();
    };
  }, [tracking, measureView, viewportRef]);

  const sizes = groups.map((group) => heights.get(group.key) ?? group.estimatedHeight);
  const pin = pendingAnchor ?? (visiblePin?.identity === identity ? visiblePin : null);
  const pinnedIndex = pin
    ? groups.findIndex((group) => group.rank === pin.rank && group.itemIds.includes(pin.id)) : -1;
  const range = (() => {
    if (!enabled) return { start: 0, end: groups.length, before: 0, after: 0 };
    if (view.height <= 0) {
      if (pinnedIndex >= 0) {
        const start = Math.max(0, pinnedIndex - BOOTSTRAP_PIN_LEAD_ROWS);
        const end = Math.min(groups.length, start + BOOTSTRAP_WINDOW_ROWS);
        return { start, end, before: span(sizes, 0, start), after: span(sizes, end, sizes.length) };
      }
      if (restoreTop !== null) return windowForSizes(sizes, restoreTop, UNMEASURED_VIEWPORT_HEIGHT_PX, view.origin);
      const start = Math.max(0, groups.length - BOOTSTRAP_WINDOW_ROWS);
      return { start, end: groups.length, before: span(sizes, 0, start), after: 0 };
    }
    let top = view.top;
    if (pinnedIndex >= 0) {
      const pinTop = view.origin + span(sizes, 0, pinnedIndex) + (pinnedIndex ? GAP : 0);
      const pinBottom = pinTop + sizes[pinnedIndex];
      if (pinBottom < top - view.height || pinTop > top + view.height * 2) top = pinTop;
    }
    return windowForSizes(sizes, top, view.height, view.origin);
  })();

  const rowRange = (groupIndex: number): WindowRange => {
    const group = groups[groupIndex];
    const rows = group.expandedRows;
    if (!enabled || !rows || rows.length <= turnRows) {
      return { start: 0, end: rows?.length ?? 0, before: 0, after: 0 };
    }
    const childSizes = rows.map((key) => heights.get(key) ?? 80);
    if (view.height <= 0) {
      const childPin = groupIndex === pinnedIndex && pin
        ? rows.findIndex((key) => key.endsWith(`:${pin.id}`)) : -1;
      if (childPin >= 0) {
        const start = Math.max(0, childPin - BOOTSTRAP_PIN_LEAD_ROWS);
        const end = Math.min(rows.length, start + BOOTSTRAP_WINDOW_ROWS);
        return { start, end, before: span(childSizes, 0, start), after: span(childSizes, end, rows.length) };
      }
      if (restoreTop !== null) return windowForSizes(childSizes, restoreTop, UNMEASURED_VIEWPORT_HEIGHT_PX, view.origin + 60);
      const start = Math.max(0, rows.length - BOOTSTRAP_WINDOW_ROWS);
      return { start, end: rows.length, before: span(childSizes, 0, start), after: 0 };
    }
    const groupTop = view.origin + span(sizes, 0, groupIndex) + (groupIndex ? GAP : 0);
    const childOrigin = groupTop + 60;
    let top = view.top;
    const childPin = groupIndex === pinnedIndex && pin
      ? rows.findIndex((key) => key.endsWith(`:${pin.id}`)) : -1;
    if (childPin >= 0) {
      const pinTop = childOrigin + span(childSizes, 0, childPin) + (childPin ? GAP : 0);
      const pinBottom = pinTop + childSizes[childPin];
      if (pinBottom < top - view.height || pinTop > top + view.height * 2) top = pinTop;
    }
    return windowForSizes(childSizes, top, view.height, childOrigin);
  };

  useLayoutEffect(() => {
    const preserved = preserveRef.current;
    preserveRef.current = null;
    const viewport = viewportRef.current;
    if (!isFollowing() && preserved?.identity === identity && viewport) {
      const node = preserved.node.isConnected ? preserved.node :
        contentRef.current?.querySelector<HTMLElement>(
          `.timeline-item[data-item-id="${CSS.escape(preserved.id)}"][data-display-rank="${preserved.rank}"], [data-turn-expanded="false"][data-item-id="${CSS.escape(preserved.id)}"][data-display-rank="${preserved.rank}"]`,
        );
      if (node) viewport.scrollTop += node.getBoundingClientRect().top - preserved.top;
    }
    rememberVisible();
  });

  // One ResizeObserver per tracking session records each mounted group's and
  // expanded row's height. Each commit only observes newly mounted nodes and
  // releases unmounted ones; re-creating the observer per commit re-observed
  // every row, and each observe() queues a fresh notification.
  const rowObserverRef = useRef<{ observer: ResizeObserver; nodes: Set<HTMLElement> } | null>(null);
  useLayoutEffect(() => {
    if (!tracking) return;
    const content = contentRef.current;
    if (!content) return;
    const observer = new ResizeObserver((entries) => {
      let changed = false;
      const nextHeights = new Map(heightsRef.current);
      for (const entry of entries) {
        const node = entry.target as HTMLElement;
        const key = node.dataset.virtualGroup ?? node.dataset.virtualRow;
        if (!key) continue;
        const height = entry.borderBoxSize[0]?.blockSize ?? node.getBoundingClientRect().height;
        if (height <= 0 || Math.abs((nextHeights.get(key) ?? 0) - height) < 1) continue;
        nextHeights.set(key, height);
        changed = true;
      }
      if (!changed) return;
      const rect = viewportRef.current?.getBoundingClientRect();
      if (rect && enabledRef.current) keepFocusInTimeline(content, rect);
      // ResizeObserver runs after layout. Keep the reader observed before the
      // height change; recapturing here would select the newly grown row.
      preserveRef.current = readingRef.current;
      const groups = groupsRef.current;
      if (nextHeights.size > Math.max(HEIGHT_CACHE_SWEEP_FLOOR, groups.length * 2)) {
        const live = new Set(groups.flatMap((group) => [group.key, ...(group.expandedRows ?? [])]));
        for (const key of nextHeights.keys()) {
          if (!live.has(key)) nextHeights.delete(key);
        }
      }
      // Two notifications can land before React commits; the second must
      // build on the first rather than on the last committed map.
      heightsRef.current = nextHeights;
      setHeights(nextHeights);
    });
    rowObserverRef.current = { observer, nodes: new Set() };
    return () => {
      observer.disconnect();
      rowObserverRef.current = null;
    };
  }, [contentRef, tracking, viewportRef]);

  useLayoutEffect(() => {
    const rows = rowObserverRef.current;
    const content = contentRef.current;
    if (!rows || !content) return;
    const mounted = new Set(content.querySelectorAll<HTMLElement>("[data-virtual-group], [data-virtual-row]"));
    for (const node of rows.nodes) {
      if (!mounted.has(node)) {
        rows.observer.unobserve(node);
        rows.nodes.delete(node);
      }
    }
    for (const node of mounted) {
      if (!rows.nodes.has(node)) {
        rows.observer.observe(node);
        rows.nodes.add(node);
      }
    }
  });

  return { range, rowRange, measureView };
}
