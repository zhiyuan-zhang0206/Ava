// Zustand store — pure-client UI preferences + cluster-coordination state.
//
// The SSE-driven streaming timeline used to live here too; it now has its own
// store (`timeline-store.ts`, `useTimelineStore`) so the high-frequency SSE
// fold — one set() per streaming delta — notifies only timeline subscribers,
// not the sidebar / spawn dialog / cluster banner that read this store. The two
// never cross-read: the timeline gate `activeThreadId` (in useTimelineStore)
// and the sidebar selection `activeId` (here) are coordinated only at the hook
// level (useTimeline calls switchThread when activeId changes).
//
// Server data (agents list, stats, timeline snapshot, token usage) lives in
// TanStack Query. Earlier this file mirrored the agents list and lifecycle
// pending flags here too; that mirror produced multi-source races (poll
// vs optimistic vs SSE) and is now gone — sidebar reads agents directly
// via useAgents → useQuery.
//
// Two slices:
//   UI slice: pure-client UI state (activeId, toast, sidebar drawer, search).
//   Cluster slice: SSE-reconnect + cluster-update coordination.
//
// Nothing here is persisted: this is volatile per-session UI state. Durable
// preferences (including the spawn picker's model/preset/effort) are DB rows via
// useUserSettings, not zustand persist.

"use client";

import { create } from "zustand";

// Single dismiss timer for the toast slot (see showToast).
let toastTimer: ReturnType<typeof setTimeout> | null = null;

// =============================================================
// UI Slice
// =============================================================

interface UISlice {
  /** Currently selected agent/thread ID */
  activeId: number | null;
  setActiveId: (id: number | null) => void;

  /** Monotonic token — value change steals focus to the composer textarea */
  composerFocusToken: number;
  focusComposer: () => void;

  /** Mobile sidebar drawer toggle */
  mobileSidebarOpen: boolean;
  setMobileSidebarOpen: (open: boolean) => void;

  /** Mobile inspector overlay toggle — session-scoped, never persisted (see
   *  inspector-panel-store.ts: the inspector is a workspace preference on
   *  desktop but a full-screen overlay on mobile, so its mobile open state is
   *  per-session view state like the sidebar drawer). */
  mobileInspectorOpen: boolean;
  setMobileInspectorOpen: (open: boolean) => void;

  /** toast message — when non-null, shows in the bottom-right; auto-clears after 3s */
  toast: string | null;
  showToast: (msg: string) => void;

  // Spawn picker selections (model / preset / reasoning effort) are DB-backed
  // user preferences now (behavior.spawn_* via useUserSettings), so they sync
  // across frontends — SpawnButton reads/writes them directly, not the store.

  /** Search query for filtering agents in the sidebar by label / ID. */
  searchQuery: string;
  setSearchQuery: (q: string) => void;
}

// =============================================================
// Cluster-coordination Slice
// =============================================================
//
// Single coordination point for SSE resilience. Two independent triggers
// funnel through `bumpReconnect()`:
//   - the heartbeat watchdog in useEventStream (45s with no frame at all =
//     half-dead connection; a proxy hop stayed OPEN but stopped delivering)
//   - the update-done detector in use-cluster-health (cluster paused
//     true -> false = a rollout/restart just finished; the old SSE socket
//     was severed when the gateway bounced)
// Both bump `reconnectNonce`; useEventStream lists it in its effect deps, so
// a bump tears down the stale EventSource and opens a fresh one (whose
// onopen reconciles agents via the existing open handler).

import type { ConnectionState } from "@/lib/use-timeline";

interface ClusterSlice {
  /** Monotonic token — bumping it forces useEventStream to tear down the
   * current EventSource and open a fresh one (it is in the effect deps). */
  reconnectNonce: number;
  /** Bump reconnectNonce — the single entry point for forcing a clean SSE
   * reopen (watchdog half-dead detection + cluster-update-done both call it). */
  bumpReconnect: () => void;

  /** Global SSE connection health, tracked here so any component can read it
   * without subscribing to the EventStream itself. Updated by
   * AppConnectionBanner (mounted at the root). */
  connState: ConnectionState;
  setConnState: (s: ConnectionState) => void;

  /** True while the cluster is paused (an update / rollout / restart is in
   * flight). Drives AuthGuard's decision to show the full-screen UpdatingPage
   * instead of redirecting an unauthenticated session to /login (the auth
   * failure is expected during a rollout, not a real logout). */
  clusterUpdating: boolean;
  setClusterUpdating: (b: boolean) => void;

  /** True when this host is paused but no orchestration is running — a rollout
   * was hard-killed and left the pause/lock behind. Drives AppConnectionBanner's
   * recovery state (offers a manual force-recover), the only banner still
   * rendered at the app root — it needs operator action. */
  clusterStranded: boolean;
  setClusterStranded: (b: boolean) => void;
}

// =============================================================
// Combined Store
// =============================================================

export type Store = UISlice & ClusterSlice;

export const useStore = create<Store>()((set) => ({
  // -- UI defaults --
  activeId: null,
  setActiveId: (id) => set({ activeId: id }),

  composerFocusToken: 0,
  focusComposer: () => set((s) => ({ composerFocusToken: s.composerFocusToken + 1 })),

  mobileSidebarOpen: false,
  setMobileSidebarOpen: (open) => set({ mobileSidebarOpen: open }),

  mobileInspectorOpen: false,
  setMobileInspectorOpen: (open) => set({ mobileInspectorOpen: open }),

  toast: null,
  showToast: (msg) => {
    set({ toast: msg });
    // One 3s dismiss timer at a time — a burst of toasts must not let an
    // older timer clear the newer message early (the old code leaked a
    // timer per toast and the first one fired whenever).
    if (toastTimer !== null) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      toastTimer = null;
      set({ toast: null });
    }, 3000);
  },

  searchQuery: "",
  setSearchQuery: (q) => set({ searchQuery: q }),

  // -- Cluster-coordination defaults --
  reconnectNonce: 0,
  bumpReconnect: () => set((s) => ({ reconnectNonce: s.reconnectNonce + 1 })),

  clusterUpdating: false,
  setClusterUpdating: (b) => set({ clusterUpdating: b }),

  clusterStranded: false,
  setClusterStranded: (b) => set({ clusterStranded: b }),

  connState: "open",
  setConnState: (s) => set({ connState: s }),
}));
