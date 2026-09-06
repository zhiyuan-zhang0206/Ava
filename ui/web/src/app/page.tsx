"use client";

// Ava home page — three-column layout: agent sidebar on the left,
// timeline + composer in the center, inspector panel on the right.
//
// agent_id == agent_id 1:1. Picking an agent in the sidebar == selecting
// an agent. State is split across two hooks (lib/use-agents +
// lib/use-timeline); this file only wires things up:
// - useAgents: agent list / lifecycle actions / pending state, all
//   driven by initial fetch + SSE event merge (no polling, no
//   optimistic writes — see lib/use-agents.ts)
// - useTimeline: BackendTimelineItem list merged from three sources + SSE folding
//
// Component layering:
// - HomePage: reads only the toast trigger (no other server state) so it
//   doesn't subscribe to SSE itself. The global /api/system broadcast is
//   provided once at the app root (components/providers.tsx), so it persists
//   across page navigation; the toast renderer lives there too (ToastHost).
// - HomeShell: calls useAgents (a reader of the scoped agents caches that
//   the root fold keeps live), owns activeId and the lifecycle
//   action wrappers, owns the inspector panel open state + preload queries,
//   threads agents/pendingActions/pendingSpawnCount/forkPending down as props.
//   Wraps HomeContent in AgentEventStreamProvider — the active-agent
//   /api/system/all stream (throttled/batched while visible, 7s REST polling
//   while hidden).
// - HomeContent: receives activeId + agents from HomeShell; calls
//   useTimeline + useTokenUsage + usePendingMessages which share that one
//   active-agent SSE connection (isEventForThread remains a defensive gate).

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";

import { AgentSidebar } from "@/components/agent-sidebar";
import { AlertsBadge } from "@/components/alerts-badge";
import { ErrorBoundary } from "@/components/error-boundary";
import { Composer } from "@/components/composer";
import { ContentToggle } from "@/components/content-toggle";
import { HeaderBar } from "@/components/header-bar";
import { HomeLayout } from "@/components/home-layout";
import { InspectorPanel, InspectorToggle } from "@/components/inspector-panel";
import { PendingStrip } from "@/components/pending-strip";
import { UploadButton } from "@/components/upload-button";
import { TimelineView } from "@/components/timeline";
import { api, MessageDeliveryUnknownError } from "@/lib/api";
import { errMsg } from "@/lib/errors";
import { useInspectorOpen } from "@/lib/inspector-panel-store";
import { useBreakpoint } from "@/lib/breakpoint";
import { useSidebarCollapsed } from "@/lib/sidebar";
import { useStore } from "@/lib/store";
import { useTimelineStore } from "@/lib/timeline-store";
import type { AgentRow, ContentBlock } from "@/lib/types";
import { useAgents } from "@/lib/use-agents";
import { usePendingMessages } from "@/lib/use-pending-messages";
import { useTimeline } from "@/lib/use-timeline";
import { timelineMaxWidthCss } from "@/lib/timeline-width";
import { useUserSettings } from "@/lib/use-user-settings";
import { useTokenUsage } from "@/lib/use-token-usage";
import { AgentEventStreamProvider } from "@/lib/useEventStream";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0, OVERFLOW_HIDDEN } from "@/lib/layout";
import { cn } from "@/lib/utils";

export default function HomePage() {
  // Read only client-only state here; the global SSE broadcast is provided at
  // the app root, and the per-active-agent stream lives inside HomeShell below.
  // The toast renderer moved to the app root (ToastHost in Providers) so
  // error toasts reach every route, not just Home.
  const showToast = useStore((s) => s.showToast);

  const showError = useCallback(
    (msg: string) => showToast(msg),
    [showToast],
  );

  return (
    // <main> landmark (Task #1051 a11y): the Home page is the primary
    // surface — screen-reader landmark navigation needs it, and a future
    // skip-link targets #main-content.
    <main id="main-content" className={cn(FLEX, FLEX_1, MIN_H_0)}>
      <HomeShell showError={showError} />
    </main>
  );
}

interface HomeShellProps {
  showError: (msg: string) => void;
}

