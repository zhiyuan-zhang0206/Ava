// store.ts direct unit tests — the UI + cluster-coordination store.
//
// The SSE-driven timeline that used to live in this store now has its own
// store (`timeline-store.ts`); its state-machine tests moved to
// timeline-store.test.ts. What remains here is the pure-client UI slice
// (activeId / toast / sidebar / search).

import { act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useStore } from "./store";

/** Reset the store to a fresh UI state — avoids cross-test contamination. */
function resetStore(): void {
  useStore.setState({
    activeId: null,
    composerFocusToken: 0,
    mobileSidebarOpen: false,
    mobileInspectorOpen: false,
    inspectorHours: 24,
    toast: null,
    searchQuery: "",
  });
}

beforeEach(() => {
  resetStore();
});

afterEach(() => {
  vi.useRealTimers();
});

// -- create() initial defaults (fresh module) ──────────────────────────────

describe("create() initial defaults (fresh module)", () => {
  // Verify the initial state set in create() — these mutations are masked by
  // setState after beforeEach; the module must be **freshly imported** to
  // observe (vi.resetModules + dynamic import = fresh singleton).
  it("freshly imported store: all UI + cluster defaults match contract", async () => {
    vi.resetModules();
    const mod = await import("./store");
    const s = mod.useStore.getState();
    expect(s.activeId).toBeNull();
    expect(s.mobileSidebarOpen).toBe(false);
    expect(s.mobileInspectorOpen).toBe(false);
    expect(s.inspectorHours).toBe(24);
    expect(s.composerFocusToken).toBe(0);
    expect(s.toast).toBeNull();
    expect(s.searchQuery).toBe("");
    expect(s.reconnectNonce).toBe(0);
    expect(s.clusterUpdating).toBe(false);
    expect(s.clusterStranded).toBe(false);
  });
});

// -- UI slice setters (focusComposer / showToast / remaining setters) ────────

describe("focusComposer", () => {
  it("composerFocusToken monotonically increases", () => {
    expect(useStore.getState().composerFocusToken).toBe(0);
    act(() => useStore.getState().focusComposer());
    expect(useStore.getState().composerFocusToken).toBe(1);
    act(() => useStore.getState().focusComposer());
    expect(useStore.getState().composerFocusToken).toBe(2);
    act(() => useStore.getState().focusComposer());
    expect(useStore.getState().composerFocusToken).toBe(3);
  });
});

describe("showToast", () => {
  it("writes toast string", () => {
    act(() => useStore.getState().showToast("hello"));
    expect(useStore.getState().toast).toBe("hello");
  });

  it("auto-clears to null after 3000ms", () => {
    vi.useFakeTimers();
    act(() => useStore.getState().showToast("temporary"));
    expect(useStore.getState().toast).toBe("temporary");

    // before timeout → not cleared
    act(() => {
      vi.advanceTimersByTime(2999);
    });
    expect(useStore.getState().toast).toBe("temporary");

    // at 3000ms → cleared
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(useStore.getState().toast).toBeNull();
  });

  it("consecutive showToast: the later toast resets the dismiss timer (Task #1051 — the old first-timer fired early and cleared the newer toast)", () => {
    vi.useFakeTimers();
    act(() => useStore.getState().showToast("first"));
    expect(useStore.getState().toast).toBe("first");
    act(() => useStore.getState().showToast("second"));
    expect(useStore.getState().toast).toBe("second");

    // 2999ms after the SECOND toast: still up (the old timer was cleared).
    act(() => {
      vi.advanceTimersByTime(2999);
    });
    expect(useStore.getState().toast).toBe("second");
    // At 3000ms → cleared.
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(useStore.getState().toast).toBeNull();
  });
});

describe("setMobileSidebarOpen", () => {
  it("writes mobileSidebarOpen", () => {
    act(() => useStore.getState().setMobileSidebarOpen(true));
    expect(useStore.getState().mobileSidebarOpen).toBe(true);
    act(() => useStore.getState().setMobileSidebarOpen(false));
    expect(useStore.getState().mobileSidebarOpen).toBe(false);
  });
});

describe("setMobileInspectorOpen", () => {
  it("writes mobileInspectorOpen", () => {
    act(() => useStore.getState().setMobileInspectorOpen(true));
    expect(useStore.getState().mobileInspectorOpen).toBe(true);
    act(() => useStore.getState().setMobileInspectorOpen(false));
    expect(useStore.getState().mobileInspectorOpen).toBe(false);
  });
});

describe("setInspectorHours", () => {
  it("shares the session window used by panel queries and row prefetch", () => {
    act(() => useStore.getState().setInspectorHours(1));
    expect(useStore.getState().inspectorHours).toBe(1);
    act(() => useStore.getState().setInspectorHours(null));
    expect(useStore.getState().inspectorHours).toBeNull();
  });
});

describe("setActiveId", () => {
  it("writes activeId (both number and null are accepted)", () => {
    act(() => useStore.getState().setActiveId(7));
    expect(useStore.getState().activeId).toBe(7);
    act(() => useStore.getState().setActiveId(null));
    expect(useStore.getState().activeId).toBeNull();
  });
});

describe("setSearchQuery", () => {
  it("writes searchQuery", () => {
    act(() => useStore.getState().setSearchQuery("agent 5"));
    expect(useStore.getState().searchQuery).toBe("agent 5");
    act(() => useStore.getState().setSearchQuery(""));
    expect(useStore.getState().searchQuery).toBe("");
  });
});

// agents / pendingActions / pendingSpawnCount / forkPending used to be
// mirrored in this store via setAgents / setPendingActions / etc. They
// were removed: server state lives in TanStack Query, lifecycle pending
// is derived from mutation isPending inside useAgents, and SpawningRow
// reads pendingSpawnCount through useAgents → props. No store coverage
// needed for those fields anymore.
