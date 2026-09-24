"use client";

// Timeline view — renders BackendTimelineItem list (chat / code /
// output / reasoning / system marker), handles sticky-bottom auto-scroll
// and last-message fork action.
//
// **Re-render optimization** (issue 2/3/9): during streaming, chunk
// events arrive at 10-50/s, triggering full-tree diffs of the timeline.
// Several knobs reduce the cost:
// - Each row is a memoized `TimelineRow`. Only the changed item's ref
//   updates per chunk (the store's slice() reducers preserve every other
//   item's identity), so React skips the whole subtree — card container,
//   header summary scan, body — for every unchanged row. Without this the
//   memoized CardHeader was still re-rendered N-per-chunk because its
//   `config` prop (a fresh object literal) and inline `onToggle` closure
//   changed identity every render, defeating its memo.
// - PythonCode / ChatMarkdown are wrapped with React.memo so unchanged
//   content prop skips Prism / remarkGfm re-parse (the main CPU cost).
// - messageCardConfig is memoized per item via `cardConfigFor` (a WeakMap
//   keyed on the stable item ref + the resolved color map), so the config
//   object stays reference-stable for unchanged rows even as the items array
//   is rebuilt each chunk.
// - mergeSnapshotWithStreaming returns the same prev reference when
//   content matches, so Zustand skips setState and the tree skips
//   reconciliation.
//
// Three interaction-logic blocks worth reading separately (code has
// more detail inline):
// 1. **Sticky-bottom scroll controller**: the sticky flag has a single
//    owner, `lib/sticky.ts:createStickyController` (pure, DOM-free, so
//    vitest drives it directly — jsdom does not actually render scroll).
//    This file only feeds DOM scroll/wheel events into it and performs
//    the scrolls it requests: the ResizeObserver pins pre-paint while
//    sticky, force-scroll paths (send / switch / button) request a stick.
//    Key design: the controller identifies our own programmatic scrolls
//    by position echo, so user intent is never inferred from timing.
// 2. **scrollToBottomRequest**: the store's single force-scroll signal,
//    bumped on agent switch (inside switchThread, in the
//    same set() that installs the new thread's items) AND on send
//    (requestScrollToBottom). One useLayoutEffect honors it: pin to bottom
//    + re-stick. Switching chats is a re-initialization and sending always
//    shows the latest, so the view starts at the latest message regardless
//    of prior scroll state. This is the single owner of the force-scroll
//    trigger — it replaced the former parent-owned scrollToken prop + a
//    duplicate activeThreadId effect that double-pinned on switch.
// 3. **Content expand/collapse**: every item renders a MessageCard — a colored
//    left-border container with a CardHeader (icon + title/summary + timestamp +
//    chevron) that toggles the body below it. Default expanded state is per-kind:
//    All blocks follow the global Details mode (useContentToggle
//    — DB-backed user settings — acting as expand-all / collapse-all), the
//    system prompt defaults collapsed, and the conversation / marker kinds default
//    expanded; clicking a header overrides one item. The ephemeral system markers
//    (compact_done / cancelled / error / unrecognized) have no card and render
//    bare. messageCardConfig is the pure per-kind visual mapping; cardConfigFor
//    memoizes it per item ref + resolved color map (WeakMap) so a row's config
//    stays reference-stable across the array rebuilds that streaming triggers.
//
// Copy / fork are hover-revealed actions pinned inside a card's bottom-right
// corner (MessageCard's `actions` overlay) — they no longer occupy their own
// layout row below the card, so a collapsed or unhovered block costs nothing
// extra in height. Copy attaches to every expanded, non-partial card; fork
// additionally attaches only under the last agent_chat card — agent halted +
// finished a thought is a clean fork boundary; forking mid-execution would
// leave the new agent confused about an unfinished task.
//
// Sub-modules:
// - `./card`      — messageCardConfig + MessageCard + CardHeader + the four rich summaries
// - `./item`      — ItemView (memo, body-only) + EnvelopeContent + InterruptedNotice
// - `./markers`   — classifyMarker + markerVisual + MarkerBody + EphemeralSystemMarker
// - `./timestamp` — formatItemTime + ItemTimestamp
// - `./buttons`   — ForkButton + CopyButton
// - `./row`       — TimelineRow (memo) + cardConfigFor (per-item config cache)
// - `./overlays`  — LoadOlderSpinner / ColdLoadSpinner / ScrollToBottomButton
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslations } from "next-intl";

import { ScrollArea } from "@/components/ui/scroll-area";
import { useContentToggle, useContentToggleReset } from "@/lib/content-toggle-store";
import { isReattachedTimelineContext, parseItemIdParts, standingHeadNoteIds } from "@/lib/timeline";
import {
  POINTER_STICKY_THRESHOLDS,
  TOUCH_STICKY_THRESHOLDS,
  type StickyController,
  createStickyController,
  isAtBottom,
} from "@/lib/sticky";
import type { BackendTimelineItem } from "@/lib/types";
import { readScrollMemory, saveScrollMemory, type SavedScroll } from "@/lib/scroll-memory";
import { useTimelineStore } from "@/lib/timeline-store";
import { canonicalBufferCoordinate, type CompactTransitionBuffer } from "@/lib/compact-transition";
import { BAR_HEIGHT_PX, BAR_CLEAR_TOP_PADDING_CLASS, FLEX_1, MIN_H_0, OVERFLOW_HIDDEN } from "@/lib/layout";
import { cn } from "@/lib/utils";
import { useTimelineColors } from "@/lib/use-timeline-colors";

import { ConnectionNotice } from "@/components/connection-notice";
import { CompactingBlock } from "./compacting-block";
import { findClosestStuckHeaderId, TurnBlock } from "./run-block";
import { classifyItem } from "./runs";
import { groupTimelineSegments } from "./segments";
import { useCompactTransitionAnchor } from "./use-compact-transition-anchor";
import { resolveSavedTimelineAnchor, useTimelineWindow, useTimelineWindowLimits } from "./use-timeline-window";
import { CompactHistoryDivider, LoadOlderSpinner, ColdLoadSpinner, ScrollToBottomButton } from "./overlays";
import { TimelineRow, cardConfigFor } from "./row";