function HomeShell({ showError }: HomeShellProps) {
  const focusComposer = useStore((s) => s.focusComposer);
  const { isNarrow, isLarge } = useBreakpoint();
  const {
    collapsed: sidebarCollapsed,
    setCollapsed: setSidebarCollapsed,
    isLoading: sidebarSettingsLoading,
  } = useSidebarCollapsed();
  const sidebarEntryResetDone = useRef(false);

  // #723: every home entry starts expanded, but a layout transition is not a
  // new entry. HomeShell stays mounted while HomeLayout switches between the
  // expanded RRP frame and collapsed static frame; AgentSidebar does not.
  // Keeping the reset here prevents that child remount from undoing a user's
  // collapse click. Wait for the persisted settings before consuming the
  // one-time reset so a cold load with collapsed=true is still expanded.
  useEffect(() => {
    if (sidebarSettingsLoading || sidebarEntryResetDone.current) return;
    sidebarEntryResetDone.current = true;
    if (sidebarCollapsed) setSidebarCollapsed(false);
  }, [sidebarCollapsed, sidebarSettingsLoading, setSidebarCollapsed]);

  const {
    agents,
    activeId,
    pendingActions,
    pendingSpawnCount,
    forkPending,
    spawn,
    fork,
    terminate,
    restart,
    resurrect,
    compact,
    isLoading: agentsLoading,
  } = useAgents(showError);

  // User ruling 2026-08-23 supersedes the 2026-08-05 floating desktop
  // overlay: desktop uses a fixed right-side panel and mobile keeps the
  // full-screen overlay. Both start CLOSED and open via the composer's toggle.
  const { open: inspectorOpen } = useInspectorOpen();

  const handleSpawn = useCallback(
    async ({
      machine,
      model,
      preset,
      reasoning_effort,
    }: {
      machine?: string;
      model?: string;
      preset?: string;
      reasoning_effort?: string;
    }) => {
      const newId = await spawn(machine, model, preset, reasoning_effort);
      if (newId !== null) focusComposer();
    },
    [spawn, focusComposer],
  );

  const handleFork = useCallback(async () => {
    if (activeId == null) return;
    const newId = await fork(activeId);
    if (newId !== null) focusComposer();
  }, [activeId, fork, focusComposer]);

  // Preload the open-pages list when an agent is selected (cheap DB read;
  // useAgentPages folds SSE into this cache). The expensive /inspect endpoint
  // is deliberately NOT preloaded here — selecting an agent must not fire a
  // ~25-Loki-aggregation call the user may never look at. InspectorPanel
  // fetches on open instead — see inspector-panel.tsx.
  useQuery({
    queryKey: ["agent-pages", activeId] as const,
    queryFn: () => {
      if (activeId == null) throw new Error("pages preload: activeId is null");
      return api.listPages(activeId);
    },
    enabled: activeId != null,
    staleTime: 10_000,
  });

  const sidebar = (
    <ErrorBoundary>
      <AgentSidebar
        agents={agents}
        isLoading={agentsLoading}
        pendingActions={pendingActions}
        pendingSpawnCount={pendingSpawnCount}
        onSpawn={handleSpawn}
        onTerminate={terminate}
        onRestart={restart}
        onResurrect={resurrect}
        onFork={fork}
        onCompact={compact}
      />
    </ErrorBoundary>
  );
  const main = (
    <AgentEventStreamProvider>
      <HomeContent
        activeId={activeId}
        agents={agents}
        forkPending={forkPending}
        showError={showError}
        handleFork={handleFork}
      />
    </AgentEventStreamProvider>
  );
  const inspector =
    inspectorOpen && activeId != null ? <InspectorPanel agentId={activeId} /> : null;

  return (
    <HomeLayout
      isNarrow={isNarrow}
      isLarge={isLarge}
      sidebarCollapsed={sidebarCollapsed}
      sidebar={sidebar}
      main={main}
      inspector={inspector}
    />
  );
}

interface HomeContentProps {
  activeId: number | null;
  agents: AgentRow[];
  forkPending: boolean;
  showError: (msg: string) => void;
  handleFork: () => void;
}

