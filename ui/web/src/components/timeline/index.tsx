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
//   keyed on the stable item ref), so the config object stays reference-
//   stable for unchanged rows even as the items array is rebuilt each chunk.
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
//    memoizes it per item ref (WeakMap) so a row's config stays reference-stable
//    across the array rebuilds that streaming triggers.
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
// - `./overlays`  — PullToLoadIndicator / LoadOlderButton / ColdLoadSpinner / ScrollToBottomButton
import {
  Fragment,
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
import { useTimelineStore } from "@/lib/timeline-store";
import { BAR_HEIGHT_PX, BAR_CLEAR_TOP_PADDING_CLASS, FLEX, FLEX_1, MIN_H_0, OVERFLOW_HIDDEN } from "@/lib/layout";
import { cn } from "@/lib/utils";

import { ConnectionNotice } from "@/components/connection-notice";
import { findClosestStuckTurnId, TurnBlock } from "./run-block";
import { classifyItem, groupIntoTurns, type TimelineGroup } from "./runs";
import { LoadOlderButton, PullToLoadIndicator, ColdLoadSpinner, ScrollToBottomButton } from "./overlays";
import { TimelineRow, cardConfigFor } from "./row";


interface Props {
  items: BackendTimelineItem[];
  /** Identity of the timeline thread / active agent. A change means a new
   *  conversation is displayed, so all per-item and per-turn pins are dropped. */
  threadKey?: string;
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
  // Scroll-up history loading: the timeline holds only a tail window; when
  // the user scrolls near the top and older items remain, onLoadOlder
  // fetches + prepends the previous window. hasMoreOlder gates the trigger;
  // loadingOlder shows the in-flight hint and guards re-triggering.
  hasMoreOlder?: boolean;
  loadingOlder?: boolean;
  onLoadOlder?: () => void;
  // Cold-load flag: the snapshot for this thread is being fetched and there is
  // nothing cached yet. Drives a centered spinner instead of a blank pane so a
  // cold switch doesn't flash empty then pop the whole column in at once.
  loading?: boolean;
  /** Conversation-column cap. The scroll surface is full-bleed (user ruling
   *  2026-08-06 — scrollbar at the pane edge, gutters scrollable); the item
   *  column itself stays capped and centered inside it. Falsy = full width. */
  maxWidthCss?: string;
}

// Pull-down-to-load thresholds. Pulling down past top fills the circular
// progress indicator; releasing once the threshold is met triggers loading the
// older compact history.
export const PULL_THRESHOLD_PX = 56;
export const MAX_PULL_PX = 80;

interface RenderGroup {
  readonly group: TimelineGroup;
  readonly indexOffset: number;
  readonly dividerRank: number | null;
}

function segmentKey(item: BackendTimelineItem): string {
  const parts = parseItemIdParts(item.item_id);
  return parts && parts.rank > 0
    ? `${parts.rank}:${parts.checkpointId ?? ""}`
    : "current";
}

/** Keep collapsible runs inside one compact segment without changing items. */
function groupTimelineSegments(items: readonly BackendTimelineItem[]): RenderGroup[] {
  const result: RenderGroup[] = [];
  let start = 0;
  while (start < items.length) {
    const key = segmentKey(items[start]);
    const rank = parseItemIdParts(items[start].item_id)?.rank ?? 0;
    let end = start + 1;
    while (end < items.length && segmentKey(items[end]) === key) end += 1;
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
      result.push({ group, indexOffset: start, dividerRank: null });
    }
    for (const group of summaryGroups) {
      result.push({ group, indexOffset: start + summaryIndex, dividerRank: null });
    }
    rawGroups.forEach((group, groupIndex) => {
      result.push({
        group,
        indexOffset: start + rawStart,
        dividerRank: rank > 0 && groupIndex === 0 ? rank : null,
      });
    });
    start = end;
  }
  return result;
}

function CompactHistoryDivider({ rank }: { readonly rank: number }) {
  const t = useTranslations("timeline");
  return (
    <div
      data-testid="compact-history-divider"
      data-segment-rank={rank}
      aria-live="off"
      className={cn("items-center gap-2 py-1 text-[11px] text-muted-foreground/70", FLEX)}
    >
      <span aria-hidden="true" className={cn("h-px bg-border/60", FLEX_1)} />
      <span aria-hidden="true" className="shrink-0">↑</span>
      <span className="shrink-0">{t("compactHistoryDivider")}</span>
      <span aria-hidden="true" className={cn("h-px bg-border/60", FLEX_1)} />
    </div>
  );
}