interface Props {
  items: BackendTimelineItem[];
  compactBuffer?: CompactTransitionBuffer | null;
  /** Identity of the timeline thread / active agent. A change means a new
   *  conversation is displayed, so all per-item and per-turn pins are dropped. */
  threadKey?: string;
  /** History-entry memory key (router.bfcacheId) for the reader's scroll
   *  position: a back/forward re-entry of a kept history entry restores it
   *  instead of re-pinning to the bottom (see lib/scroll-memory.ts). */
  scrollMemoryKey?: string;
  // Streaming-code flag: pass true when the last item is an agent_code
  // that is still streaming, so PythonCode shows a cursor
  streamingCode?: boolean;
  // Whether the agent is mid-turn. While active, the last (currently-streaming)
  // item is peeled out of turn-collapse so an in-progress step stays visible.
  turnActive?: boolean;
  // Fork the current agent — the button only appears under the last
  // agent_chat item (a clean boundary; agent has halted and finished a
  // thought; mid-execution fork would leave the new agent confused about
  // an unfinished task). null = no active agent / fork unavailable.
  onFork?: (() => void) | null;
  forkPending?: boolean;
  // Scroll-up history loading: a user-driven arrival at the top loads the
  // previous window; a short initial viewport may fill at most two pages.
  hasMoreOlder?: boolean;
  loadingOlder?: boolean;
  onLoadOlder?: () => void;
  retainedItemsMax?: number;
  // Cold-load flag: the snapshot for this thread is being fetched and there is
  // nothing cached yet. Drives a centered spinner instead of a blank pane so a
  // cold switch doesn't flash empty then pop the whole column in at once.
  loading?: boolean;
  /** Conversation-column cap. The scroll surface is full-bleed (user ruling
   *  2026-08-06 — scrollbar at the pane edge, gutters scrollable); the item
   *  column itself stays capped and centered inside it. Falsy = full width. */
  maxWidthCss?: string;
}

