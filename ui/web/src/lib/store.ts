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

import type { OpenTasksHint } from "@/lib/types";

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

  /** Inspector aggregate window. Session-scoped selection for the panel's
   *  cost and activity reads; null = all time. */
  inspectorHours: number | null;
  setInspectorHours: (hours: number | null) => void;

  /** toast message — when non-null, shows in the bottom-right; auto-clears after 3s */
  toast: string | null;
  showToast: (msg: string) => void;

  /** Terminate open-tasks notice (task #3374) — set when a terminate response
   *  reports the agent still owned open tasks; the root-level
   *  OpenTasksNoticeHost renders it. Never auto-clears: dismiss explicitly. */
  openTasksNotice: OpenTasksHint | null;
  showOpenTasksNotice: (notice: OpenTasksHint) => void;
  dismissOpenTasksNotice: () => void;

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
// The update-done detector in use-cluster-health bumps reconnectNonce after
// a gateway bounce. The shared transport asks the leader to replace its
// EventSources; the legacy per-page path reopens its own. The SSE heartbeat
// watchdog also repairs a half-dead socket, directly through the shared
// transport or through bumpReconnect() on the legacy path.

import type { ConnectionState } from "@/lib/use-timeline";

interface ClusterSlice {
  /** Monotonic token requesting fresh system EventSources after a gateway bounce. */
  reconnectNonce: number;
  /** Bump reconnectNonce for the cluster-update and legacy watchdog paths. */
  bumpReconnect: () => void;

  /** Global SSE connection health, tracked here so any component can read it
   * without subscribing to the EventStream itself. Updated by
   * AppConnectionBanner (mounted at the root). */
  connState: ConnectionState;
  setConnState: (s: ConnectionState) => void;


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

  inspectorHours: 24,
  setInspectorHours: (hours) => set({ inspectorHours: hours }),

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

  openTasksNotice: null,
  showOpenTasksNotice: (notice) => set({ openTasksNotice: notice }),
  dismissOpenTasksNotice: () => set({ openTasksNotice: null }),

  searchQuery: "",
  setSearchQuery: (q) => set({ searchQuery: q }),

  // -- Cluster-coordination defaults --
  reconnectNonce: 0,
  bumpReconnect: () => set((s) => ({ reconnectNonce: s.reconnectNonce + 1 })),


  connState: "open",
  setConnState: (s) => set({ connState: s }),
}));