export function TimelineView({
  items,
  threadKey,
  streamingCode = false,
  turnActive = false,
  onFork,
  forkPending,
  hasMoreOlder = false,
  loadingOlder = false,
  onLoadOlder,
  loading = false,
  maxWidthCss,
}: Props) {
  const t = useTranslations("timeline");
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
  // Live mirrors for the scroll handler (registered once with empty deps, so
  // it must read the latest load-older props through refs, not a stale
  // closure). Synced in an effect — refs must not be written during render.
  const loadOlderRef = useRef<(() => void) | undefined>(onLoadOlder);
  const hasMoreOlderRef = useRef(hasMoreOlder);
  const loadingOlderRef = useRef(loadingOlder);
  const itemsRef = useRef(items);
  useEffect(() => {
    loadOlderRef.current = onLoadOlder;
    hasMoreOlderRef.current = hasMoreOlder;
    loadingOlderRef.current = loadingOlder;
    itemsRef.current = items;
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
  const pendingAnchorRef = useRef<{ id: string; frontId: string | null; docTop: number } | null>(null);

  // Pull-down-to-load state and gesture tracking.
  const [pullDistance, setPullDistance] = useState(0);
  const pullDistanceRef = useRef(0);
  const latestPullRef = useRef(0);
  const isPullingTouchRef = useRef(false);
  const touchStartYRef = useRef<number | null>(null);
  const touchStartXRef = useRef<number | null>(null);
  const wheelPullRef = useRef(0);
  const wheelTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const rafIdRef = useRef<number | null>(null);
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
  // Settled-at-top flag driving the load-older fallback control (keyboard /
  // screen-reader / scrollbar users reach the top via scroll-only inputs, so
  // they need an explicit affordance; the pull gestures below do not fire for
  // them). Measured in onScroll and in the ResizeObserver callback so a
  // short thread whose content fits the viewport (scrollTop stays 0, no
  // scroll event ever fires) still gets the control.
  const [atTop, setAtTop] = useState(false);
  const stuckRafRef = useRef<number | null>(null);
  const [activeStuckTurnId, setActiveStuckTurnId] = useState<string | null>(null);

  const updateStuckTurn = useCallback(() => {
    const viewport =
      viewportRef.current ??
      wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]') ??
      null;
    if (!viewport) return;
    const nextStuckId = findClosestStuckTurnId(viewport, BAR_HEIGHT_PX);
    setActiveStuckTurnId((prev) => (prev === nextStuckId ? prev : nextStuckId));
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

  const measureAtTop = useCallback(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    setAtTop(viewport.scrollTop <= 0);
  }, []);

  // Pin the viewport to the bottom and report the performed scroll back to
  // the controller (post-write snapshot, so the browser-clamped actual
  // scrollTop becomes the controller's baseline).
  const pinToBottom = useCallback(
    (viewport: HTMLElement) => {
      viewport.scrollTop = viewport.scrollHeight;
      controller.notifyPinnedToBottom({
        scrollTop: viewport.scrollTop,
        scrollHeight: viewport.scrollHeight,
        clientHeight: viewport.clientHeight,
      });
    },
    [controller],
  );

  // Shift the viewport by delta (prepend-compensation). Not reported to
  // the controller: prepend always shifts DOWN (the older window pushes
  // the anchor down) and the position rule never unsticks on downward
  // motion, so the echo is inert and re-syncs the baseline on its own.
  const scrollByDelta = useCallback((viewport: HTMLElement, delta: number) => {
    viewport.scrollTop += delta;
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
    for (const n of viewport.querySelectorAll<HTMLElement>("[data-item-id]")) {
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
        frontId: frontRealId,
        docTop: anchorNode.getBoundingClientRect().top + viewport.scrollTop,
      };
    }
  }, []);

  // rAF-throttled pull-distance render: touchmove/wheel can fire at
  // 60-120/s; one state update per animation frame keeps the indicator
  // silky without re-rendering the whole timeline per event.
  const schedulePullRender = useCallback((pull: number) => {
    pullDistanceRef.current = pull;
    latestPullRef.current = pull;
    if (rafIdRef.current !== null) return; // a frame is already scheduled
    rafIdRef.current = requestAnimationFrame(() => {
      rafIdRef.current = null;
      setPullDistance(latestPullRef.current);
    });
  }, []);

  // Click path of the load-older fallback control: same anchor capture as the
  // gesture paths, so the prepend lands with zero jitter for this input too.
  const handleLoadOlderClick = useCallback(() => {
    captureAnchor();
    loadOlderRef.current?.();
  }, [captureAnchor]);

  useLayoutEffect(() => {
    if (loadingOlder && !prevLoadingOlderRef.current && pendingAnchorRef.current === null) {
      captureAnchor();
    }
    prevLoadingOlderRef.current = loadingOlder;
  }, [loadingOlder, captureAnchor]);

  useEffect(() => {
    const viewport = wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]') ?? null;
    if (!viewport) return;
    viewportRef.current = viewport;
    const snapshot = () => ({
      scrollTop: viewport.scrollTop,
      scrollHeight: viewport.scrollHeight,
      clientHeight: viewport.clientHeight,
    });
    // Every scroll event goes to the controller. Inertial momentum scroll
    // to top stops naturally at scrollTop = 0 without auto-triggering loadOlder.
    const onScroll = () => {
      controller.handleScroll(snapshot());
      measureAtBottom();
      measureAtTop();
      stuckRafRef.current ??= requestAnimationFrame(() => {
        stuckRafRef.current = null;
        updateStuckTurn();
      });
    };
    viewport.addEventListener("scroll", onScroll, { passive: true });

    // Wheel intent — the "stop following" signal a slow scroll-up needs on
    // mouse/trackpad (per-event scroll deltas stay under unstickDeltaPx
    // while auto-scroll keeps re-pinning the baseline, so position alone
    // can never express it). The controller absorbs upward notches at the
    // bottom (resting-finger noise) and at the last-pinned bottom (a chunk
    // grew the content between this event and the pin that follows).
    // Touch devices never fire wheel. The same event also drives the
    // pull-down-to-load gesture when the viewport is already at the top.
    const onWheel = (e: WheelEvent) => {
      controller.handleWheel(e.deltaY, snapshot());
      if (hasMoreOlderRef.current && !loadingOlderRef.current && viewport.scrollTop <= 0) {
        if (e.deltaY < 0) {
          if (wheelTimerRef.current) clearTimeout(wheelTimerRef.current);
          wheelPullRef.current = Math.min(
            MAX_PULL_PX,
            wheelPullRef.current + Math.abs(e.deltaY) * 0.35,
          );
          const currentPull = wheelPullRef.current;
          schedulePullRender(currentPull);
          wheelTimerRef.current = setTimeout(() => {
            const currentPull = wheelPullRef.current;
            // A slow frame can let the 180ms settle fire before the pull's
            // rAF render ran — the ring would never display while the load
            // still fires (#2623 P2). Flush the full fill synchronously so it
            // always paints once, then reset on the next frame (a new pull
            // started in between keeps its ring: the reset frame renders
            // latestPullRef, which a new pull updated).
            if (rafIdRef.current) cancelAnimationFrame(rafIdRef.current);
            rafIdRef.current = requestAnimationFrame(() => {
              rafIdRef.current = null;
              setPullDistance(latestPullRef.current);
            });
            setPullDistance(currentPull);
            if (
              currentPull >= PULL_THRESHOLD_PX &&
              hasMoreOlderRef.current &&
              !loadingOlderRef.current
            ) {
              captureAnchor();
              loadOlderRef.current?.();
            }
            wheelPullRef.current = 0;
            pullDistanceRef.current = 0;
            latestPullRef.current = 0;
          }, 180);
        } else if (e.deltaY > 0) {
          if (wheelPullRef.current > 0) {
            wheelPullRef.current = 0;
            pullDistanceRef.current = 0;
            latestPullRef.current = 0;
            if (rafIdRef.current) cancelAnimationFrame(rafIdRef.current);
            rafIdRef.current = null;
            setPullDistance(0);
            if (wheelTimerRef.current) clearTimeout(wheelTimerRef.current);
          }
        }
      }
    };
    viewport.addEventListener("wheel", onWheel, { passive: true });

    // Touch pull-down gesture at the top of the viewport.
    const onTouchStart = (e: TouchEvent) => {
      controller.handleTouchStart();
      // DOM lib types touches as always-present, but plain Event dispatches
      // (tests, synthetic events) can carry none — read it as optional.
      const touches = e.touches as TouchList | undefined;
      if (
        touches?.length === 1 &&
        viewport.scrollTop <= 0 &&
        hasMoreOlderRef.current &&
        !loadingOlderRef.current
      ) {
        // A hybrid device can arm a wheel pull (its settle timer still
        // pending) and then start a touch pull; the stale wheel timer would
        // fire mid-touch and zero the touch pull state, killing the release
        // trigger. Retire the wheel pull when the touch pull arms.
        if (wheelTimerRef.current) {
          clearTimeout(wheelTimerRef.current);
          wheelTimerRef.current = null;
        }
        wheelPullRef.current = 0;
        touchStartYRef.current = touches[0].clientY;
        touchStartXRef.current = touches[0].clientX;
        isPullingTouchRef.current = true;
      } else {
        touchStartYRef.current = null;
        touchStartXRef.current = null;
        isPullingTouchRef.current = false;
      }
    };

    const onTouchMove = (e: TouchEvent) => {
      const touches = e.touches as TouchList | undefined;
      if (
        !isPullingTouchRef.current ||
        touchStartYRef.current === null ||
        touchStartXRef.current === null ||
        !touches?.length
      ) {
        return;
      }
      if (!hasMoreOlderRef.current || loadingOlderRef.current || viewport.scrollTop > 0) {
        isPullingTouchRef.current = false;
        pullDistanceRef.current = 0;
        latestPullRef.current = 0;
        if (rafIdRef.current) cancelAnimationFrame(rafIdRef.current);
        rafIdRef.current = null;
        setPullDistance(0);
        return;
      }
      const dy = touches[0].clientY - touchStartYRef.current;
      const dx = touches[0].clientX - touchStartXRef.current;
      if (dy > 0 && Math.abs(dy) > Math.abs(dx)) {
        const pull = Math.min(MAX_PULL_PX, dy * 0.45);
        schedulePullRender(pull);
        if (e.cancelable && dy > 5) {
          e.preventDefault();
        }
      } else if (dy <= 0) {
        pullDistanceRef.current = 0;
        latestPullRef.current = 0;
        if (rafIdRef.current) cancelAnimationFrame(rafIdRef.current);
        rafIdRef.current = null;
        setPullDistance(0);
      }
    };

    const onTouchEnd = () => {
      controller.handleTouchEnd(snapshot());
      if (isPullingTouchRef.current) {
        isPullingTouchRef.current = false;
        const currentPull = pullDistanceRef.current;
        if (
          currentPull >= PULL_THRESHOLD_PX &&
          hasMoreOlderRef.current &&
          !loadingOlderRef.current
        ) {
          captureAnchor();
          loadOlderRef.current?.();
        }
        pullDistanceRef.current = 0;
        latestPullRef.current = 0;
        if (rafIdRef.current) cancelAnimationFrame(rafIdRef.current);
        rafIdRef.current = null;
        setPullDistance(0);
      }
      touchStartYRef.current = null;
      touchStartXRef.current = null;
    };

    viewport.addEventListener("touchstart", onTouchStart, { passive: true });
    viewport.addEventListener("touchmove", onTouchMove, { passive: false });
    viewport.addEventListener("touchend", onTouchEnd, { passive: true });
    viewport.addEventListener("touchcancel", onTouchEnd, { passive: true });

    // ResizeObserver callbacks run after layout and before paint.
    const ro = new ResizeObserver(() => {
      if (controller.handleLayoutChange(snapshot())) pinToBottom(viewport);
      measureAtBottom();
      measureAtTop();
    });
    ro.observe(viewport);
    if (contentRef.current) ro.observe(contentRef.current);

    return () => {
      viewport.removeEventListener("scroll", onScroll);
      viewport.removeEventListener("wheel", onWheel);
      viewport.removeEventListener("touchstart", onTouchStart);
      viewport.removeEventListener("touchmove", onTouchMove);
      viewport.removeEventListener("touchend", onTouchEnd);
      viewport.removeEventListener("touchcancel", onTouchEnd);
      if (wheelTimerRef.current) clearTimeout(wheelTimerRef.current);
      if (rafIdRef.current) cancelAnimationFrame(rafIdRef.current);
      ro.disconnect();
    };
  }, [controller, measureAtBottom, measureAtTop, pinToBottom, updateStuckTurn, captureAnchor, schedulePullRender]);

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
    // We are about to force the viewport to the bottom, so hide the button
    // immediately rather than waiting for the post-scroll measurement.
    // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional one-shot sync on force-scroll
    setAtBottom(true);
    const viewport =
      viewportRef.current ??
      wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]') ??
      null;
    if (!viewport) return;
    pinToBottom(viewport);
  }, [scrollToBottomRequest, pinToBottom]);

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
      viewport.querySelector<HTMLElement>(`[data-item-id="${escapedId}"]`) ??
      viewport.querySelector<HTMLElement>(`[data-turn-member-ids~="${escapedId}"]`);
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
    void viewport.offsetHeight; // force synchronous layout
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

  // Single Details mode — governs default expanded state for every block
  // across all message kinds (All / Last / None).
  // The mode is DB-backed; until the settings query lands, useUserSettings
  // falls back to USER_SETTING_DEFAULTS whose expand_runs_mode default is
  // "all" — rendering with that would flash every detail block open on every
  // cold load (refresh / app start / desktop rollout reload) before the real
  // value arrives and collapses them again ("details=none but blocks
  // auto-expand" report). While loading, render the safe collapsed state and
  // switch to the real mode when it lands; a reveal (collapsed → expanded
  // for "all" users) is far less jarring than a flash of everything open.
  const { detailsMode, isLoading: detailsModeLoading } = useContentToggle();
  const effectiveDetailsMode = detailsModeLoading ? "none" : detailsMode;

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
    setActiveStuckTurnId(null);
  }

  // item_ids are message indexes local to each thread, so the same ids recur
  // across agents. Scope pinned state to the thread or an agent switch can
  // resurrect another conversation's expansion choices.
  const [prevThreadKey, setPrevThreadKey] = useState(threadKey);
  if (prevThreadKey !== threadKey) {
    setPrevThreadKey(threadKey);
    setOverrides(new Set());
    setTurnOverrides(new Map());
    setActiveStuckTurnId(null);
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
    setActiveStuckTurnId(null);
  }

  // Sync stuck turn on changes or layout shifts
  useLayoutEffect(() => {
    updateStuckTurn();
  }, [updateStuckTurn, turnOverrides, effectiveDetailsMode, items]);

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
  const renderRow = (item: BackendTimelineItem, index: number, forceExpand?: boolean) => {
    const config = cardConfigFor(item);
    const streaming =
      streamingCode && index === items.length - 1 && item.kind === "agent_code";
    // Ephemeral system markers (config === null): bare, not collapsible. The
    // expanded / showActions / fork props are inert for them.
    if (config === null) {
      return (
        <TimelineRow
          key={item.item_id}
          item={item}
          config={null}
          streaming={streaming}
          expanded={false}
          showActions={false}
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
        key={item.item_id}
        item={item}
        config={config}
        streaming={streaming}
        expanded={expanded}
        showActions={showActions}
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
  const groups = groupTimelineSegments(items);

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
      <PullToLoadIndicator pullDistance={pullDistance} pullThreshold={PULL_THRESHOLD_PX} loadingOlder={loadingOlder} />
      <LoadOlderButton
        visible={atTop && hasMoreOlder && !loadingOlder && pullDistance === 0}
        onClick={handleLoadOlderClick}
      />
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
          aria-live="polite"
          aria-relevant="additions"
          style={maxWidthCss ? { maxWidth: maxWidthCss } : undefined}
          className={cn("mx-auto w-full px-4 pb-3 space-y-3", BAR_CLEAR_TOP_PADDING_CLASS)}
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
          {groups.map((entry) => {
            const group = entry.group;
            const groupKey =
              group.kind === "single" ? group.item.item_id : group.items[0].item_id;
            const renderedGroup = (() => {
              if (group.kind === "single") {
                return renderRow(group.item, entry.indexOffset + group.index);
              }
              // Every secondary run (even a single item) becomes a collapsible work
              // block. The last turn auto-expands while the agent is active so the
              // streaming item is visible. Run id = the first member's item_id
              // (stable across streaming commits).
              const turnId = group.items[0].item_id;
              // The turn is "last" only when it is the last group overall — not just
              // the last turn-kind group. When a primary item follows this turn, the
              // turn is no longer last and its live clock must stop immediately.
              const isLastTurn = entry === groups[groups.length - 1];
              const runExpanded = turnOverrides.has(turnId)
                ? (turnOverrides.get(turnId) ?? false)
                : effectiveDetailsMode === "all"
                  ? true
                  : effectiveDetailsMode === "last"
                    ? isLastTurn && turnActive
                    : false;
              return (
                <TurnBlock
                  id={turnId}
                  memberIds={group.items.map((it) => it.item_id)}
                  summary={group.summary}
                  expanded={runExpanded}
                  onToggle={() => toggleTurn(turnId, runExpanded)}
                  turnActive={turnActive && isLastTurn}
                  isStuck={activeStuckTurnId === turnId}
                >
                  {runExpanded
                    ? group.items.map((it, i) =>
                        renderRow(
                          it,
                          entry.indexOffset + group.startIndex + i,
                          runExpanded,
                        ),
                      )
                    : null}
                </TurnBlock>
              );
            })();
            return (
              <Fragment key={`segment-group:${groupKey}`}>
                {entry.dividerRank === null ? null : (
                  <CompactHistoryDivider rank={entry.dividerRank} />
                )}
                {renderedGroup}
              </Fragment>
            );
          })}
          <div ref={endRef} />
        </div>
      </ScrollArea>
      <ScrollToBottomButton atBottom={atBottom} onClick={handleScrollToBottom} />
    </div>
  );
}
