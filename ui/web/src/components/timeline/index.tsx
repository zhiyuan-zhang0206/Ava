"use client";

import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { ScrollArea } from "@/components/ui/scroll-area";
import { useContentToggle, useContentToggleReset } from "@/lib/layout/content-toggle-store";
import { isReattachedTimelineContext, parseItemIdParts, standingHeadNoteIds } from "@/lib/timeline/timeline";
import {
  POINTER_STICKY_THRESHOLDS,
  TOUCH_STICKY_THRESHOLDS,
  type StickyController,
  createStickyController,
  isAtBottom,
} from "@/lib/layout/sticky";
import type { BackendTimelineItem } from "@/lib/contracts/types";
import { readScrollMemory, type SavedScroll } from "@/lib/layout/scroll-memory";
import { useTimelineStore } from "@/lib/timeline/timeline-store";
import { canonicalBufferCoordinate, type CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import { BAR_HEIGHT_PX, BAR_CLEAR_TOP_PADDING_CLASS, FLEX_1, MIN_H_0, OVERFLOW_HIDDEN } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";
import { useTimelineColors } from "@/lib/timeline/use-timeline-colors";

import { ConnectionNotice } from "@/components/notifications/connection-notice";
import { CompactingBlock } from "./model/compacting-block";
import { LiveClockGate } from "./model/reasoning-clock";
import { findClosestStuckHeaderId, TurnBlock } from "./run-block";
import { rememberTimelinePosition, readTimelineExpansion, rememberTimelineExpansion, watchReadingNavigation } from "./model/reading-position";
import { materializeBeforeAnchor } from "./model/materialize-prefix";
import { settleSavedTimelineScroll } from "./model/restore-settle";
import { classifyItem } from "./model/runs";
import { groupTimelineSegments } from "./model/segments";
import { useCompactTransitionAnchor } from "./model/use-compact-transition-anchor";
import { useOlderHistoryPull } from "./model/use-older-history-pull";
import { applySavedTimelineScroll, useTimelineWindow, useTimelineWindowLimits } from "./model/use-timeline-window";
import { LoadOlderSpinner, LoadOlderKeyboardButton, ColdLoadSpinner, ScrollToBottomButton } from "./overlays";
import { TimelineRow, cardConfigFor } from "./row";

interface Props {
  items: BackendTimelineItem[];
  compactBuffer?: CompactTransitionBuffer | null;
  threadKey?: string;
  scrollMemoryKey?: string;

  streamingCode?: boolean;

  turnActive?: boolean;

  onFork?: (() => void) | null;
  forkPending?: boolean;

  hasMoreOlder?: boolean;
  loadingOlder?: boolean;
  onLoadOlder?: () => void;
  retainedItemsMax?: number;

  loading?: boolean;
  loadingNew?: boolean;
  maxWidthCss?: string;
}

export const TimelineView = memo(function TimelineView({
  items: canonicalItems,
  compactBuffer = null,
  threadKey,
  scrollMemoryKey,
  streamingCode = false,
  turnActive = false,
  onFork,
  forkPending,
  hasMoreOlder = false,
  loadingOlder = false,
  onLoadOlder,
  retainedItemsMax = 250,
  loading = false,
  loadingNew = false,
  maxWidthCss,
}: Props) {
  const t = useTranslations("timeline");
  const { activationRows, turnRows, measureRows } = useTimelineWindowLimits();
  const bufferedItems = useMemo(
    () => compactBuffer?.rows.map((row) => row.item) ?? [],
    [compactBuffer],
  );
  const currentItems = useMemo(
    () => compactBuffer
      ? canonicalItems.filter((item) => (parseItemIdParts(item.item_id)?.rank ?? 0) === 0)
      : canonicalItems,
    [canonicalItems, compactBuffer],
  );
  const items = useMemo(
    () => compactBuffer ? [...bufferedItems, ...currentItems] : canonicalItems,
    [compactBuffer, bufferedItems, currentItems, canonicalItems],
  );

  const endRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const viewportRef = useRef<HTMLElement | null>(null);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const historySpaceRef = useRef<HTMLDivElement>(null);

  const pendingRestoreRef = useRef<SavedScroll | null>(scrollMemoryKey ? readScrollMemory(scrollMemoryKey) : null);

  const holdRestoredPositionRef = useRef(!!pendingRestoreRef.current && !pendingRestoreRef.current.followBottom);
  const settledRestoreRef = useRef<SavedScroll | null>(null);
  const restoreInFlight = useCallback(() => pendingRestoreRef.current !== null || (settledRestoreRef.current !== null && holdRestoredPositionRef.current), []);
  const initialRestorePassRef = useRef(true);

  const restoreRequestRef = useRef<number | null>(null);
  useLayoutEffect(() => () => { initialRestorePassRef.current = true; restoreRequestRef.current = null; }, []);

  const scrollMemoryRef = useRef<{ entryKey: string; contentKey: string } | null>(null);

  const loadOlderRef = useRef<(() => void) | undefined>(onLoadOlder);
  const hasMoreOlderRef = useRef(hasMoreOlder);
  const loadingOlderRef = useRef(loadingOlder);
  const itemsRef = useRef(items);
  useLayoutEffect(() => {
    loadOlderRef.current = onLoadOlder;
    hasMoreOlderRef.current = hasMoreOlder;
    loadingOlderRef.current = loadingOlder;
    itemsRef.current = items;
    scrollMemoryRef.current =
      scrollMemoryKey && threadKey !== undefined
        ? { entryKey: scrollMemoryKey, contentKey: threadKey }
        : null;
  });

  useLayoutEffect(() => () => {
    const mem = scrollMemoryRef.current;
    const viewport = viewportRef.current;
    if (mem && viewport && !restoreInFlight()) rememberTimelinePosition(viewport, mem.entryKey, mem.contentKey);
  }, [restoreInFlight]);

  const pendingAnchorRef = useRef<{ id: string; rank: number; frontId: string | null; docTop: number } | null>(null);

  const prevLoadingOlderRef = useRef(loadingOlder);

  const [atBottom, setAtBottom] = useState(true);
  const stuckRafRef = useRef<number | null>(null);

  const [activeStuckHeaderId, setActiveStuckHeaderId] = useState<string | null>(null);
  const [activeStuckChildId, setActiveStuckChildId] = useState<string | null>(null);

  const updateStuckHeader = useCallback(() => {
    const viewport =
      viewportRef.current ??
      wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]') ??
      null;
    if (!viewport) return;
    const { topId, childId } = findClosestStuckHeaderId(viewport, BAR_HEIGHT_PX);
    setActiveStuckHeaderId((prev) => (prev === topId ? prev : topId));
    setActiveStuckChildId((prev) => (prev === childId ? prev : childId));
  }, []);

  const stickyThresholds = useMemo(
    () =>
      typeof window !== "undefined" && window.matchMedia("(pointer: coarse)").matches
        ? TOUCH_STICKY_THRESHOLDS
        : POINTER_STICKY_THRESHOLDS,
    [],
  );

  const [controller] = useState<StickyController>(() =>
    createStickyController(stickyThresholds),
  );
  const isFollowing = useCallback(() => !holdRestoredPositionRef.current && controller.isSticky(), [controller]);
  const canonicalItemsRef = useRef(canonicalItems);
  const retainedItemsMaxRef = useRef(retainedItemsMax);

  const trimFollowing = useCallback(() => {
    if (!controller.isSticky() || holdRestoredPositionRef.current) return;
    const store = useTimelineStore.getState();
    if (store.items !== canonicalItemsRef.current) return;
    if (store.compactBuffer || store.items.length <= retainedItemsMaxRef.current) return;
    store.trimOldestWhileFollowing(retainedItemsMaxRef.current);
  }, [controller]);
  useLayoutEffect(() => {
    canonicalItemsRef.current = canonicalItems;
    retainedItemsMaxRef.current = retainedItemsMax;
    trimFollowing();
  }, [canonicalItems, retainedItemsMax, trimFollowing]);
  const compactPin = useCompactTransitionAnchor({
    controller,
    viewportRef,
    prependAnchorRef: pendingAnchorRef,
    compactBuffer,
  });

  const [announcedTurnActive, setAnnouncedTurnActive] = useState(turnActive);
  const [turnAnnouncement, setTurnAnnouncement] = useState<string | null>(null);
  if (announcedTurnActive !== turnActive) {
    setAnnouncedTurnActive(turnActive);
    setTurnAnnouncement(turnActive ? t("agentResponding") : t("agentResponseComplete"));
  }

  const measureAtBottom = useCallback(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    if (holdRestoredPositionRef.current) {
      setAtBottom(viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight <= 1);
      return;
    }
    setAtBottom(
      isAtBottom(
        {
          scrollTop: viewport.scrollTop,
          scrollHeight: viewport.scrollHeight,
          clientHeight: viewport.clientHeight,
        },
        stickyThresholds,
      ),
    );
  }, [stickyThresholds]);

  const pinToBottom = useCallback(
    (viewport: HTMLElement, freshRun = false) => {
      viewport.scrollTop = viewport.scrollHeight;
      controller.notifyPinnedToBottom(
        {
          scrollTop: viewport.scrollTop,
          scrollHeight: viewport.scrollHeight,
          clientHeight: viewport.clientHeight,
        },
        freshRun,
      );
    },
    [controller],
  );

  const scrollByDelta = useCallback((viewport: HTMLElement, delta: number) => {
    viewport.scrollTop += delta;
    prependEchoRef.current = true;
  }, []);

  const captureAnchor = useCallback(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const currentItems = itemsRef.current;
    const headNoteIds = standingHeadNoteIds(currentItems);
    const contextIds = new Set(
      currentItems
        .filter((it) => isReattachedTimelineContext(it) || headNoteIds.has(it.item_id))
        .map((it) => it.item_id),
    );
    const vpRect = viewport.getBoundingClientRect();
    let anchorNode: HTMLElement | null = null;
    let contextNode: HTMLElement | null = null;
    for (const n of viewport.querySelectorAll<HTMLElement>(
      ".timeline-item, [data-turn-expanded='false'][data-item-id]",
    )) {
      const id = n.dataset.itemId;
      if (!id || id.startsWith("_")) continue;
      if (contextIds.has(id)) {
        contextNode ??= n;
        continue;
      }
      const r = n.getBoundingClientRect();
      if (r.bottom >= vpRect.top && r.top <= vpRect.bottom) {
        anchorNode = n;
        break;
      }
    }
    anchorNode ??= contextNode;
    const anchorId = anchorNode?.dataset.itemId;
    if (anchorNode && anchorId) {
      const frontRealId =
        currentItems.find(
          (it) =>
            !isReattachedTimelineContext(it) &&
            !headNoteIds.has(it.item_id) &&
            !it.item_id.startsWith("_"),
        )?.item_id ?? null;
      pendingAnchorRef.current = {
        id: anchorId,
        rank: Number(anchorNode.dataset.displayRank ?? 0),
        frontId: frontRealId,
        docTop: anchorNode.getBoundingClientRect().top + viewport.scrollTop,
      };
    }
  }, []);

  const coldFillRef = useRef({ pages: 0, open: true, inFlight: false, sawLoading: false });

  const prependEchoRef = useRef(false);
  useLayoutEffect(() => {
    coldFillRef.current = { pages: 0, open: true, inFlight: false, sawLoading: false };
    prependEchoRef.current = false;
  }, [threadKey]);
  useEffect(() => {
    const fill = coldFillRef.current;
    if (loadingOlder) fill.sawLoading = true;
    else if (fill.sawLoading) {
      fill.inFlight = false;
      fill.sawLoading = false;
    }
  }, [loadingOlder]);

  const loadOneOlderPage = useCallback(() => {
    const viewport = viewportRef.current;
    if (!viewport || !hasMoreOlderRef.current || loadingOlderRef.current) return false;
    if (!loadOlderRef.current || pendingAnchorRef.current !== null) return false;
    captureAnchor();
    loadOlderRef.current();
    return true;
  }, [captureAnchor]);

  const canPullOlder = useCallback(() =>
    hasMoreOlderRef.current && !loadingOlderRef.current && !!loadOlderRef.current && pendingAnchorRef.current === null,
  []);
  const closeColdFill = useCallback(() => { coldFillRef.current.open = false; }, []);
  const {
    prepare: prepareHistoryPull, contentHeight: historyContentHeight, offset: olderPullOffset, progress: olderPullProgress, returning: olderPullReturning,
    isActive: historyPullActive, finishLoad: finishHistoryPull, onScroll: pullScroll, onScrollEnd: pullScrollEnd,
    onWheel: pullWheel, onTouchStart: pullTouchStart, onTouchMove: pullTouchMove,
    onTouchEnd: pullTouchEnd, onTouchCancel: cancelPullTouch,
  } = useOlderHistoryPull(threadKey, historySpaceRef, contentRef, canPullOlder, loadOneOlderPage, closeColdFill);

  const maybeColdFill = useCallback(() => {
    const fill = coldFillRef.current;
    const viewport = viewportRef.current;
    if (!fill.open || !viewport || viewport.clientHeight <= 0 || itemsRef.current.length === 0) return;
    if (historyContentHeight(viewport) > viewport.clientHeight || fill.pages >= 2) {
      fill.open = false;
      return;
    }
    if (fill.inFlight) return;
    if (loadOneOlderPage()) {
      fill.inFlight = true;
      fill.pages += 1;
    }
  }, [loadOneOlderPage, historyContentHeight]);

  useLayoutEffect(() => {
    if (loadingOlder && !prevLoadingOlderRef.current && pendingAnchorRef.current === null) {
      captureAnchor();
    }
    prevLoadingOlderRef.current = loadingOlder;
  }, [loadingOlder, captureAnchor]);

  const applyPendingRestore = useCallback(
    (viewport: HTMLElement | null) => {
      const saved = pendingRestoreRef.current;
      if (initialRestorePassRef.current || !saved || !viewport) return;
      if (threadKey === undefined) return;
      if (saved.contentKey !== threadKey) {
        pendingRestoreRef.current = null;
        holdRestoredPositionRef.current = false;
        return;
      }
      if (!applySavedTimelineScroll(viewport, saved, compactBuffer, canonicalItemsRef.current)) {
        restoreRequestRef.current ??= useTimelineStore.getState().scrollToBottomRequest;
        controller.notifyRestored({ scrollTop: viewport.scrollTop, scrollHeight: viewport.scrollHeight, clientHeight: viewport.clientHeight });
        return;
      }
      controller.notifyRestored({ scrollTop: viewport.scrollTop, scrollHeight: viewport.scrollHeight, clientHeight: viewport.clientHeight });
      pendingRestoreRef.current = null;
      settledRestoreRef.current = saved.anchor ? saved : null;
    },
    [compactBuffer, controller, threadKey],
  );

  const settleRestoredPosition = useCallback((viewport: HTMLElement | null, layoutChanged: boolean) => {
    const saved = settledRestoreRef.current;
    if (!saved || !viewport) return;
    if (pendingRestoreRef.current !== null || !holdRestoredPositionRef.current || threadKey === undefined || saved.contentKey !== threadKey) { settledRestoreRef.current = null; return; }
    if (layoutChanged && settleSavedTimelineScroll(viewport, saved, compactBuffer, canonicalItemsRef.current)) settledRestoreRef.current = null;
  }, [compactBuffer, threadKey]);

  useEffect(() => {
    const viewport = wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]') ?? null;
    if (!viewport) return;
    viewportRef.current = viewport;
    const snapshot = () => ({
      scrollTop: viewport.scrollTop,
      scrollHeight: viewport.scrollHeight,
      clientHeight: viewport.clientHeight,
    });

    const onScroll = () => {
      const boundaryCorrection = pullScroll(viewport);
      if (historyPullActive()) { updateStuckHeader(); return; }
      const direction = holdRestoredPositionRef.current && !pendingRestoreRef.current ? "none" : controller.handleScroll(snapshot());
      if (direction === "up") coldFillRef.current.open = false;

      const isPrependEcho = prependEchoRef.current || boundaryCorrection;
      prependEchoRef.current = false;

      if (direction !== "none" && !isPrependEcho) {
        if (pendingRestoreRef.current) holdRestoredPositionRef.current = true;
        pendingRestoreRef.current = null;
      }
      measureAtBottom();
      trimFollowing();
      const mem = scrollMemoryRef.current;
      if (mem && !restoreInFlight()) {
        rememberTimelinePosition(viewport, mem.entryKey, mem.contentKey);
      }
      stuckRafRef.current ??= requestAnimationFrame(() => {
        stuckRafRef.current = null;
        updateStuckHeader();
      });
    };
    viewport.addEventListener("scroll", onScroll, { passive: true });
    const onScrollEnd = (event: Event) => { if (event.target === viewport) pullScrollEnd(); };
    viewport.addEventListener("scrollend", onScrollEnd);
    const stopWatchingReadingInput = watchReadingNavigation(viewport, wrapperRef.current, () => { holdRestoredPositionRef.current = false; });

    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey && e.deltaY !== 0 && Math.abs(e.deltaY) >= Math.abs(e.deltaX)) holdRestoredPositionRef.current = false;
      if (!e.ctrlKey && Math.abs(e.deltaY) >= Math.abs(e.deltaX)) controller.handleWheel(e.deltaY, snapshot());
      if (e.deltaY !== 0 && !controller.isSticky() && !isAtBottom(snapshot(), stickyThresholds)) pendingRestoreRef.current = null;
      pullWheel(e, viewport);
    };
    viewport.addEventListener("wheel", onWheel, { passive: false });

    const onTouchStart = (e: TouchEvent) => {
      holdRestoredPositionRef.current = false;
      controller.handleTouchStart();
      pullTouchStart(e, viewport);
    };
    const onTouchMove = (e: TouchEvent) => { pullTouchMove(e, viewport); };

    const onTouchEnd = () => {
      controller.handleTouchEnd(snapshot());
      pullTouchEnd();
    };
    const onTouchCancel = () => {
      controller.handleTouchEnd(snapshot());
      cancelPullTouch();
    };

    viewport.addEventListener("touchstart", onTouchStart, { passive: true });
    viewport.addEventListener("touchmove", onTouchMove, { passive: false });
    viewport.addEventListener("touchend", onTouchEnd, { passive: true });
    viewport.addEventListener("touchcancel", onTouchCancel, { passive: true });

    const ro = new ResizeObserver(() => {

      prepareHistoryPull(viewport);
      applyPendingRestore(viewport);
      settleRestoredPosition(viewport, true);
      if (!historyPullActive() && !holdRestoredPositionRef.current && controller.handleLayoutChange(snapshot())) pinToBottom(viewport);
      measureAtBottom();
      maybeColdFill();
      trimFollowing();
    });
    ro.observe(viewport);
    if (contentRef.current) ro.observe(contentRef.current);

    return () => {
      viewport.removeEventListener("scroll", onScroll);
      viewport.removeEventListener("scrollend", onScrollEnd);
      stopWatchingReadingInput();
      viewport.removeEventListener("wheel", onWheel);
      viewport.removeEventListener("touchstart", onTouchStart);
      viewport.removeEventListener("touchmove", onTouchMove);
      viewport.removeEventListener("touchend", onTouchEnd);
      viewport.removeEventListener("touchcancel", onTouchCancel);
      ro.disconnect();
    };
  }, [applyPendingRestore, settleRestoredPosition, restoreInFlight, controller, stickyThresholds, measureAtBottom, maybeColdFill, pinToBottom, trimFollowing, updateStuckHeader, prepareHistoryPull, historyPullActive, pullScroll, pullScrollEnd, pullWheel, pullTouchStart, pullTouchMove, pullTouchEnd, cancelPullTouch]);

  const scrollToBottomRequest = useTimelineStore((s) => s.scrollToBottomRequest);
  const lastForceRequest = useRef<{ request: number; thread: string | undefined } | null>(null);
  useLayoutEffect(() => {
    if (lastForceRequest.current?.request === scrollToBottomRequest && lastForceRequest.current.thread === threadKey) return;
    lastForceRequest.current = { request: scrollToBottomRequest, thread: threadKey };
    pendingAnchorRef.current = null;
    if (pendingRestoreRef.current?.contentKey !== threadKey) pendingRestoreRef.current = null;

    if (pendingRestoreRef.current) {
      if (restoreRequestRef.current === null || restoreRequestRef.current === scrollToBottomRequest) return;
      pendingRestoreRef.current = null;
    }
    holdRestoredPositionRef.current = false;

    setAtBottom(true);
    const viewport =
      viewportRef.current ??
      wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]') ??
      null;
    if (!viewport) return;

    pinToBottom(viewport, true);
  }, [scrollToBottomRequest, pinToBottom, threadKey]);

  useLayoutEffect(() => {
    const viewport = viewportRef.current ??
      wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]') ?? null;
    initialRestorePassRef.current = false;
    applyPendingRestore(viewport);
    settleRestoredPosition(viewport, false);
  });

  useLayoutEffect(() => {
    const anchor = pendingAnchorRef.current;
    if (!anchor) return;
    const viewport =
      viewportRef.current ??
      wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]') ??
      null;
    if (!viewport) return;
    const escapedId = CSS.escape(anchor.id);
    const findNode = () =>
      viewport.querySelector<HTMLElement>(`.timeline-item[data-item-id="${escapedId}"][data-display-rank="${anchor.rank}"]`) ??
      viewport.querySelector<HTMLElement>(`[data-turn-expanded="false"][data-item-id="${escapedId}"][data-display-rank="${anchor.rank}"]`) ??
      viewport.querySelector<HTMLElement>(`[data-turn-member-ids~="${escapedId}"][data-display-rank="${anchor.rank}"]`);

    const headNoteIds = standingHeadNoteIds(items);
    const frontRealId = items.find(
      (it) =>
        !isReattachedTimelineContext(it) &&
        !headNoteIds.has(it.item_id) &&
        !it.item_id.startsWith("_"),
    )?.item_id ?? null;
    if (frontRealId === anchor.frontId) {

      const node = findNode();
      if (!node) {
        pendingAnchorRef.current = null;
        return;
      }
      anchor.docTop = node.getBoundingClientRect().top + viewport.scrollTop;
      return;
    }

    const anchorNode = findNode();
    if (!anchorNode) {
      pendingAnchorRef.current = null;
      return;
    }
    const cvSaved = materializeBeforeAnchor(viewport, anchorNode);
    void viewport.getBoundingClientRect();
    const node = findNode();
    if (!node) {
      for (const { el, cv } of cvSaved) el.style.contentVisibility = cv;
      pendingAnchorRef.current = null;
      return;
    }
    const delta = node.getBoundingClientRect().top + viewport.scrollTop - anchor.docTop;

    if (delta <= 0) {
      for (const { el, cv } of cvSaved) el.style.contentVisibility = cv;
      pendingAnchorRef.current = null;
      return;
    }
    scrollByDelta(viewport, delta);
    pendingAnchorRef.current = null;
  }, [items, scrollByDelta]);

  const prevLoadingOlderReleaseRef = useRef(loadingOlder);
  useLayoutEffect(() => {
    if (prevLoadingOlderReleaseRef.current && !loadingOlder) {
      pendingAnchorRef.current = null;
      const wasPulling = historyPullActive();
      finishHistoryPull();
      const viewport = viewportRef.current;
      if (wasPulling && viewport) controller.notifyRestored({
        scrollTop: viewport.scrollTop, scrollHeight: viewport.scrollHeight, clientHeight: viewport.clientHeight,
      });
    }
    prevLoadingOlderReleaseRef.current = loadingOlder;
  }, [loadingOlder, finishHistoryPull, historyPullActive, controller]);

  const { detailsMode, isLoading: detailsModeLoading } = useContentToggle();
  const effectiveDetailsMode = detailsModeLoading ? "none" : detailsMode;

  const timelineColors = useTimelineColors();

  const [turnOverrides, setTurnOverrides] = useState<ReadonlyMap<string, boolean>>(
    () => readTimelineExpansion(scrollMemoryKey, effectiveDetailsMode)?.turns ?? new Map(),
  );
  const toggleTurn = useCallback((id: string, currentlyExpanded: boolean) => {
    setTurnOverrides((prev) => {
      const next = new Map(prev);
      next.set(id, !currentlyExpanded);
      return next;
    });
  }, []);

  const [overrides, setOverrides] = useState<ReadonlySet<string>>(
    () => readTimelineExpansion(scrollMemoryKey, effectiveDetailsMode)?.items ?? new Set(),
  );
  const toggleExpanded = useCallback(
    (id: string, _kind: BackendTimelineItem["kind"]) => {
      setOverrides((prev) => {
        const next = new Set(prev);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        return next;
      });
    },
    [],
  );

  const [prevDetailsMode, setPrevDetailsMode] = useState(effectiveDetailsMode);
  if (prevDetailsMode !== effectiveDetailsMode) {
    setPrevDetailsMode(effectiveDetailsMode);
    setOverrides(readTimelineExpansion(scrollMemoryKey, effectiveDetailsMode)?.items ?? new Set());
    setTurnOverrides(readTimelineExpansion(scrollMemoryKey, effectiveDetailsMode)?.turns ?? new Map());
    setActiveStuckHeaderId(null);
    setActiveStuckChildId(null);
  }

  const [prevThreadKey, setPrevThreadKey] = useState(threadKey);
  if (prevThreadKey !== threadKey) {
    setPrevThreadKey(threadKey);
    setOverrides(new Set());
    setTurnOverrides(new Map());
    setActiveStuckHeaderId(null);
    setActiveStuckChildId(null);
  }

  const resetPending = useContentToggleReset((s) => s.resetPending);
  const consumeReset = useContentToggleReset((s) => s.consumeReset);
  useLayoutEffect(() => {
    if (!resetPending) return;
    consumeReset();
    setOverrides(new Set());
    setTurnOverrides(new Map());
    setActiveStuckHeaderId(null);
    setActiveStuckChildId(null);
  }, [resetPending, consumeReset]);

  useLayoutEffect(() => {
    updateStuckHeader();
  }, [updateStuckHeader, turnOverrides, overrides, effectiveDetailsMode, items]);

  useEffect(() => () => {
    if (stuckRafRef.current !== null) cancelAnimationFrame(stuckRafRef.current);
    stuckRafRef.current = null;
  }, []);

  useLayoutEffect(() => {
    if (!detailsModeLoading) rememberTimelineExpansion(scrollMemoryKey, { mode: effectiveDetailsMode, items: overrides, turns: turnOverrides });
  }, [scrollMemoryKey, effectiveDetailsMode, detailsModeLoading, overrides, turnOverrides]);

  const lastAgentChatIdx = items.findLastIndex((item) => item.kind === "agent_chat");

  const renderRow = (
    item: BackendTimelineItem,
    index: number,
    source: "buffer" | "canonical",
    rank: number,

    forceExpand?: boolean,
  ) => {
    const renderKey = source === "buffer"
      ? `buffer:${compactBuffer?.epoch}:${rank}:${item.item_id}`
      : `canonical:${item.item_id}`;
    const config = cardConfigFor(item, timelineColors);
    const streaming =
      streamingCode && index === items.length - 1 && item.kind === "agent_code";

    if (config === null) {
      return (
        <TimelineRow
          key={renderKey}
          timelineSource={source} displayRank={rank}
          item={item}
          config={null}
          streaming={streaming}
          expanded={false}
          showActions={false}
          isStuck={false}
          onToggle={toggleExpanded}
          onFork={null} forkPending={false}
        />
      );
    }

    const isPrimary = classifyItem(item) === "primary";
    const isLastItem = index === items.length - 1;
    const defaultExpanded = isPrimary || forceExpand ? true
      : effectiveDetailsMode === "all" ? true
      : effectiveDetailsMode === "last" ? isLastItem
      : config.fixedDefault;
    const expanded = overrides.has(item.item_id) ? !defaultExpanded : defaultExpanded;

    const showActions = expanded && !item.partial;

    const isForkRow = index === lastAgentChatIdx;
    return (
      <TimelineRow
        key={renderKey}
        timelineSource={source} displayRank={rank}
        item={item}
        config={config}
        streaming={streaming}
        expanded={expanded}
        showActions={showActions}

        stickyHeader={isPrimary ? false : "nested"}
        isStuck={!isPrimary && activeStuckChildId === item.item_id}
        onToggle={toggleExpanded}
        onFork={isForkRow ? onFork ?? null : null}
        forkPending={isForkRow ? forkPending ?? false : false}
      />
    );
  };

  const groups = useMemo(() => {
    if (!compactBuffer) {
      return groupTimelineSegments(canonicalItems).map((entry) => ({ ...entry, source: "canonical" as const }));
    }
    const buffered = groupTimelineSegments(
      bufferedItems,
      (_item, index) => compactBuffer.rows[index].rank,
    ).map((entry) => ({ ...entry, source: "buffer" as const }));
    const current = groupTimelineSegments(currentItems)
      .map((entry) => ({
        ...entry,
        indexOffset: entry.indexOffset + bufferedItems.length,
        source: "canonical" as const,
      }));
    return [...buffered, ...current];
  }, [canonicalItems, compactBuffer, bufferedItems, currentItems]);

  const groupEntries = useMemo(() => groups.map((entry) => {
    const group = entry.group;
    const groupKey = group.kind === "single" ? group.item.item_id : group.items[0].item_id;
    const virtualKey = `${threadKey ?? ""}:${entry.source}:${entry.source === "buffer" ? compactBuffer?.epoch : ""}:${entry.rank}:${groupKey}`;
    const isLastTurn = entry === groups[groups.length - 1];
    const runExpanded = group.kind === "turn" && (turnOverrides.has(groupKey)
      ? (turnOverrides.get(groupKey) ?? false)
      : effectiveDetailsMode === "all"
        ? true
        : effectiveDetailsMode === "last"
          ? isLastTurn && turnActive
          : false);
    const virtualRows = group.kind === "turn" && runExpanded
      ? group.items.map((item) => `${virtualKey}:${item.item_id}`)
      : null;
    return { entry, groupKey, virtualKey, runExpanded, virtualRows };
  }), [groups, compactBuffer?.epoch, turnOverrides, effectiveDetailsMode, turnActive, threadKey]);
  const renderedRows = groupEntries.reduce((total, entry) => total + (entry.virtualRows?.length ?? 1), 0);
  const virtualEnabled = renderedRows > activationRows;
  const virtualGroups = useMemo(() => groupEntries.map(({ entry, virtualKey, virtualRows }) => ({
      key: virtualKey,
      rank: entry.rank,
      itemIds: entry.group.kind === "single"
        ? [entry.group.item.item_id]
        : entry.group.items.map((item) => item.item_id),
      estimatedHeight: (entry.group.kind === "turn" && virtualRows
        ? 60 + virtualRows.length * 92
        : entry.group.kind === "turn" ? 56 : 80),
      expandedRows: virtualRows,
    })), [groupEntries]);
  const compactPinnedId = compactPin && (() => {
    const bufferedCoordinates = new Map((compactBuffer?.rows ?? []).map((row) => [
      `${row.rank}:${row.item.item_id}`, canonicalBufferCoordinate(row),
    ]));
    for (const { entry } of groupEntries) {
      for (const item of entry.group.kind === "single" ? [entry.group.item] : entry.group.items) {
        const coordinate = entry.source === "buffer"
          ? bufferedCoordinates.get(`${entry.rank}:${item.item_id}`)
          : parseItemIdParts(item.item_id);
        if (entry.rank === compactPin.rank && coordinate?.msg === compactPin.msg && coordinate.block === compactPin.block) return { id: item.item_id, rank: entry.rank };
      }
    }
    return null;
  })();
  const { range: virtualRange, rowRange } = useTimelineWindow({
    groups: virtualGroups,
    enabled: virtualEnabled,
    turnRows, measureRows, viewportRef, contentRef,
    isFollowing,
    identity: threadKey ?? null,
    pendingAnchor: pendingAnchorRef.current ?? compactPinnedId ??
      (!pendingRestoreRef.current?.followBottom && pendingRestoreRef.current?.anchor
        ? { id: pendingRestoreRef.current.anchor.itemId, rank: pendingRestoreRef.current.anchor.rank }
        : null),
    restoreTop: pendingRestoreRef.current?.scrollTop ?? null,
  });

  const handleScrollToBottom = useCallback(() => {
    const viewport =
      viewportRef.current ??
      wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]') ??
      null;
    if (!viewport) return;

    pendingAnchorRef.current = null; pendingRestoreRef.current = null;
    holdRestoredPositionRef.current = false;
    controller.requestStick();
    setAtBottom(true);
    viewport.scrollTo({ top: viewport.scrollHeight, behavior: "smooth" });
  }, [controller]);

  return (
    <div ref={wrapperRef} className={cn("relative", FLEX_1, MIN_H_0, OVERFLOW_HIDDEN)}>
      <LoadOlderSpinner loadingOlder={loadingOlder} pullProgress={olderPullProgress} pullOffset={olderPullOffset} returning={olderPullReturning} />
      {hasMoreOlder && onLoadOlder ? <LoadOlderKeyboardButton loading={loadingOlder} onClick={loadOneOlderPage} /> : null}
      <ColdLoadSpinner show={loading && items.length === 0} />
      <ScrollArea
        className="h-full text-[13px] leading-relaxed"
        viewportClassName="[overflow-anchor:none] overscroll-y-contain"
      >
        <div ref={historySpaceRef} data-history-pull-space aria-hidden="true" />
        <div
          ref={contentRef}
          role="log"
          data-timeline-item-count={items.length}
          aria-live="polite"
          aria-relevant="additions"
          style={maxWidthCss ? { maxWidth: maxWidthCss } : undefined}
          className={cn("mx-auto w-full px-2 pb-3 space-y-3 sm:px-4", BAR_CLEAR_TOP_PADDING_CLASS, virtualEnabled && "timeline-virtual")}
        >
          {turnAnnouncement ? (
            <span key={turnAnnouncement} className="sr-only" data-testid="timeline-turn-announcement">
              {turnAnnouncement}
            </span>
          ) : null}
          <ConnectionNotice />
          {virtualEnabled && virtualRange.before > 0 ? (
            <div data-timeline-spacer="before" style={{ height: virtualRange.before }} aria-hidden="true" />
          ) : null}
          <LiveClockGate value={turnActive}>
            {groupEntries.slice(virtualRange.start, virtualRange.end).map(({ entry, virtualKey, runExpanded, virtualRows }, offset) => {
              const group = entry.group;
              const groupIndex = virtualRange.start + offset;
              const renderedGroup = (() => {
                if (group.kind === "single") {
                  return renderRow(group.item, entry.indexOffset + group.index, entry.source, entry.rank);
                }

                const turnId = group.items[0].item_id;
                const isLastTurn = entry === groups[groups.length - 1];
                const childRange = rowRange(groupIndex);
                return (
                  <TurnBlock
                    id={turnId}
                    memberIds={group.items.map((it) => it.item_id)}
                    summary={group.summary}
                    expanded={runExpanded}
                    onToggle={toggleTurn}
                    turnActive={turnActive && isLastTurn}
                    isStuck={activeStuckHeaderId === turnId}
                    timelineSource={entry.source} displayRank={entry.rank}
                  >
                    {runExpanded ? (
                      virtualRows ? (
                        <>
                          {virtualEnabled && childRange.before > 0 ? <div data-timeline-spacer="turn-before" style={{ height: childRange.before }} aria-hidden="true" /> : null}
                          {group.items.slice(childRange.start, childRange.end).map((it, offset) => {
                            const index = childRange.start + offset;

                            return (
                              <div key={virtualRows[index]} data-virtual-row={virtualRows[index]}>
                                {renderRow(it, entry.indexOffset + group.startIndex + index, entry.source, entry.rank, effectiveDetailsMode !== "none")}
                              </div>
                            );
                          })}
                          {virtualEnabled && childRange.after > 0 ? <div data-timeline-spacer="turn-after" style={{ height: childRange.after }} aria-hidden="true" /> : null}
                        </>
                      ) : null
                    ) : null}
                  </TurnBlock>
                );
              })();
              return (
                <div key={virtualKey} data-virtual-group={virtualKey}>
                  {renderedGroup}
                </div>
              );
            })}
          </LiveClockGate>
          {virtualEnabled && virtualRange.after > 0 ? (
            <div data-timeline-spacer="after" style={{ height: virtualRange.after }} aria-hidden="true" />
          ) : null}
          <CompactingBlock />
          <div ref={endRef} />
        </div>
      </ScrollArea>
      <ScrollToBottomButton atBottom={atBottom} refreshing={loadingNew} onClick={handleScrollToBottom} />
    </div>
  );
});