function HomeContent({
  activeId,
  agents,
  forkPending,
  showError,
  handleFork,
}: HomeContentProps) {
  const t = useTranslations("common");
  const composerFocusToken = useStore((s) => s.composerFocusToken);
  // Timeline content-column width — user-adjustable fraction of the viewport
  // (display.timeline_width_ratio, default 0.4). The composer gets the same
  // width (see lib/timeline-width.ts).
  const { settings: userSettings } = useUserSettings();
  // Task #805 (user ruling): the timeline-width ratio is a DESKTOP concept.
  // Below the md breakpoint (phones — same 768px boundary the sidebar uses
  // for its overlay drawer) the timeline and composer are FULL-WIDTH and the
  // ratio is ignored; the inline maxWidth is simply not applied. Desktop
  // keeps min(<ratio>vw, 1280px).
  const { isNarrow } = useBreakpoint();
  const contentColumnMaxWidth = isNarrow
    ? undefined
    : timelineMaxWidthCss(userSettings["display.timeline_width_ratio"]);
  // #723-⑧: the composer aligns with the timeline column width (the #715
  // 48px-narrower offset was dropped on user request). One falsy convention
  // everywhere (audit C2): undefined = full width on narrow viewports — no
  // inline maxWidth is rendered then, same as contentColumnMaxWidth above.
  const composerMaxWidth = isNarrow
    ? undefined
    : timelineMaxWidthCss(userSettings["display.timeline_width_ratio"]);
  const setMobileSidebarOpen = useStore((s) => s.setMobileSidebarOpen);
  // Single force-scroll signal (timeline-store-owned). Agent switch bumps it
  // inside switchThread; send bumps it here via requestScrollToBottom.
  const requestScrollToBottom = useTimelineStore((s) => s.requestScrollToBottom);

  // File upload — one or more files sent as a single batch (one request, one
  // inbound to the agent). progress is the whole batch's byte progress.
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [uploadCount, setUploadCount] = useState(0);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const uploadHandler = useCallback(
    async (files: File[]) => {
      if (activeId == null || files.length === 0) return;
      setUploading(true);
      setUploadProgress(0);
      setUploadCount(files.length);
      setUploadError(null);
      try {
        await api.uploadFiles(activeId, files, setUploadProgress);
      } catch (e: unknown) {
        setUploadError(errMsg(e));
      } finally {
        setUploading(false);
      }
    },
    [activeId],
  );

  // Agent switch → force scroll to bottom is bumped by the store's
  // switchThread (in the same set() that swaps in the new thread's items),
  // so there is no parent-side switch effect here.

  const { items, streamingCode, turnActive, hasMoreOlder, loadingOlder, loadOlder, isLoading } =
    useTimeline(activeId, showError);
  const { contextTokens, maxContextTokens, softCompactTokens, hardCompactTokens } = useTokenUsage(
    activeId,
    showError,
  );
  const pendingMessages = usePendingMessages(activeId, showError);

  const handleSend = useCallback(
    async (
      content: string,
      imageUrls: string[],
      clientMessageId: string,
    ): Promise<boolean> => {
      if (activeId == null) return false;
      try {
        // One send is one message, whatever was typed. Text invoking several
        // commands (`/plan … /recap`) is sent whole and expanded server-side
        // inside that single inbound, so the agent reads the commands as one
        // composite instruction rather than as unrelated turns.
        if (imageUrls.length === 0) {
          await api.sendMessage(activeId, content, clientMessageId);
        } else {
          // Multimodal: an optional leading text block, then one image_url block
          // per attachment (the backend gates + inlines them for the model).
          const blocks: ContentBlock[] = [];
          if (content) blocks.push({ type: "text", text: content });
          for (const u of imageUrls) blocks.push({ type: "image_url", image_url: { url: u } });
          await api.sendMessage(activeId, blocks, clientMessageId);
        }
        // Scroll to the bottom on every send so the user sees the latest
        // exchange regardless of prior scroll position. This also re-enables
        // sticky-bottom so the agent's streaming reply is followed automatically.
        requestScrollToBottom();
        return true;
      } catch (e: unknown) {
        if (e instanceof MessageDeliveryUnknownError) {
          showError(t("deliveryUnconfirmedToast"));
          throw e;
        }
        showError(`Send failed: ${errMsg(e)}`);
        return false;
      }
    },
    [activeId, showError, requestScrollToBottom, t],
  );

  // Upload one pasted / dropped image silently and hand its reference url back
  // to the composer, which attaches it to the next message as native content.
  const attachImage = useCallback(
    async (file: File): Promise<string> => {
      if (activeId == null) throw new Error("no active agent");
      const batch = await api.uploadFiles(activeId, [file], undefined, false);
      return batch.files[0].url;
    },
    [activeId],
  );

  const handleStop = useCallback(async () => {
    if (activeId == null) return;
    try {
      await api.cancel(activeId);
    } catch (e: unknown) {
      showError(`Cancel failed: ${errMsg(e)}`);
    }
  }, [activeId, showError]);

  const activeAgent = agents.find((a) => a.agent_id === activeId);
  const activeLabel = activeAgent
    ? `Agent #${activeAgent.agent_id}${activeAgent.label ? ` · ${activeAgent.label}` : ""} · ${activeAgent.status.charAt(0).toUpperCase() + activeAgent.status.slice(1)}`
    : "…";
  // "busy" = the agent is mid-turn. turnActive (the SSE turn-in-progress
  // signal) alone is flappy: it flips false at every llm_done — including the
  // MID-turn ones between an LLM call and the exec / next call that follows —
  // so anything keyed on it bounces at every call boundary. status==="running"
  // covers the whole turn including those gaps; the union is the steady
  // signal. Two consumers:
  // - Composer Stop button: available whenever the agent is running, not only
  //   mid-action — Stop INSERTs a durable cancel inbound, so a stop pressed
  //   between actions is still caught at the next claim pass.
  // - TimelineView turnActive: drives the last detail block's auto-expand and
  //   its live "Working for Xs" clock in Last mode; feeding it raw turnActive
  //   made the block collapse+re-expand (and the clock stop/start) at every
  //   mid-turn llm_done — the streaming jitter.
  const agentBusy = turnActive || activeAgent?.status === "running";
  const composerMode: "disabled" | "idle" | "busy" =
    activeId == null ? "disabled" : agentBusy ? "busy" : "idle";

  return (
    <section className={cn("relative", FLEX, MIN_H_0, FLEX_1, MIN_W_0, OVERFLOW_HIDDEN)}>
      <ErrorBoundary>
        {/* Timeline surface — full-bleed scroll container (user ruling
            2026-08-06): the timeline is the main page, so its ScrollArea
            spans the whole pane edge to edge. The scrollbar sits at the
            pane's right edge and the gutters scroll with the wheel. The
            conversation column (cap = display.timeline_width_ratio) lives
            INSIDE the scroll viewport, centered; HeaderBar floats ABOVE the
            surface (absolute + backdrop blur); the composer stack sits in
            normal flow below it (user ruling 2026-08-06 11:35) so a growing
            textarea pushes the timeline up instead of overlapping it.
            #805: on narrow viewports contentColumnMaxWidth is undefined,
            so the column is full-width (mobile). */}
        <div data-testid="timeline-surface" className={cn("relative", FLEX, FLEX_COL, FLEX_1, MIN_H_0, MIN_W_0)}>
          <TimelineView
            items={items}
            threadKey={activeId != null ? String(activeId) : undefined}
            streamingCode={streamingCode}
            turnActive={agentBusy}
            onFork={activeId != null ? handleFork : null}
            forkPending={forkPending}
            hasMoreOlder={hasMoreOlder}
            loadingOlder={loadingOlder}
            onLoadOlder={loadOlder}
            loading={isLoading}
            maxWidthCss={contentColumnMaxWidth}
          />
          <HeaderBar
            label={activeLabel}
            onOpenSidebar={() => setMobileSidebarOpen(true)}
            maxWidthCss={contentColumnMaxWidth}
          >
            {/* Alert system (Task #1224): unresolved badge in the top bar
                jumps to /insights#alerts. */}
            <AlertsBadge />
            <InspectorToggle />
          </HeaderBar>
          {/* Bottom stack: pending strip + composer in normal flow (user
              ruling 2026-08-06 11:35): a growing textarea pushes the timeline
              up instead of overlapping it. The composer's own top divider
              separates it from the timeline content. */}
          <div className="relative z-20 bg-background">
            <PendingStrip items={pendingMessages} maxWidthCss={composerMaxWidth} />
            <Composer
              maxWidthCss={composerMaxWidth}
              mode={composerMode}
              onSend={handleSend}
              onStop={handleStop}
              onUploadFiles={uploadHandler}
              onAttachImage={activeAgent?.supports_vision === true ? attachImage : undefined}
              filesUploading={uploading}
              focusToken={composerFocusToken}
              contextTokens={contextTokens}
              maxContextTokens={maxContextTokens}
              softCompactTokens={softCompactTokens}
              hardCompactTokens={hardCompactTokens}
              agentId={activeId}
              agentTerminated={activeAgent?.status === "terminated"}
              details={<ContentToggle />}
            >
              <UploadButton
                agentId={activeId}
                onUpload={uploadHandler}
                uploading={uploading}
                progress={uploadProgress}
                count={uploadCount}
                error={uploadError}
              />
            </Composer>
          </div>
        </div>
      </ErrorBoundary>
    </section>
  );
}
