"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { CompactTransitionBuffer } from "@/lib/compact-transition";
import type { SavedScroll } from "@/lib/scroll-memory";
import { parseItemIdParts } from "@/lib/timeline";
import type { BackendTimelineItem } from "@/lib/types";

const GAP = 12; // space-y-3 on the timeline and expanded turn body
const BUFFER_VIEWPORTS = 1;

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
  identity: string | null;
  viewportRef: { current: HTMLElement | null };
  contentRef: { current: HTMLElement | null };
  pendingAnchor: { id: string; rank: number } | null;
  restoreTop: number | null;
}

export function useTimelineWindow({
  groups,
  enabled,
  identity,
  viewportRef,
  contentRef,
  pendingAnchor,
  restoreTop,
}: Options) {
  const [heights, setHeights] = useState(() => new Map<string, number>());
  const [visiblePin, setVisiblePin] = useState<{ id: string; rank: number; identity: string | null } | null>(null);
  const previousIdentityRef = useRef(identity);
  const [view, setView] = useState({ top: restoreTop ?? 0, height: 0, origin: 52 });
  const viewRef = useRef(view);
  const readingRef = useRef<{ node: HTMLElement; top: number; id: string; rank: number; identity: string | null } | null>(null);
  const preserveRef = useRef<typeof readingRef.current>(null);
  const tracking = enabled || groups.reduce((count, group) =>
    count + (group.expandedRows?.length ?? 1), 0) >= 75;
  const rememberVisible = useCallback(() => {
    const content = contentRef.current;
    const viewport = viewportRef.current;
    const node = content && viewport ? visibleRow(content, viewport.getBoundingClientRect()) : null;
    const id = node?.dataset.itemId;
    if (!node || !id) return;
    const rank = Number(node.dataset.displayRank ?? 0);
    readingRef.current = { node, top: node.getBoundingClientRect().top, id, rank, identity };
    setVisiblePin((previous) => previous?.id === id && previous.rank === rank && previous.identity === identity
      ? previous : { id, rank, identity });
  }, [contentRef, identity, viewportRef]);
  useLayoutEffect(() => { viewRef.current = view; }, [view]);
  // Layout cleanup runs before React replaces rows. It covers activation at
  // row 101 and a Details-mode change even when neither fires a scroll event.
  useLayoutEffect(() => () => {
    if (!tracking || preserveRef.current) return;
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

  const measureView = useCallback(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const rect = viewport.getBoundingClientRect();
    const content = contentRef.current;
    if (content && enabled) keepFocusInTimeline(content, rect);
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
  }, [contentRef, enabled, groups, heights, rememberVisible, viewportRef]);

  // A compact handoff moves scrollTop in an earlier layout effect. Sampling
  // here puts that position into the same pre-paint render that clears its pin.
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
        const start = Math.max(0, pinnedIndex - 12);
        const end = Math.min(groups.length, start + 24);
        return { start, end, before: span(sizes, 0, start), after: span(sizes, end, sizes.length) };
      }
      if (restoreTop !== null) return windowForSizes(sizes, restoreTop, 680, view.origin);
      const start = Math.max(0, groups.length - 24);
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
    if (!enabled || !rows || rows.length <= 100) {
      return { start: 0, end: rows?.length ?? 0, before: 0, after: 0 };
    }
    const childSizes = rows.map((key) => heights.get(key) ?? 80);
    if (view.height <= 0) {
      const childPin = groupIndex === pinnedIndex && pin
        ? rows.findIndex((key) => key.endsWith(`:${pin.id}`)) : -1;
      if (childPin >= 0) {
        const start = Math.max(0, childPin - 12);
        const end = Math.min(rows.length, start + 24);
        return { start, end, before: span(childSizes, 0, start), after: span(childSizes, end, rows.length) };
      }
      if (restoreTop !== null) return windowForSizes(childSizes, restoreTop, 680, view.origin + 60);
      const start = Math.max(0, rows.length - 24);
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
    if (preserved?.identity === identity && viewport) {
      const node = preserved.node.isConnected ? preserved.node :
        contentRef.current?.querySelector<HTMLElement>(
          `.timeline-item[data-item-id="${CSS.escape(preserved.id)}"][data-display-rank="${preserved.rank}"], [data-turn-expanded="false"][data-item-id="${CSS.escape(preserved.id)}"][data-display-rank="${preserved.rank}"]`,
        );
      if (node) viewport.scrollTop += node.getBoundingClientRect().top - preserved.top;
    }
    rememberVisible();
  });

  useLayoutEffect(() => {
    if (!tracking) return;
    const content = contentRef.current;
    if (!content) return;
    const nodes = content.querySelectorAll<HTMLElement>("[data-virtual-group], [data-virtual-row]");
    const observer = new ResizeObserver((entries) => {
      let changed = false;
      const nextHeights = new Map(heights);
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
      if (rect && enabled) keepFocusInTimeline(content, rect);
      // ResizeObserver runs after layout. Keep the reader observed before the
      // height change; recapturing here would select the newly grown row.
      preserveRef.current = readingRef.current;
      if (nextHeights.size > Math.max(500, groups.length * 2)) {
        const live = new Set(groups.flatMap((group) => [group.key, ...(group.expandedRows ?? [])]));
        for (const key of nextHeights.keys()) {
          if (!live.has(key)) nextHeights.delete(key);
        }
      }
      setHeights(nextHeights);
    });
    nodes.forEach((node) => observer.observe(node));
    return () => observer.disconnect();
  }, [contentRef, enabled, groups, heights, range.start, range.end, readingRef, tracking, viewportRef]);

  return { range, rowRange, measureView };
}