export function TimelineView({
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
  // Auto-scroll to bottom — sticky-bottom mode:
  // - default sticks to the bottom; while sticky, any content growth pulls
  //   the viewport down **in the same frame the growth lays out** (the
  //   ResizeObserver below fires after layout, before paint), so new text
  //   never paints below the fold first and then visibly scrolls into
  //   place. Driving the scroll from observed height (not items commits)
  //   also covers growth that happens with no items change at all — the
  //   throttled markdown/highlight flushes, async images — which used to
  //   leave the viewport stranded mid-content until the next chunk.
  // - user scrolls up → unstick; subsequent growth no longer force-pulls
  // - user manually scrolls back into the bottom zone (dist <
  //   bottomZone) then re-stick
  //
  // The sticky flag has a SINGLE owner: lib/sticky.ts:createStickyController.
  // This component only wires DOM events into it and performs the scrolls
  // it requests. No code path here mutates sticky directly — event handlers
  // feed observations in (handleScroll / handleWheel / handleLayoutChange),
  // and force-scroll paths request (requestStick / pinToBottom). Every
  // scroll this component performs is reported back, which is what keeps
  // the controller's baseline truthful and makes the echo a no-op — the
  // job the old design needed wheel-direction refs, prev-snapshot resyncs,
  // grace timers and double sticky assertions (spread across six writers)
  // to approximate.
  const endRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const viewportRef = useRef<HTMLElement | null>(null);
  const wrapperRef = useRef<HTMLDivElement>(null);
  // Back/forward scroll restoration (lib/scroll-memory.ts): the position
  // saved under this history entry, read once at mount and held until the
  // restore effect lands it -- or a user-commanded force-scroll supersedes
  // it. Nothing renders from it, so it lives in a ref.
  const pendingRestoreRef = useRef<SavedScroll | null>(
    scrollMemoryKey ? readScrollMemory(scrollMemoryKey) : null,
  );
  // Memory identity for the scroll handler (registered once with empty deps,
  // so it reads the latest props through a ref).
  const scrollMemoryRef = useRef<{ entryKey: string; contentKey: string } | null>(null);
  // Live mirrors for the scroll handler (registered once with empty deps, so
  // it must read the latest load-older props through refs, not a stale
  // closure). Synced before the observer's pre-paint callback — refs must
  // not be written during render.
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
  // Pending anchor for scroll-position preservation on prepend. Captured at
  // the moment a load-older fetch is triggered (the topmost node the prepend
  // can displace that the user is actually reading — see the capture in the
  // scroll handler), consumed on the commit that prepends the older window:
  // the anchor's DOCUMENT-space top (rect.top + scrollTop, invariant under
  // user scrolls) is refreshed on every commit while pending, and the
  // landing scrolls by the anchor's actual displacement since the last
  // commit — the reading position never moves, no matter how far the user
  // scrolled during the fetch or how many windows land back-to-back.
  // `frontId` is the landing signal: the first item that is not standing
  // context re-attached at the head of every window. The ARRAY front does not
  // change when older items land — only the front real item does.
  const pendingAnchorRef = useRef<{ id: string; rank: number; frontId: string | null; docTop: number } | null>(null);

  const prevLoadingOlderRef = useRef(loadingOlder);
  // Scroll-to-bottom button visibility. Driven by the *measured* current
  // distance to the bottom (sticky.ts:isAtBottom), NOT by the sticky
  // hysteresis flag. The two diverge whenever content height changes with
  // no scroll event — async image/markdown load, content-toggle re-filter,
  // viewport resize (mobile keyboard) — so a flag updated only in onScroll
  // would leave the button hidden while the user is far from the bottom.
  // A ResizeObserver re-measures on those height changes. Initial true =
  // button hidden until the user scrolls away.
  const [atBottom, setAtBottom] = useState(true);
  const stuckRafRef = useRef<number | null>(null);
  // The ids of the pinned headers: one owner for all surfaces — a work block's
  // header or a top-level message card's (level 1, one line under the HeaderBar)
  // and the child card pinned nested under an expanded work block's header
  // (level 2, task #3215).
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

  // Pointer-aware sticky thresholds: touch keeps the wide bounce-tolerant
  // bottom zone; mouse/trackpad gets a tight one so a small scroll-up to
  // read does not snap back. Resolved once on mount (pointer type does not
  // change within a session); SSR has no matchMedia → default to touch.
  // Drives both the sticky controller and the scroll-button atBottom measure.
  const stickyThresholds = useMemo(
    () =>
      typeof window !== "undefined" && window.matchMedia("(pointer: coarse)").matches
        ? TOUCH_STICKY_THRESHOLDS
        : POINTER_STICKY_THRESHOLDS,
    [],
  );

  // The single owner of the sticky flag. Created once via the useState
  // initializer (the sanctioned lazy-init that survives re-renders — the
  // setter is never called, so the identity is stable for the component's
  // lifetime); all event handlers and force-scroll paths below go through
  // it. stickyThresholds is mount-stable, so capturing the first value is
  // correct.
  const [controller] = useState<StickyController>(() =>
    createStickyController(stickyThresholds),
  );
  const canonicalItemsRef = useRef(canonicalItems);
  const retainedItemsMaxRef = useRef(retainedItemsMax);
  // A measured runaway reached 857 rendered items / 44k attached nodes,
  // 278ms frames, and clicks over 5s. A busy tail was 57 items and a normal
  // long session needs a few hundred, so the default is 250 rather than 100
  // or 1000. Only a bottom follower can safely release the oldest rows.
  const trimFollowing = useCallback(() => {
    if (!controller.isSticky()) return;
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

  // Announce only changes to the turn boundary. Streaming chunks change
  // `items`, never `turnActive`, so they do not generate screen-reader noise.
  const [announcedTurnActive, setAnnouncedTurnActive] = useState(turnActive);
  const [turnAnnouncement, setTurnAnnouncement] = useState<string | null>(null);
  if (announcedTurnActive !== turnActive) {
    setAnnouncedTurnActive(turnActive);
    setTurnAnnouncement(turnActive ? t("agentResponding") : t("agentResponseComplete"));
  }

  const measureAtBottom = useCallback(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
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

  // Pin the viewport to the bottom and report the performed scroll back to
  // the controller (post-write snapshot, so the browser-clamped actual
  // scrollTop becomes the controller's baseline). `freshRun` marks the
  // user-commanded force pins (send / agent switch) — see the force-scroll
  // effect below and lib/sticky.ts.
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

  // Shift the viewport by delta (prepend-compensation). Not reported to
  // the controller: prepend always shifts DOWN (the older window pushes
  // the anchor down) and the position rule never unsticks on downward
  // motion, so the echo is inert and re-syncs the baseline on its own.
  // It is NOT inert for the paging budget, which resets on downward
  // classification — mark the echo so onScroll can spare it: a page's own
  // landing must not refill the burst the page came from.
  const scrollByDelta = useCallback((viewport: HTMLElement, delta: number) => {
    viewport.scrollTop += delta;
    prependEchoRef.current = true;
  }, []);

  // Capture the scroll anchor at the trigger moment. The anchor is the
  // TOPMOST node the prepend can displace that the user can actually see —
  // the first VISIBLE real item. Standing-context nodes are skipped because
  // a prepend of older history inserts after them.
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
  const upwardGestureRef = useRef({ lastAt: 0, pages: 0 });
  // A landing's compensation write (scrollByDelta) echoes back as a scroll
  // event the controller classifies "down", though it is no user motion. The
  // scroll handler consumes this mark on the next event so the echo cannot
  // reset the paging budget — with a reset after every landing, the
  // three-page cap could never bind.
  const prependEchoRef = useRef(false);
  useLayoutEffect(() => {
    coldFillRef.current = { pages: 0, open: true, inFlight: false, sawLoading: false };
    upwardGestureRef.current = { lastAt: 0, pages: 0 };
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

  const maybeLoadOlderAtTop = useCallback((userScrolledUp: boolean) => {
    const viewport = viewportRef.current;
    if (!userScrolledUp || !viewport || viewport.scrollTop > 0 || controller.isSticky()) return;
    const gesture = upwardGestureRef.current;
    const now = Date.now();
    if (now - gesture.lastAt > 750) gesture.pages = 0; // a paused burst refills
    gesture.lastAt = now; // a continuously arriving pull stays capped until it pauses
    if (gesture.pages >= 3) return; // one sustained gesture cannot walk unbounded history
    if (loadOneOlderPage()) gesture.pages += 1;
  }, [controller, loadOneOlderPage]);

  const maybeColdFill = useCallback(() => {
    const fill = coldFillRef.current;
    const viewport = viewportRef.current;
    if (!fill.open || !viewport || viewport.clientHeight <= 0 || itemsRef.current.length === 0) return;
    if (viewport.scrollHeight > viewport.clientHeight || fill.pages >= 2) {
      fill.open = false;
      return;
    }
    if (fill.inFlight) return;
    if (loadOneOlderPage()) {
      fill.inFlight = true;
      fill.pages += 1;
    }
  }, [loadOneOlderPage]);

  useLayoutEffect(() => {
    if (loadingOlder && !prevLoadingOlderRef.current && pendingAnchorRef.current === null) {
      captureAnchor();
    }
    prevLoadingOlderRef.current = loadingOlder;
  }, [loadingOlder, captureAnchor]);

  // Back/forward restore: land the saved reading position (see
  // lib/scroll-memory.ts). Retried on every commit and in the ResizeObserver
  // pass — the first commit of a re-entry can precede the cached items'
  // measurable layout, and the position must land in whichever pass first can
  // hold it, always before paint. notifyRestored fixes the sticky flag to the
  // restored spot: a mid-timeline restore stops following, an at-bottom
  // restore keeps following.
  const applyPendingRestore = useCallback(
    (viewport: HTMLElement | null) => {
      const saved = pendingRestoreRef.current;
      if (!saved || !viewport) return;
      if (threadKey === undefined) return; // conversation not identified yet
      if (saved.contentKey !== threadKey) {
        pendingRestoreRef.current = null; // other content now -- position is stale
        return;
      }
      const max = viewport.scrollHeight - viewport.clientHeight;
      if (max <= 0) return; // content not laid out yet -- retried on the next pass
      const anchor = !saved.followBottom ? saved.anchor : undefined;
      const target = anchor ? resolveSavedTimelineAnchor(viewport, anchor, compactBuffer, canonicalItemsRef.current) : null;
      if (target?.present && !target.node) return;
      if (target?.node) {
        viewport.scrollTop += target.node.getBoundingClientRect().top -
          viewport.getBoundingClientRect().top - target.viewportTop;
      } else {
        viewport.scrollTop = Math.min(saved.scrollTop, max);
      }
      controller.notifyRestored({
        scrollTop: viewport.scrollTop,
        scrollHeight: viewport.scrollHeight,
        clientHeight: viewport.clientHeight,
      });
      pendingRestoreRef.current = null;
    },
    [compactBuffer, controller, threadKey],
  );

  useEffect(() => {
    const viewport = wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]') ?? null;
    if (!viewport) return;
    viewportRef.current = viewport;
    const snapshot = () => ({
      scrollTop: viewport.scrollTop,
      scrollHeight: viewport.scrollHeight,
      clientHeight: viewport.clientHeight,
    });
    // Only an upward movement classified by the sticky controller can page.
    // Pin/prepend echoes and layout clamps therefore cannot start a chain.
    const onScroll = () => {
      const direction = controller.handleScroll(snapshot());
      if (direction === "up") coldFillRef.current.open = false;
      // One landing, one echo: consume the mark on this event so only a
      // genuine downward move (not the prepend echo below it) ends the burst.
      const isPrependEcho = prependEchoRef.current;
      prependEchoRef.current = false;
      if (direction === "down" && !isPrependEcho) upwardGestureRef.current.pages = 0;
      measureAtBottom();
      maybeLoadOlderAtTop(direction === "up");
      trimFollowing();
      const mem = scrollMemoryRef.current;
      if (mem) {
        // The reader's position for this history entry; the sticky flag rides
        // along so a follower returns following instead of frozen at a stale
        // offset.
        const rect = viewport.getBoundingClientRect();
        const anchorNode = [...viewport.querySelectorAll<HTMLElement>(
          ".timeline-item[data-item-id], [data-turn-expanded='false'][data-item-id]",
        )].find((node) => {
          const box = node.getBoundingClientRect();
          return box.bottom > rect.top && box.top < rect.bottom;
        });
        saveScrollMemory(mem.entryKey, {
          contentKey: mem.contentKey,
          scrollTop: viewport.scrollTop,
          anchor: anchorNode?.dataset.itemId
            ? { itemId: anchorNode.dataset.itemId, rank: Number(anchorNode.dataset.displayRank ?? 0), viewportTop: anchorNode.getBoundingClientRect().top - rect.top }
            : undefined,
          followBottom: controller.isSticky(),
        });
      }
      stuckRafRef.current ??= requestAnimationFrame(() => {
        stuckRafRef.current = null;
        updateStuckHeader();
      });
    };
    viewport.addEventListener("scroll", onScroll, { passive: true });

    // Wheel intent — an early "stop following" signal for mouse/trackpad
    // (a notch can arrive before the position moves) and the resting-finger
    // absorb at the bottom. It is no longer the only escape a slow
    // scroll-up has: the controller accumulates small upward scroll moves
    // into a run, which is what a scrollbar drag (no wheel at all) depends
    // on. Touch devices never fire wheel.
    const onWheel = (e: WheelEvent) => {
      controller.handleWheel(e.deltaY, snapshot());
    };
    viewport.addEventListener("wheel", onWheel, { passive: true });

    // Touch start/end feed the sticky controller only — the pull-down-to-load
    // gesture retired with the auto-load change (task #4186).
    const onTouchStart = () => {
      controller.handleTouchStart();
    };

    const onTouchEnd = () => {
      controller.handleTouchEnd(snapshot());
    };

    viewport.addEventListener("touchstart", onTouchStart, { passive: true });
    viewport.addEventListener("touchend", onTouchEnd, { passive: true });
    viewport.addEventListener("touchcancel", onTouchEnd, { passive: true });

    // ResizeObserver callbacks run after layout and before paint.
    const ro = new ResizeObserver(() => {
      // The first measurable layout can arrive with no commit of its own
      // (the mount's commit can render a zero-height surface); land a pending
      // restore here, before handleLayoutChange can pin, so the reader never
      // sees the bottom first.
      applyPendingRestore(viewport);
      if (controller.handleLayoutChange(snapshot())) pinToBottom(viewport);
      measureAtBottom();
      maybeColdFill();
      trimFollowing();
    });
    ro.observe(viewport);
    if (contentRef.current) ro.observe(contentRef.current);

    return () => {
      viewport.removeEventListener("scroll", onScroll);
      viewport.removeEventListener("wheel", onWheel);
      viewport.removeEventListener("touchstart", onTouchStart);
      viewport.removeEventListener("touchend", onTouchEnd);
      viewport.removeEventListener("touchcancel", onTouchEnd);
      ro.disconnect();
    };
  }, [applyPendingRestore, controller, measureAtBottom, maybeLoadOlderAtTop, maybeColdFill, pinToBottom, trimFollowing, updateStuckHeader]);

  // The SINGLE force-scroll trigger. The store bumps scrollToBottomRequest on
  // exactly the two moments a scroll-to-bottom is unconditional — agent switch
  // (inside switchThread) and send (requestScrollToBottom) —
  // and this effect honors it: pin to the bottom + re-stick (pinToBottom sets
  // sticky via notifyPinnedToBottom). This replaces the former dual trigger
  // (parent-owned scrollToken effect + a separate activeThreadId effect), which
  // double-pinned on switch and could pin before the new items were in the DOM.
  //
  // The switch bump rides in the SAME store set() that installs the new
  // thread's items, so on a hot-cache switch this layout effect runs with the
  // full new thread already committed and pins to its true bottom. On a
  // cold-cache switch it pins the empty viewport now; the items arrive via
  // React Query and the latched sticky flag + ResizeObserver pull the viewport
  // down as they lay out — no second trigger needed.
  //
  // Also drops any pending load-older anchor. item_id is per-thread sequential
  // ("2.0", "3.0", …), not globally unique, so a fetch triggered just before a
  // switch (or a send) would otherwise survive into the new thread's render
  // and — since this effect runs first (declared above the prepend-
  // compensation effect, same commit, same React layout-effect ordering) —
  // clearing it here always wins the race: the anchor is gone before that
  // effect's DOM query can accidentally match an unrelated node sharing the
  // stale id and yank the just-pinned bottom away.
  const scrollToBottomRequest = useTimelineStore((s) => s.scrollToBottomRequest);
  useLayoutEffect(() => {
    pendingAnchorRef.current = null;
    // A kept history entry's pending restore owns the position while it lasts
    // (the effect right below lands it). This expects the mount's own switch
    // bump — switchThread bumps on every mount, and that bump's pin run lands
    // one commit after the restore's first look — so every request in the
    // window is treated as part of entering the page. The window closes with
    // the first commit whose content is measurable (the restore clears the
    // flag), long before a user command can arrive; a restore abandoned on a
    // content mismatch leaves sticky at its mount value, so growth still
    // follows.
    if (pendingRestoreRef.current) return;
    // We are about to force the viewport to the bottom, so hide the button
    // immediately rather than waiting for the post-scroll measurement.
    setAtBottom(true);
    const viewport =
      viewportRef.current ??
      wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]') ??
      null;
    if (!viewport) return;
    // A user-commanded force-scroll (send / agent switch): pin with a
    // FRESH run — the reader's earlier departure is spent, so a
    // post-command twitch (trackpad nudge, momentum tail) cannot re-trigger
    // the stale run and silently release following while the awaited reply
    // streams in (user report 2026-09-19).
    pinToBottom(viewport, true);
  }, [scrollToBottomRequest, pinToBottom]);

  useLayoutEffect(() => {
    applyPendingRestore(
      viewportRef.current ??
        wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]') ??
        null,
    );
  });

  // Streamed-growth auto-scroll lives in the ResizeObserver above — any
  // change to the content height (items commit, throttled parse flush,
  // async image) snaps the viewport down in the same pre-paint tick while
  // sticky. There is deliberately no items-effect scroll: items changes
  // that matter all change the content height, and height is the signal
  // the bottom position actually depends on.
  //
  // (Writes use viewport.scrollTop = scrollHeight, not endRef.scrollIntoView
  // — scrollIntoView is occasionally unreliable under the ScrollArea
  // wrapper; writing viewport.scrollTop is a first-class DOM API that nails
  // the viewport to the bottom.)

  // Prepend-compensation: consume the anchor captured at load-older trigger.
  // When the older window lands, the anchor item is pushed down; scroll the
  // viewport by the anchor's ACTUAL document-space displacement since the
  // last commit, so the reading position does not move a single pixel.
  // Intervening commits (streaming deltas) leave the anchor still-first →
  // refresh its doc top and keep waiting. Thread switch → anchor node gone →
  // drop it. Runs in a layout effect (before paint) so there is no visible
  // jump.
  //
  // "Has it landed yet" is read from the ITEMS ARRAY (items[0].item_id), not
  // by comparing DOM nodes: the anchor was always captured as the array's
  // current front id (a turn-collapsed front renders via TurnBlock, itself
  // stamped data-item-id = its first member = items[0] by construction — see
  // the lookup below), so an unchanged front means an unchanged id, landed
  // means a different one. A DOM-identity check does not survive the run-
  // collapse case this effect exists for: a turn-collapse boundary sitting
  // right at the anchor is the common case in practice (agent turns are
  // mostly secondary chatter),
  // and prepending older items can extend that run's front so its NEW first
  // member — a just-prepended older item — is now the array's [0] AND still
  // the first rendered node, exactly like the anchor's own node was before
  // the prepend landed. Comparing DOM node identity cannot tell those two
  // "the front node didn't change" situations apart; the item id can.
  //
  // The compensation delta is measured in DOCUMENT space (rect.top +
  // scrollTop), not viewport space: document position is invariant under
  // user scrolls, so scrolling during the fetch cannot stale it, and
  // refreshing it on every pre-landing commit means a landing that follows
  // other landings measures only ITS OWN displacement — no double-counting
  // (#1272: the old code pinned the anchor to its trigger-time VIEWPORT top,
  // which (a) yanked the viewport back by however far the user scrolled
  // while the fetch was in flight, (b) double-counted when two windows
  // landed back-to-back, and (c) with the anchor below the viewport (the
  // expanded 0.0 prompt card is tens of thousands of px tall) scrolled by
  // the anchor's whole displacement — the reading position ended up tens of
  // thousands of px off-screen).
  //
  // Once landed, the node lookup tries the EXACT data-item-id first — the
  // anchor's own row, present whenever it is standalone or a member of an
  // EXPANDED run — and only falls back to data-turn-member-ids (the enclosing
  // TurnBlock) when no exact node exists, i.e. the anchor is now inside a
  // COLLAPSED run and its own row is unmounted. Trying both in one selector
  // would let the (document-order-earlier) TurnBlock shadow the exact row
  // whenever both exist, anchoring to the run's top instead of the anchor's
  // actual position inside it — wrong whenever the anchor is not the run's
  // first member.
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
    // Prepend landed? Standing context remains at the array front across a
    // prepend, so compare the first real item id captured at trigger instead.
    // Streaming commits / snapshot folds only touch the tail, so the front
    // real id is unchanged until an older window actually lands.
    const headNoteIds = standingHeadNoteIds(items);
    const frontRealId = items.find(
      (it) =>
        !isReattachedTimelineContext(it) &&
        !headNoteIds.has(it.item_id) &&
        !it.item_id.startsWith("_"),
    )?.item_id ?? null;
    if (frontRealId === anchor.frontId) {
      // Prepend not landed yet — keep waiting. Refresh the anchor's document
      // top so the eventual landing compensates the displacement since THIS
      // commit (a streaming fold / snapshot replace can shift the layout
      // while waiting). Document top (rect.top + scrollTop) is invariant
      // under user scrolls, so the user scrolling during the fetch cannot
      // stale it. The anchor node is re-located each time: the front run's
      // shape can change while waiting (a turn collapse unmounts rows).
      const node = findNode();
      if (!node) {
        pendingAnchorRef.current = null; // anchor gone (thread switch) — abandon
        return;
      }
      anchor.docTop = node.getBoundingClientRect().top + viewport.scrollTop;
      return;
    }
    // Landed. Force content-visibility:auto elements ABOVE the anchor to
    // render before measuring. Newly prepended items start off-screen with
    // contain-intrinsic-size estimates (80 px per item); without forcing a
    // render their actual heights are not yet known, so getBoundingClientRect
    // returns a position based on estimates. When the browser later renders
    // them (they are within the render threshold), their true heights replace
    // the estimates and the viewport jumps — the "load-older jitter".
    // A single pass against the anchor's pre-force top is NOT enough: every
    // forced element grows and pushes the elements below it down, so a row
    // whose estimate-based gap to the anchor is smaller than the accumulated
    // correction is skipped and keeps its 80px estimate — the measured delta
    // under-counts the real growth and the reading position sinks (#2623:
    // ~110px under-compensation on a real large window). Iterate to a
    // fixpoint instead: re-measure the anchor after each pass and keep
    // forcing rows whose CURRENT top is above its CURRENT top until the
    // anchor stops moving. Only rows above the anchor are forced — rows
    // below it cannot move it, and forcing the whole list (hundreds of
    // items, a ~90k px prompt card) would defeat the content-visibility
    // purpose every landing. Restoring `auto` afterwards is safe:
    // contain-intrinsic-size: auto remembers the rendered real height, so the
    // restored rows do not shrink back to the 80px estimate. All of this runs
    // in a layout effect (before paint) so the user never sees the
    // temporarily-rendered content.
    const anchorNode = findNode();
    if (!anchorNode) {
      pendingAnchorRef.current = null; // anchor gone (thread switch) — abandon
      return;
    }
    const cvSaved: { el: HTMLElement; cv: string }[] = [];
    let anchorTop = anchorNode.getBoundingClientRect().top;
    for (;;) {
      let forcedAny = false;
      for (const el of viewport.querySelectorAll<HTMLElement>('.timeline-item')) {
        if (el === anchorNode || el.contains(anchorNode)) break;
        if (el.style.contentVisibility === 'visible') continue;
        const r = el.getBoundingClientRect();
        if (r.top >= anchorTop) break; // at/below the anchor — the above-set is complete
        cvSaved.push({ el, cv: el.style.contentVisibility });
        el.style.contentVisibility = 'visible';
        forcedAny = true;
      }
      if (!forcedAny) break;
      const nextTop = anchorNode.getBoundingClientRect().top;
      if (nextTop === anchorTop) break;
      anchorTop = nextTop;
    }
    void viewport.getBoundingClientRect(); // force synchronous layout
    const node = findNode();
    if (!node) {
      for (const { el, cv } of cvSaved) el.style.contentVisibility = cv;
      pendingAnchorRef.current = null; // anchor gone (thread switch) — abandon
      return;
    }
    const delta = node.getBoundingClientRect().top + viewport.scrollTop - anchor.docTop;
    for (const { el, cv } of cvSaved) el.style.contentVisibility = cv;
    // A prepend always pushes the anchor DOWN (new content lands above it), so
    // a zero/negative delta means the content above was replaced or removed —
    // a compact reset / snapshot truncation, not a landing. Scrolling by that
    // delta would jump the viewport; abandon the anchor instead.
    if (delta <= 0) {
      pendingAnchorRef.current = null;
      return;
    }
    scrollByDelta(viewport, delta);
    pendingAnchorRef.current = null;
  }, [items, scrollByDelta]);

  // A load cycle that ends WITHOUT a landing must not leave the capture
  // pending — a failed fetch (or one that returned nothing) would otherwise
  // hang the anchor forever, and the pendingAnchorRef gate in
  // maybeLoadOlderAtTop would block every future auto-load: the reader's
  // paging would die silently until a thread switch (QA #3031). Ordering is
  // load-bearing: this effect is declared AFTER the landing effect, so on a
  // commit carrying a landing the landing runs first (compensating and
  // clearing the anchor); reaching this with loadingOlder back to false and
  // the anchor still pending means no landing is coming.
  const prevLoadingOlderReleaseRef = useRef(loadingOlder);
  useLayoutEffect(() => {
    if (prevLoadingOlderReleaseRef.current && !loadingOlder && pendingAnchorRef.current !== null) {
      pendingAnchorRef.current = null; // fetch ended without a landing — release the gate
    }
    prevLoadingOlderReleaseRef.current = loadingOlder;
  }, [loadingOlder]);

  // Single Details mode — governs default expanded state for every block
  // across all message kinds (All / Last / None).
  // The mode is DB-backed; until the settings query lands, useUserSettings
  // falls back to USER_SETTING_DEFAULTS (expand_runs_mode default "none",
  // user ruling 2026-09-17) and the loading state must render the safe
  // collapsed state regardless of what it reports — a stale "all" read would
  // flash every detail block open on every cold load (refresh / app start /
  // desktop rollout reload) before the real value arrives and collapses them
  // again ("details=none but blocks auto-expand" report). While loading,
  // render the collapsed state and switch to the real mode when it lands; a
  // reveal (collapsed → expanded for "all" users) is far less jarring than a
  // flash of everything open.
  const { detailsMode, isLoading: detailsModeLoading } = useContentToggle();
  const effectiveDetailsMode = detailsModeLoading ? "none" : detailsMode;

  // Configurable timeline colors, resolved from the user settings — one lookup
  // for the whole view; reference-stable while no display.color.* changes.
  const timelineColors = useTimelineColors();

  // Work-block collapse: fold adjacent secondary items into aggregate work blocks
  // under a "Details" header. Always on — the Details toggle button controls
  // expand-all / collapse-all, not show/hide. Each block's expand state is a Map
  // of turn ids (the first item_id, stable across streaming commits) → the
  // user's PINNED expanded state: a click always flips the turn's current
  // rendered state and pins it, so the choice survives default changes — most
  // importantly the streaming last turn auto-collapsing when the agent goes
  // idle. Absence from the map means "follow the detailsMode-driven default".
  // (A plain "membership = opposite of the default" set, as `overrides` uses,
  // cannot represent the last turn: its default itself flips when turnActive
  // ends, so an override stored against the old default meant the wrong thing
  // afterwards and clicks appeared dead — #510.)

  const [turnOverrides, setTurnOverrides] = useState<ReadonlyMap<string, boolean>>(
    () => new Map(),
  );
  const toggleTurn = useCallback((id: string, currentlyExpanded: boolean) => {
    setTurnOverrides((prev) => {
      const next = new Map(prev);
      next.set(id, !currentlyExpanded);
      return next;
    });
  }, []);

  // Per-item expand/collapse overrides: clicking a card header flips one item
  // to the opposite of its detailsMode-driven default. A Set of overridden
  // item ids — membership means "opposite of the current default".
  const [overrides, setOverrides] = useState<ReadonlySet<string>>(
    () => new Set(),
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

  // When detailsMode changes, clear all per-item and per-turn overrides
  // so the new mode takes full effect. State is adjusted during render
  // (the React "storing information from previous renders" pattern, not
  // an effect) so the cleared overrides commit in the same paint as the
  // new default.
  const [prevDetailsMode, setPrevDetailsMode] = useState(effectiveDetailsMode);
  if (prevDetailsMode !== effectiveDetailsMode) {
    setPrevDetailsMode(effectiveDetailsMode);
    setOverrides(new Set());
    setTurnOverrides(new Map());
    setActiveStuckHeaderId(null);
    setActiveStuckChildId(null);
  }

  // item_ids are message indexes local to each thread, so the same ids recur
  // across agents. Scope pinned state to the thread or an agent switch can
  // resurrect another conversation's expansion choices.
  const [prevThreadKey, setPrevThreadKey] = useState(threadKey);
  if (prevThreadKey !== threadKey) {
    setPrevThreadKey(threadKey);
    setOverrides(new Set());
    setTurnOverrides(new Map());
    setActiveStuckHeaderId(null);
    setActiveStuckChildId(null);
  }

  // Same-mode re-pick (user ruling 2026-08-06): the selector bumps this token
  // on EVERY selection — including re-picking the current mode — so manual
  // expansions made under that mode are reverted (None → collapse all again).
  const resetToken = useContentToggleReset((s) => s.resetToken);
  const [prevResetToken, setPrevResetToken] = useState(resetToken);
  if (prevResetToken !== resetToken) {
    setPrevResetToken(resetToken);
    setOverrides(new Set());
    setTurnOverrides(new Map());
    setActiveStuckHeaderId(null);
    setActiveStuckChildId(null);
  }

  // Sync the stuck header on changes or layout shifts. turnOverrides /
  // overrides flip an expanded state (which arms or releases a pin); items
  // covers content growth/removal.
  useLayoutEffect(() => {
    updateStuckHeader();
  }, [updateStuckHeader, turnOverrides, overrides, effectiveDetailsMode, items]);

  useEffect(() => {
    return () => {
      if (stuckRafRef.current !== null) {
        cancelAnimationFrame(stuckRafRef.current);
        stuckRafRef.current = null;
      }
    };
  }, []);

  // Find the index of the last agent_chat — fork button attaches only there
  const lastAgentChatIdx = (() => {
    for (let i = items.length - 1; i >= 0; i--) {
      if (items[i].kind === "agent_chat") return i;
    }
    return -1;
  })();

  // One rendered row for the item at its global `index`. Shared verbatim by the
  // ungrouped path and the expanded TurnBlock body, so turn-collapse never changes
  // how an individual item renders — it only decides whether the item sits under
  // a run header. All memo-stability inputs (config via the WeakMap cache, the
  // stable toggleExpanded) are captured here.
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
    // Ephemeral system markers (config === null): bare, not collapsible. The
    // expanded / showActions / fork props are inert for them.
    if (config === null) {
      return (
        <TimelineRow
          key={renderKey}
          timelineSource={source}
          displayRank={rank}
          item={item}
          config={null}
          streaming={streaming}
          expanded={false}
          showActions={false}
          stickyHeader={false}
          isStuck={false}
          onToggle={toggleExpanded}
          onFork={null}
          forkPending={false}
        />
      );
    }
    // Default expanded state: driven by the single Details mode.
    //   "all"  — all blocks expanded
    //   "last" — only the last item expanded (follows streaming), but primary
    //     items (agent_chat, human inbound) are always expanded — "last" only
    //     collapses secondary detail blocks (thinking / code / output).
    //   "none" — all blocks collapsed
    // A live override always means "opposite of the current default".
    const isPrimary = classifyItem(item) === "primary";
    const isLastItem = index === items.length - 1;
    const defaultExpanded = isPrimary || forceExpand ? true
      : effectiveDetailsMode === "all" ? true
      : effectiveDetailsMode === "last" ? isLastItem
      : config.fixedDefault;
    const expanded = overrides.has(item.item_id) ? !defaultExpanded : defaultExpanded;
    // Copy attaches inside every card's bottom-right corner (hover-revealed,
    // see MessageCard's `actions` prop) once expanded and no longer
    // streaming — a still-changing payload is not a stable copy target, and
    // a collapsed card shows nothing at all (not even hover-invisible DOM).
    const showActions = expanded && !item.partial;
    // Fork attaches only under the last agent_chat; hand the fork handler +
    // pending flag to that row ALONE so a forkPending flip (or an unstable
    // onFork) can never re-render the other rows.
    const isForkRow = index === lastAgentChatIdx;
    return (
      <TimelineRow
        key={renderKey}
        timelineSource={source}
        displayRank={rank}
        item={item}
        config={config}
        streaming={streaming}
        expanded={expanded}
        showActions={showActions}
        // A primary item is always a top-level card (it breaks turns), so its
        // header pins below the HeaderBar; a secondary item is always a work-
        // block child, whose header pins nested under the turn's (task #3215).
        // The stuck id can only match while the row is expanded (see
        // TimelineRow's data-card-sticky gate) — the level sets which id applies.
        stickyHeader={isPrimary ? "top" : "nested"}
        isStuck={
          isPrimary
            ? activeStuckHeaderId === item.item_id
            : activeStuckChildId === item.item_id
        }
        onToggle={toggleExpanded}
        onFork={isForkRow ? onFork ?? null : null}
        forkPending={isForkRow ? forkPending ?? false : false}
      />
    );
  };

  // Fold adjacent secondary items into work blocks (always on). Secondary items
  // are always groupable — including in-progress / streaming items — so the first
  // streaming chunk lands directly inside a work block (no bare-then-wrap layout
  // shift). Primary/bare items break turns by classifyItem returning non-secondary.
  // Scroll/pull indicators and expansion pins do not change the document.
  // Reuse its grouping until a snapshot, history page, or live item changes it.
  const groups = useMemo(() => {
    if (!compactBuffer) {
      return groupTimelineSegments(canonicalItems).map((entry) => ({ ...entry, source: "canonical" as const }));
    }
    const buffered = groupTimelineSegments(
      bufferedItems,
      (_item, index) => compactBuffer.rows[index].rank,
    ).map((entry) => ({ ...entry, source: "buffer" as const }));
    const current = groupTimelineSegments(currentItems, undefined, buffered.length > 0)
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
        : entry.group.kind === "turn" ? 56 : 80) + (entry.dividerRank === null ? 0 : 32),
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
    turnRows,
    measureRows,
    viewportRef,
    contentRef,
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
    // requestStick (not a pin): the smooth ride to the bottom is
    // asynchronous, so sticky must be on before the intermediate scroll
    // events arrive. The controller's position rule never unsticks on
    // downward motion, so the ride survives; on arrival dist < bottomZone
    // confirms the stick. Growth mid-ride pins instantly (the controller
    // is already sticky) — same behavior as before.
    controller.requestStick();
    setAtBottom(true);
    viewport.scrollTo({ top: viewport.scrollHeight, behavior: "smooth" });
  }, [controller]);

  return (
    <div ref={wrapperRef} className={cn("relative", FLEX_1, MIN_H_0, OVERFLOW_HIDDEN)}>
      <LoadOlderSpinner loadingOlder={loadingOlder} />
      <ColdLoadSpinner show={loading && items.length === 0} />
      {/* overflow-anchor: none disables Chrome scroll anchoring on the timeline
          viewport. Scroll anchoring silently adjusts scrollTop to keep the
          on-screen content stable when height changes ABOVE the viewport — but
          the timeline's own sticky-bottom controller (lib/sticky.ts) owns
          scrollTop, and a net-neutral reflow (content shrinks above + grows
          below) makes the browser move scrollTop >20px on its own, a "third
          hand" the sticky state machine can't attribute → a false unstick. Kill
          it here so the controller is the only thing that moves the viewport.
          overscroll-y-contain stops a vertical scroll gesture that exhausts the
          timeline's own range from chaining into page-level scroll/bounce —
          it does not touch scrollTop itself, so it is inert to the sticky
          controller and the touch bounce-tolerance zones in lib/sticky.ts. */}
      <ScrollArea
        className="h-full text-[13px] leading-relaxed"
        viewportClassName="[overflow-anchor:none] overscroll-y-contain"
      >
        {/* BAR_CLEAR_TOP_PADDING_CLASS (52px) clears the floating
            HeaderBar (h-11, lib/layout.ts) — 52px = 44px bar + 8px clearance,
            derived from BAR_HEIGHT_PX so a bar-height change can't silently
            slide the first row underneath it (user ruling 2026-08-06); the
            composer stack lives in normal flow below the surface (user
            ruling 2026-08-06 11:35) so only a small pb remains. */}
        <div
          ref={contentRef}
          role="log"
          data-timeline-item-count={items.length}
          aria-live="polite"
          aria-relevant="additions"
          style={maxWidthCss ? { maxWidth: maxWidthCss } : undefined}
          className={cn("mx-auto w-full px-4 pb-3 space-y-3", BAR_CLEAR_TOP_PADDING_CLASS, virtualEnabled && "timeline-virtual")}
        >
          {turnAnnouncement ? (
            <span key={turnAnnouncement} className="sr-only" data-testid="timeline-turn-announcement">
              {turnAnnouncement}
            </span>
          ) : null}
          <ConnectionNotice />
          {/* Rows and history dividers opt out individually so streaming and
              prepends stay quiet without changing the direct-child timeline
              structure used by scroll anchoring and compact-history dividers. */}
          {virtualEnabled && virtualRange.before > 0 ? (
            <div data-timeline-spacer="before" style={{ height: virtualRange.before }} aria-hidden="true" />
          ) : null}
          {groupEntries.slice(virtualRange.start, virtualRange.end).map(({ entry, virtualKey, runExpanded, virtualRows }, offset) => {
            const group = entry.group;
            const groupIndex = virtualRange.start + offset;
            const renderedGroup = (() => {
              if (group.kind === "single") {
                return renderRow(group.item, entry.indexOffset + group.index, entry.source, entry.rank);
              }
              // Every secondary run (even a single item) becomes a collapsible work
              // block. The last turn auto-expands while the agent is active so the
              // streaming item is visible. Run id = the first member's item_id
              // (stable across streaming commits).
              const turnId = group.items[0].item_id;
              const isLastTurn = entry === groups[groups.length - 1];
              const childRange = rowRange(groupIndex);
              return (
                <TurnBlock
                  id={turnId}
                  memberIds={group.items.map((it) => it.item_id)}
                  summary={group.summary}
                  expanded={runExpanded}
                  onToggle={() => toggleTurn(turnId, runExpanded)}
                  turnActive={turnActive && isLastTurn}
                  isStuck={activeStuckHeaderId === turnId}
                  timelineSource={entry.source}
                  displayRank={entry.rank}
                >
                  {runExpanded ? (
                    virtualRows ? (
                      <>
                        {virtualEnabled && childRange.before > 0 ? <div data-timeline-spacer="turn-before" style={{ height: childRange.before }} aria-hidden="true" /> : null}
                        {group.items.slice(childRange.start, childRange.end).map((it, offset) => {
                          const index = childRange.start + offset;
                          return (
                            <div key={virtualRows[index]} data-virtual-row={virtualRows[index]}>
                              {renderRow(it, entry.indexOffset + group.startIndex + index, entry.source, entry.rank, runExpanded)}
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
            const content = (
              <>
                {entry.dividerRank === null ? null : (
                  <CompactHistoryDivider rank={entry.dividerRank} />
                )}
                {renderedGroup}
              </>
            );
            return (
              <div key={virtualKey} data-virtual-group={virtualKey} className={entry.dividerRank === null ? undefined : "space-y-3"}>
                {content}
              </div>
            );
          })}
          {virtualEnabled && virtualRange.after > 0 ? (
            <div data-timeline-spacer="after" style={{ height: virtualRange.after }} aria-hidden="true" />
          ) : null}
          <CompactingBlock />
          <div ref={endRef} />
        </div>
      </ScrollArea>
      <ScrollToBottomButton atBottom={atBottom} onClick={handleScrollToBottom} />
    </div>
  );
}
