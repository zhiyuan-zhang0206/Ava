// agent-sidebar/ component test — DesktopSidebar collapse/expand +
// Mobile drawer open/close + SidebarBody (empty / tree / spawning
// placeholder) + StatsCards tri-state (loading/data/error) + handleSelect
// closes mobile drawer + handleRename calls api.
//
// useSidebarCollapsed / useStatsDashboard / useStore are mocked as stubs so
// the test controls Zustand state. AgentRow / ScrollArea / Button are
// simplified stubs to reduce noise. buildAgentTree is real — it's a pure
// helper with its own tests.

/* eslint-disable @typescript-eslint/no-unsafe-return */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BAR_DIVIDER_CLASS, BAR_HEIGHT_CLASS } from "@/lib/layout";
import { SIDEBAR_SORT_DEFAULT, type StatsWindowHours } from "@/lib/sidebar";
import type * as SidebarModule from "@/lib/sidebar";
import type { AgentRow, OpenNotice, StatsDashboard } from "@/lib/types";

// -- Hoisted state for mocks --
const state = {
  agents: [] as AgentRow[],
  activeId: null as number | null,
  pendingActions: {} as Record<number, "restarting" | "terminating" | "resurrecting">,
  pendingSpawnCount: 0,
  mobileSidebarOpen: false,
  sidebarCollapsed: false,
  stats: undefined as StatsDashboard | undefined,
  statsError: null as unknown,
  statsFetching: false,
  statsRefetch: vi.fn(),
  statsWindowHours: 24 as StatsWindowHours,
  showTerminated: false,
  sidebarViewMode: "tree" as "tree" | "flat",
  searchQuery: "",
  userSettings: {} as Record<string, unknown>,
  setSetting: vi.fn(),
  setActiveId: vi.fn(),
  setMobileSidebarOpen: vi.fn(),
  setSidebarCollapsed: vi.fn(),
  setStatsWindowHours: vi.fn(),
  setSidebarViewMode: vi.fn(),
  setSearchQuery: vi.fn(),
};

const pushSpy = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushSpy }),
}));

vi.mock("@/lib/store", () => ({
  useStore: <T,>(selector: (s: unknown) => T): T => {
    // agents / pendingActions / pendingSpawnCount used to live in the
    // store; they now flow through props (HomeShell → AgentSidebar).
    // Keep them off the fake store so a stray reference here would
    // surface as undefined rather than silently fall back to mirrored state.
    const fake = {
      activeId: state.activeId,
      setActiveId: state.setActiveId,
      mobileSidebarOpen: state.mobileSidebarOpen,
      setMobileSidebarOpen: state.setMobileSidebarOpen,
      searchQuery: state.searchQuery,
      setSearchQuery: state.setSearchQuery,
    };
    return selector(fake);
  },
}));

vi.mock("@/lib/sidebar", async () => {
  const actual = await vi.importActual<typeof SidebarModule>("@/lib/sidebar");
  const React = await import("react");
  return {
    ...actual,
    useSidebarCollapsed: () => ({
      collapsed: state.sidebarCollapsed,
      setCollapsed: state.setSidebarCollapsed,
    }),
    useStatsDashboard: () => ({
      stats: state.stats,
      error: state.statsError,
      isFetching: state.statsFetching,
      refetch: state.statsRefetch,
    }),
    useStatsWindow: () => ({
      windowHours: state.statsWindowHours,
      setWindowHours: state.setStatsWindowHours,
    }),
    useSidebarViewMode: () => ({
      viewMode: state.sidebarViewMode,
      setViewMode: state.setSidebarViewMode,
    }),
    // Sort is now DB-backed; its persistence is covered in lib/sidebar.test.ts.
    // Here we only exercise the sort-bar UI (reorder on click), so back it with
    // plain local state — reactive, no persistence to reason about.
    useSidebarSort: () => {
      const [sort, setSort] = React.useState(actual.SIDEBAR_SORT_DEFAULT);
      return { sort, setSort };
    },
  };
});

vi.mock("@/lib/api", () => ({
  api: {
    patchAgentLabel: vi.fn().mockResolvedValue(undefined),
  },
}));

// happy-dom lacks localStorage on some platforms; provide a deterministic
// in-memory one so any incidental storage access stays isolated per test.
function installLocalStoragePolyfill(): void {
  const store = new Map<string, string>();
  const fake: Storage = {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (k) => store.get(k) ?? null,
    setItem: (k, v) => store.set(k, v),
    removeItem: (k) => store.delete(k),
    key: (i) => Array.from(store.keys())[i] ?? null,
  };
  Object.defineProperty(globalThis, "localStorage", {
    value: fake,
    writable: true,
    configurable: true,
  });
}

// User settings stub — `state.userSettings` plays the merged settings map
// (empty = every opt-in signal at its quiet default). `display.show_terminated`
// is now DB-backed too (moved off localStorage), so it flows through this same
// map; `state.showTerminated` injects it for the terminated-toggle tests, and
// an explicit key in `state.userSettings` still wins.
vi.mock("@/lib/use-user-settings", () => ({
  useUserSettings: () => ({
    settings: { "display.show_terminated": state.showTerminated, ...state.userSettings },
    setSetting: state.setSetting,
    isLoading: false,
  }),
}));

// AgentRow stub — renders data-testid="row-<id>" + active/pending data
// attrs, and exposes onSelect / onTerminate / onRestart / onResurrect /
// onRename invocations for tests.
vi.mock("@/components/agent-row", () => ({
  AgentRow: ({
    agent,
    active,
    pending,
    wide,
    depth,
    onSelect,
    onTerminate,
    onRestart,
    onResurrect,
    onFork,
    onRename,
  }: {
    agent: AgentRow;
    active: boolean;
    pending: string | undefined;
    wide: boolean;
    depth: number;
    onSelect: () => void;
    onTerminate: () => void;
    onRestart: () => void;
    onResurrect: (prompt?: string) => void;
    onFork: (prompt?: string) => void;
    onRename: (l: string) => void;
    onCompact: () => void;
  }) => (
    <li
      data-testid={`row-${agent.agent_id}`}
      data-active={active ? "1" : "0"}
      data-pending={pending ?? ""}
      data-wide={wide ? "1" : "0"}
      data-depth={depth}
    >
      <button data-testid={`select-${agent.agent_id}`} onClick={onSelect}>{agent.label ?? `#${agent.agent_id}`}</button>
      <button data-testid={`terminate-${agent.agent_id}`} onClick={onTerminate}>x</button>
      <button data-testid={`restart-${agent.agent_id}`} onClick={onRestart}>r</button>
      {/* mirror the real row's quick buttons: resurrect is a bare event, no prompt */}
      <button data-testid={`resurrect-${agent.agent_id}`} onClick={() => onResurrect()}>R</button>
      <button data-testid={`fork-${agent.agent_id}`} onClick={() => onFork()}>f</button>
      <button data-testid={`rename-${agent.agent_id}`} onClick={() => onRename("new-label")}>rn</button>
    </li>
  ),
}));

// SpawnButton stub — renders a simple button that calls onSpawn(); the
// cross-machine picker is covered separately by spawn-button.test.tsx.
// Keeps aria-label="Spawn agent" so existing sidebar tests (click Spawn
// → onSpawn called) keep working.
vi.mock("@/components/spawn-button", () => ({
  SpawnButton: ({ onSpawn }: { onSpawn: (machine?: string) => void }) => (
    <button aria-label="Spawn agent" onClick={() => onSpawn()}>
      Spawn
    </button>
  ),
}));

vi.mock("@/components/ui/scroll-area", () => ({
  ScrollArea: ({ children, className }: { children: React.ReactNode; className?: string }) => (
    <div data-testid="scroll-area" className={className}>
      {children}
    </div>
  ),
}));

// R4 layer 4: AgentSidebar mounts the desktop rail vs the mobile drawer by
// breakpoint (single source — useBreakpoint). Default to desktop; the
// mobile-drawer tests flip the mock.
const { bpMock } = vi.hoisted(() => ({
  bpMock: vi.fn(() => ({ tier: "xl", isNarrow: false, isLarge: true })),
}));
vi.mock("@/lib/breakpoint", () => ({ useBreakpoint: () => bpMock() }));

vi.mock("@/components/ui/button", () => ({
  Button: ({
    children,
    onClick,
    "aria-label": ariaLabel,
  }: {
    children: React.ReactNode;
    onClick?: () => void;
    "aria-label"?: string;
    size?: string;
    variant?: string;
  }) => (
    <button onClick={onClick} aria-label={ariaLabel}>
      {children}
    </button>
  ),
}));

import { AgentSidebar } from "./agent-sidebar";
import { api } from "@/lib/api";

afterEach(cleanup);

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function makeAgent(overrides: Partial<AgentRow>): AgentRow {
  return {
    agent_id: 1,
    spawner: "user",
    fork_source_agent_id: null,
    status: "idling",
    pid: 100,
    spawned_at: "2026-05-15T00:00:00Z",
    started_at: "2026-05-15T00:00:00Z",
    last_active_at: "2026-05-15T00:00:00Z",
    last_inbound_at: "2026-05-15T00:00:00Z",
    label: null,
    machine: "test",
    supports_vision: true,
    notices_awaiting_response: [], unread_notice_count: 0,
    heartbeat_paused_until: null,
    liveness_state: "online",
    ...overrides,
  };
}

const handlerFns = {
  onSpawn: vi.fn(),
  onTerminate: vi.fn(),
  onRestart: vi.fn(),
  onResurrect: vi.fn(),
  onFork: vi.fn(),
  onCompact: vi.fn(),
};

// Defaults that source the now-required server-state props from the
// shared mutable `state` object — every <AgentSidebar {...handlers}>
// call picks them up automatically, mirroring real HomeShell wiring.
const handlers = new Proxy(handlerFns, {
  get(target, prop, receiver) {
    if (prop === "agents") return state.agents;
    if (prop === "pendingActions") return state.pendingActions;
    if (prop === "pendingSpawnCount") return state.pendingSpawnCount;
    if (prop === "isLoading") return false;
    return Reflect.get(target, prop, receiver);
  },
  ownKeys() {
    return [
      ...Object.keys(handlerFns),
      "agents",
      "pendingActions",
      "pendingSpawnCount",
      "isLoading",
    ];
  },
  getOwnPropertyDescriptor() {
    return { enumerable: true, configurable: true };
  },
}) as typeof handlerFns & {
  agents: AgentRow[];
  pendingActions: Record<number, "restarting" | "terminating" | "resurrecting">;
  pendingSpawnCount: number;
  isLoading: boolean;
};

beforeEach(() => {
  pushSpy.mockReset();
  vi.clearAllMocks();
  // Fresh, empty storage per test so nothing leaks across tests.
  installLocalStoragePolyfill();
  state.agents = [];
  state.activeId = null;
  state.pendingActions = {};
  state.pendingSpawnCount = 0;
  state.mobileSidebarOpen = false;
  state.sidebarCollapsed = false;
  state.stats = undefined;
  state.statsError = null;
  state.statsFetching = false;
  state.statsRefetch = vi.fn();
  state.statsWindowHours = 24;
  state.showTerminated = false;
  state.sidebarViewMode = "tree";
  state.searchQuery = "";
  state.userSettings = {};
  bpMock.mockReturnValue({ tier: "xl", isNarrow: false, isLarge: true });
});

describe("DesktopSidebar collapse/expand", () => {
  it("collapsed=true → blank rail: expand button only, no agent list / spawn / dots", () => {
    state.sidebarCollapsed = true;
    state.pendingSpawnCount = 1;
    state.agents = [
      makeAgent({ agent_id: 1, status: "idling" }),
      makeAgent({ agent_id: 2, status: "running" }),
    ];
    const { container } = wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByLabelText("Expand sidebar")).toBeTruthy();
    // Completely quiet: no mini agent list, no spawn button, no status dots,
    // no spinners — nothing that could leak dynamic change while collapsed.
    expect(screen.queryByText("#1")).toBeNull();
    expect(screen.queryByText("#2")).toBeNull();
    expect(screen.queryByLabelText("Spawn agent")).toBeNull();
    expect(container.querySelectorAll(".animate-spin").length).toBe(0);
    expect(container.querySelectorAll(".animate-pulse").length).toBe(0);
  });

  it("collapsed=false → shows ChevronLeft + 'Ava' title", () => {
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByLabelText("Collapse sidebar")).toBeTruthy();
    expect(screen.getByText("Ava")).toBeTruthy();
    expect(screen.queryByLabelText("Drag to resize width")).toBeNull();
  });

  it("collapsed rail fills the resizable panel frame", () => {
    state.sidebarCollapsed = true;
    const { container } = wrap(<AgentSidebar {...handlers} />);
    const rail = screen.getByLabelText("Expand sidebar").closest("aside");
    expect(rail?.className).toContain("w-full");
    // no unbounded/scrollable overflow inside the collapsed rail
    expect(container.querySelectorAll("aside").length).toBeGreaterThan(0);
  });

  it("leaves the vertical separator to the owning layout handle", () => {
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByText("Ava").closest("aside")?.className).not.toContain("border-r");
    cleanup();

    state.sidebarCollapsed = true;
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByLabelText("Expand sidebar").closest("aside")?.className).not.toContain(
      "border-r",
    );
  });

  it("expanded header and collapsed-rail toggle share the same BAR_HEIGHT_CLASS", () => {
    wrap(<AgentSidebar {...handlers} />);
    const expandedHeader = screen.getByText("Ava").closest("header");
    expect(expandedHeader?.className).toContain(BAR_HEIGHT_CLASS);
    cleanup();

    state.sidebarCollapsed = true;
    wrap(<AgentSidebar {...handlers} />);
    // The rail's BAR_HEIGHT_CLASS moved to the buttons' flex-row container
    // (each button is flex-1 inside it, #723).
    const collapsedToggle = screen.getByLabelText("Expand sidebar");
    expect(collapsedToggle.parentElement?.className).toContain(BAR_HEIGHT_CLASS);
  });

  it("insets the expanded title divider", () => {
    wrap(<AgentSidebar {...handlers} />);
    const header = screen.getByText("Ava").closest("header");
    for (const dividerClass of BAR_DIVIDER_CLASS.split(" ")) {
      expect(header?.className).toContain(dividerClass);
    }
    expect(header?.className).not.toContain("border-b");
  });

  it("expanded sidebar aside clips horizontal overflow (overflow-x-hidden backstop)", () => {
    wrap(<AgentSidebar {...handlers} />);
    const aside = screen.getByText("Ava").closest("aside");
    expect(aside?.className).toContain("overflow-x-hidden");
  });

  it("expanded sidebar aside is a flex column (Task #1053 regression: R4-PR3 dropped display:flex, so the agent-list ScrollArea stopped bounding and the whole aside — header/bars/fixed footer — scrolled together)", () => {
    wrap(<AgentSidebar {...handlers} />);
    const aside = screen.getByText("Ava").closest("aside");
    expect(aside?.className).toContain("flex");
    expect(aside?.className).toContain("flex-col");
  });

  it("click collapsed expand button → setCollapsed(false)", () => {
    state.sidebarCollapsed = true;
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByLabelText("Expand sidebar"));
    expect(state.setSidebarCollapsed).toHaveBeenCalledWith(false);
  });

  it("click expanded collapse button → setCollapsed(true)", () => {
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByLabelText("Collapse sidebar"));
    expect(state.setSidebarCollapsed).toHaveBeenCalledWith(true);
  });

  it("click Spawn (expanded header) → onSpawn is called", () => {
    wrap(<AgentSidebar {...handlers} />);
    // The expanded SidebarHeader also exposes the Spawn aria-label
    fireEvent.click(screen.getByLabelText("Spawn agent"));
    expect(handlers.onSpawn).toHaveBeenCalled();
  });

  it("expanded desktop sidebar fills the resizable panel frame", () => {
    const { container } = wrap(<AgentSidebar {...handlers} />);
    const aside = container.querySelector<HTMLElement>("aside")!;
    expect(aside).toBeTruthy();
    expect(aside.className).toContain("w-full");
    expect(aside.style.width).toBe("");
  });
});

describe("MobileSidebar open/close", () => {
  beforeEach(() => {
    // Narrow viewport — the mobile drawer is the mounted surface.
    bpMock.mockReturnValue({ tier: "xs", isNarrow: true, isLarge: false });
  });

  it("mobileOpen=false → not rendered (null)", () => {
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.queryByLabelText("Close sidebar")).toBeNull();
  });

  it("mobileOpen=true → renders overlay + close button", () => {
    state.mobileSidebarOpen = true;
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByLabelText("Close sidebar")).toBeTruthy();
  });

  it("click close X → setMobileSidebarOpen(false)", () => {
    state.mobileSidebarOpen = true;
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByLabelText("Close sidebar"));
    expect(state.setMobileSidebarOpen).toHaveBeenCalledWith(false);
  });
});

describe("SidebarBody empty / tree / spawning placeholder", () => {
  it("agents empty + no pending → empty state (no ul)", () => {
    wrap(<AgentSidebar {...handlers} />);
    // Empty state renders no ul / row
    expect(screen.queryByTestId("row-1")).toBeNull();
  });

  it("agents non-empty → renders tree (one row per agent)", () => {
    state.agents = [
      makeAgent({ agent_id: 1, label: "first" }),
      makeAgent({ agent_id: 2, label: "second" }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    // Desktop mock: the rail is the only mounted surface.
    expect(screen.getByTestId("row-1")).toBeTruthy();
    expect(screen.getByTestId("row-2")).toBeTruthy();
  });

  it("pendingSpawnCount=2 → renders 2 SpawningRows, quiet by default (no motion)", () => {
    state.pendingSpawnCount = 2;
    const { container } = wrap(<AgentSidebar {...handlers} />);
    // The placeholder rows render (direct feedback to the user's spawn click)…
    expect(screen.getAllByTestId("spawning-row").length).toBe(2);
    // …but with status colors at their quiet default, no spinner / pulse leaks.
    expect(container.querySelectorAll(".animate-spin").length).toBe(0);
    expect(container.querySelectorAll(".animate-pulse").length).toBe(0);
  });

  it("pendingSpawnCount + status colors enabled → SpawningRows animate", () => {
    state.userSettings = { "display.show_agent_status": true };
    state.pendingSpawnCount = 2;
    const { container } = wrap(<AgentSidebar {...handlers} />);
    expect(container.querySelectorAll(".animate-spin").length).toBeGreaterThanOrEqual(2);
    expect(container.querySelectorAll(".animate-pulse").length).toBeGreaterThanOrEqual(2);
  });

  it("has pending → activeId masked to null, real rows lose active highlight", () => {
    state.agents = [makeAgent({ agent_id: 5 })];
    state.activeId = 5;
    state.pendingSpawnCount = 1;
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByTestId("row-5").getAttribute("data-active")).toBe("0");
  });

  it("no pending + activeId=5 → row-5 active=1", () => {
    state.agents = [makeAgent({ agent_id: 5 })];
    state.activeId = 5;
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByTestId("row-5").getAttribute("data-active")).toBe("1");
  });

  it("agent in pendingActions → row receives pending prop", () => {
    state.agents = [makeAgent({ agent_id: 7 })];
    state.pendingActions = { 7: "terminating" };
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByTestId("row-7").getAttribute("data-pending")).toBe("terminating");
  });
});

describe("wide mode + fork propagation", () => {
  it("desktop rows render the full monitoring presentation", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByTestId("row-1").getAttribute("data-wide")).toBe("1");
  });

  it("fork child nests under its source in the tree", () => {
    state.agents = [
      makeAgent({ agent_id: 1, spawner: "user" }),
      makeAgent({ agent_id: 2, spawner: "user", fork_source_agent_id: 1 }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    // The fork (no longer badged) still nests one level under its source.
    expect(screen.getByTestId("row-1").getAttribute("data-depth")).toBe("0");
    expect(screen.getByTestId("row-2").getAttribute("data-depth")).toBe("1");
  });

  it("mobile drawer renders wide regardless of sidebar width", () => {
    bpMock.mockReturnValue({ tier: "xs", isNarrow: true, isLarge: false });
    state.mobileSidebarOpen = true;
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    // Narrow viewport: only the mobile drawer mounts; its rows are wide.
    const row = screen.getByTestId("row-1");
    expect(row.getAttribute("data-wide")).toBe("1");
  });
});

describe("handleSelect and handleRename forwarding", () => {
  it("click row select → setActiveId(id) + setMobileSidebarOpen(false)", () => {
    state.agents = [makeAgent({ agent_id: 3 })];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByTestId("select-3"));
    expect(state.setActiveId).toHaveBeenCalledWith(3);
    expect(state.setMobileSidebarOpen).toHaveBeenCalledWith(false);
  });

  it("click row terminate → onTerminate prop called with agent id", () => {
    state.agents = [makeAgent({ agent_id: 4 })];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByTestId("terminate-4"));
    expect(handlers.onTerminate).toHaveBeenCalledWith(4);
  });

  it("click row restart/resurrect/fork → corresponding handler called with id", () => {
    state.agents = [makeAgent({ agent_id: 4 })];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByTestId("restart-4"));
    expect(handlers.onRestart).toHaveBeenCalledWith(4);
    // quick resurrect is a pure lifecycle event — id only, no prompt.
    fireEvent.click(screen.getByTestId("resurrect-4"));
    expect(handlers.onResurrect).toHaveBeenCalledWith(4, undefined);
    fireEvent.click(screen.getByTestId("fork-4"));
    expect(handlers.onFork).toHaveBeenCalledWith(4, undefined);
  });

  it("click row rename → api.patchAgentLabel(id, label) called", async () => {
    state.agents = [makeAgent({ agent_id: 9 })];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByTestId("rename-9"));
    await waitFor(() => expect(api.patchAgentLabel).toHaveBeenCalledWith(9, "new-label"));
  });
});

// Stats live in the sidebar-footer popover now (user ruling 2026-08-05):
// tests open it via the chart icon before asserting on the cards.
function openStats() {
  fireEvent.click(screen.getByLabelText("Statistics"));
}

describe("StatsCards tri-state (loading / data / error)", () => {
  it("stats=undefined + error=null → 6 cards show '—' placeholder", () => {
    state.agents = [makeAgent({ agent_id: 1 })]; // make the body render
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(6);
    expect(screen.getByText("Live agents")).toBeTruthy();
    expect(screen.getByText("Tokens")).toBeTruthy();
    expect(screen.getByText("Cache hit")).toBeTruthy();
    expect(screen.getByText("Cost")).toBeTruthy();
    expect(screen.getByText("Average turn time")).toBeTruthy();
    expect(screen.getByText("Warnings / errors")).toBeTruthy();
  });

  it("fetching without data shows an updating spinner and six skeleton values", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.statsFetching = true;
    wrap(<AgentSidebar {...handlers} />);
    openStats();

    expect(screen.getByRole("status", { name: "Statistics are updating" })).toBeTruthy();
    expect(document.querySelectorAll(".animate-pulse")).toHaveLength(6);
    expect(screen.queryByText("—")).toBeNull();
  });

  it("stats has data → 6 cards show real numbers", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = {
      live_count: 5,
      window_hours: 24,
      tokens: { input: 12_345, output: 6_789, cache_read: 0, cache_hit_pct: 80 },
      cost_usd: 1.2345,
      avg_turn_seconds: 3.14,
      warnings: 2,
      errors: 1,
      warnings_dismissed: 1,
      warnings_net: 1,
      errors_dismissed: 1,
      errors_net: 0,
      total_events: 100,
    };
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    expect(screen.getByText("5")).toBeTruthy(); // live
    expect(screen.getByText("19.1k")).toBeTruthy(); // tokens compact (12345+6789=19134)
    expect(screen.getByText("80.00%")).toBeTruthy(); // cache hit, 2 decimals
    expect(screen.getByText("$1.23")).toBeTruthy(); // windowed cost
    expect(screen.getByText("3s")).toBeTruthy(); // avg turn
    // warnings/errors card: one unresolved number per level rendered as a
    // single "N / M" value in the original 2x3 card layout (restored per user
    // feedback 2026-08-30); zero levels render as plain 0, so warnings_net=1
    // with errors_net=0 shows the bare "1 / 0" value
    expect(screen.getByText("1 / 0")).toBeTruthy();
    // no Total / Resolved / Remaining labels anywhere in the card
    expect(screen.queryByText(/Total/)).toBeNull();
    expect(screen.queryByText(/Resolved/)).toBeNull();
    expect(screen.queryByText(/Remaining/)).toBeNull();
  });

  it("both levels fully resolved → plain '0 / 0', no all-clear badge", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = {
      live_count: 5,
      window_hours: 24,
      tokens: { input: 12_345, output: 6_789, cache_read: 0, cache_hit_pct: 80 },
      cost_usd: 1.2345,
      avg_turn_seconds: 3.14,
      warnings: 3,
      errors: 2,
      warnings_dismissed: 3,
      warnings_net: 0,
      errors_dismissed: 2,
      errors_net: 0,
      total_events: 100,
    };
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    // user ruling 2026-08-30: zero levels render as plain 0, no all-clear badge
    expect(screen.getByText("0 / 0")).toBeTruthy();
    expect(screen.queryByText("All clear")).toBeNull();
  });

  it("error without data shows retry and clicking it refetches", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.statsError = new Error("stats endpoint 500");
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    expect(screen.getAllByText("!").length).toBeGreaterThanOrEqual(6);
    // The popover renders through a portal, so query the document, not the
    // wrapper container.
    expect(document.querySelectorAll(".text-destructive").length).toBeGreaterThan(0);
    const retry = screen.getByRole("button", { name: "Retry" });
    expect(retry.getAttribute("title")).toBe("stats endpoint 500");
    fireEvent.click(retry);
    expect(state.statsRefetch).toHaveBeenCalledOnce();
  });

  it("refetch failure keeps stale values and labels them with a warning", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = {
      live_count: 5,
      window_hours: 24,
      tokens: { input: 100, output: 50, cache_read: 0, cache_hit_pct: 80 },
      cost_usd: 1,
      avg_turn_seconds: 3,
      warnings: 2,
      errors: 1,
      warnings_dismissed: 1,
      warnings_net: 1,
      errors_dismissed: 1,
      errors_net: 0,
      total_events: 100,
    };
    state.statsError = new Error("stats endpoint 500");
    wrap(<AgentSidebar {...handlers} />);
    openStats();

    expect(screen.getByText("5")).toBeTruthy();
    expect(screen.queryByText("!")).toBeNull();
    expect(screen.getByTitle("stats endpoint 500")).toBeTruthy();
  });

  it("tokens compact: < 1000 raw, < 1M as k, >= 1M as M", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = {
      live_count: 0,
      window_hours: 24,
      tokens: { input: 1_500_000, output: 0, cache_read: 0, cache_hit_pct: 0 },
      cost_usd: 0,
      avg_turn_seconds: null,
      warnings: 0,
      errors: 0,
      warnings_dismissed: 0,
      warnings_net: 0,
      errors_dismissed: 0,
      errors_net: 0,
      total_events: 0,
    };
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    expect(screen.getByText("1.5M")).toBeTruthy();
  });

  it("avg_turn_seconds=null → shows placeholder, not 'NaNs'", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = {
      live_count: 0,
      window_hours: 24,
      tokens: { input: 100, output: 50, cache_read: 0, cache_hit_pct: 0 },
      cost_usd: 0,
      avg_turn_seconds: null,
      warnings: 0,
      errors: 0,
      warnings_dismissed: 0,
      warnings_net: 0,
      errors_dismissed: 0,
      errors_net: 0,
      total_events: 0,
    };
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    expect(screen.queryByText(/NaN/)).toBeNull();
  });
});

describe("StatsCards window selector", () => {
  it("hides stale windowed values while retaining the live-agent count", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.statsWindowHours = 1;
    state.stats = {
      live_count: 5,
      window_hours: 24,
      tokens: { input: 12_345, output: 6_789, cache_read: 0, cache_hit_pct: 80 },
      cost_usd: 1.2345,
      avg_turn_seconds: 3.14,
      warnings: 2,
      errors: 1,
      warnings_dismissed: 1,
      warnings_net: 1,
      errors_dismissed: 1,
      errors_net: 0,
      total_events: 100,
    };

    wrap(<AgentSidebar {...handlers} />);
    openStats();

    expect(screen.getByText("5")).toBeTruthy();
    // all five windowed cards (incl. the three-way W/E card) hide stale totals
    expect(screen.getAllByText("…")).toHaveLength(5);
    expect(screen.queryByText("19.1k")).toBeNull();
    expect(screen.queryByText("80.00%")).toBeNull();
    expect(screen.queryByText("$1.23")).toBeNull();
    expect(screen.queryByText("3s")).toBeNull();
    expect(screen.queryByText("Total 2")).toBeNull();
    expect(screen.getAllByTitle("Updating for 1h…")).toHaveLength(5);
  });

  it("renders all six window options with the persisted value selected", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.statsWindowHours = 72;
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    const select = screen.getByLabelText<HTMLSelectElement>("Statistics window");
    expect(select.value).toBe("72");
    const labels = Array.from(select.options).map((o) => o.text);
    expect(labels).toEqual(["5m", "1h", "6h", "24h", "3d", "7d"]);
  });

  it("changing the select calls setWindowHours with the numeric window value", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    const select = screen.getByLabelText("Statistics window");
    fireEvent.change(select, { target: { value: "6" } });
    expect(state.setStatsWindowHours).toHaveBeenCalledWith(6);
  });

  it("card labels render for each stat", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.statsWindowHours = 1;
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    expect(screen.getByText("Warnings / errors")).toBeTruthy();
    expect(screen.getByText("Live agents")).toBeTruthy();
    expect(screen.getByText("Cost")).toBeTruthy();
  });
});

describe("Tree render depth (sub-agent indent)", () => {
  it("forked agent → renders indented under its spawner (depth=1)", () => {
    state.agents = [
      makeAgent({ agent_id: 1, spawner: "user" }),
      // agent 2 is a fork of agent 1 — buildAgentTree places it as a child of agent 1
      makeAgent({ agent_id: 2, spawner: "agent:1", fork_source_agent_id: 1 }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByTestId("row-1").getAttribute("data-depth")).toBe("0");
    expect(screen.getByTestId("row-2").getAttribute("data-depth")).toBe("1");
  });
});

describe("terminated toggle", () => {
  it("no terminated agents → no toggle button", () => {
    state.agents = [makeAgent({ agent_id: 1, status: "idling" })];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.queryByLabelText("Show terminated agents")).toBeNull();
    expect(screen.queryByLabelText("Hide terminated agents")).toBeNull();
  });

  it("showTerminated=false → toggle shows no count, terminated rows hidden, live row stays", () => {
    state.agents = [
      makeAgent({ agent_id: 1, status: "idling" }),
      makeAgent({ agent_id: 2, status: "terminated" }),
      makeAgent({ agent_id: 3, status: "terminated" }),
    ];
    state.showTerminated = false;
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByText("Show terminated")).toBeTruthy();
    expect(screen.getByTestId("row-1")).toBeTruthy();
    expect(screen.queryByTestId("row-2")).toBeNull();
    expect(screen.queryByTestId("row-3")).toBeNull();
  });

  it("showTerminated=true → terminated rows rendered, toggle shows count + hide", () => {
    state.agents = [
      makeAgent({ agent_id: 1, status: "idling" }),
      makeAgent({ agent_id: 2, status: "terminated" }),
    ];
    state.showTerminated = true;
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByText("Hide 1 terminated")).toBeTruthy();
    expect(screen.getByTestId("row-2")).toBeTruthy();
  });

  it("malformed persisted setting stays opt-out", () => {
    state.agents = [
      makeAgent({ agent_id: 1, status: "idling" }),
      makeAgent({ agent_id: 2, status: "terminated" }),
    ];
    state.userSettings["display.show_terminated"] = "true";
    wrap(<AgentSidebar {...handlers} />);

    expect(screen.getByText("Show terminated")).toBeTruthy();
    expect(screen.queryByTestId("row-2")).toBeNull();
  });

  it("all agents terminated + hidden → toggle still shows so they can be revealed", () => {
    state.agents = [makeAgent({ agent_id: 2, status: "terminated" })];
    state.showTerminated = false;
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByText("Show terminated")).toBeTruthy();
    expect(screen.queryByTestId("row-2")).toBeNull();
  });

  it("click toggle → setSetting(display.show_terminated, !showTerminated)", () => {
    state.agents = [makeAgent({ agent_id: 2, status: "terminated" })];
    state.showTerminated = false;
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByLabelText("Show terminated agents"));
    expect(state.setSetting).toHaveBeenCalledWith("display.show_terminated", true);
  });

  it("toggle button carries no title tooltip (aria-label covers a11y)", () => {
    state.agents = [makeAgent({ agent_id: 2, status: "terminated" })];
    state.showTerminated = false;
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByLabelText("Show terminated agents").getAttribute("title")).toBeNull();
  });
});

describe("agent tree: live child of a terminated parent mounts on the nearest visible ancestor (#312 regression)", () => {
  // Real roster shape behind the #312 orphan report: 228 (alive, user) ->
  // 240 (terminated) -> 312 (alive, spawned by 240). The roster prop always
  // carries the terminated 240 row (lineage joint), so with terminated
  // hidden 312 must nest under 228 — never surface as a depth-0 orphan root.
  const lineageRoster = () => [
    makeAgent({ agent_id: 228, spawner: "user", status: "idling" }),
    makeAgent({ agent_id: 240, spawner: "agent:228", status: "terminated" }),
    makeAgent({ agent_id: 312, spawner: "agent:240", status: "idling" }),
  ];

  it("terminated hidden (default): 312 nests under 228, no orphan root, 240 row hidden", () => {
    state.showTerminated = false;
    state.agents = lineageRoster();
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByTestId("row-228").getAttribute("data-depth")).toBe("0");
    expect(screen.getByTestId("row-312").getAttribute("data-depth")).toBe("1");
    expect(screen.queryByTestId("row-240")).toBeNull();
  });

  it("terminated shown: 312 keeps its true lineage position under 240", () => {
    state.showTerminated = true;
    state.agents = lineageRoster();
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByTestId("row-228").getAttribute("data-depth")).toBe("0");
    expect(screen.getByTestId("row-240").getAttribute("data-depth")).toBe("1");
    expect(screen.getByTestId("row-312").getAttribute("data-depth")).toBe("2");
  });
});

// The legacy localStorage → DB migration for display.show_terminated (and every
// other preference key) now lives in one place — lib/settings-migration.ts —
// and is covered by settings-migration.test.ts, not here.

describe("Flat view mode", () => {
  it("renders the sort icon button (collapsed to a single icon, #723r2)", () => {
    state.sidebarViewMode = "flat";
    state.agents = [makeAgent({ agent_id: 1 }), makeAgent({ agent_id: 2 })];
    wrap(<AgentSidebar {...handlers} />);
    // Toolbar no longer has a "Sort:" label or 3 inline key buttons - only one icon button
    expect(screen.queryByText("Sort:")).toBeNull();
    expect(screen.queryByText("active")).toBeNull();
    expect(screen.getByLabelText("Sort by id descending")).toBeTruthy();
  });

  it("sort popover: opening shows the three keys; clicking the active key flips direction", () => {
    state.sidebarViewMode = "flat";
    state.agents = [makeAgent({ agent_id: 2 }), makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByLabelText("Sort by id descending"));
    expect(screen.getByText("Sort by")).toBeTruthy();
    expect(screen.getByText("id")).toBeTruthy();
    expect(screen.getByText("active")).toBeTruthy();
    expect(screen.getByText("status")).toBeTruthy();
    fireEvent.click(screen.getByText("id"));
    // Clicking the same key again flips the direction -> aria-label updates to ascending
    expect(screen.getByLabelText("Sort by id ascending")).toBeTruthy();
  });

  it("toolbar keeps flex-wrap as a backstop (sort is now a single icon)", () => {
    state.sidebarViewMode = "flat";
    state.agents = [makeAgent({ agent_id: 1 }), makeAgent({ agent_id: 2 })];
    wrap(<AgentSidebar {...handlers} />);
    const sortBtn = screen.getByLabelText("Sort by id descending");
    const toolbarRow = sortBtn.closest("div.border-b");
    expect(toolbarRow?.className).toContain("flex-wrap");
  });

  it("spawn bar container allows its contents to shrink (min-w-0)", () => {
    wrap(<AgentSidebar {...handlers} />);
    const spawnBar = screen.getByLabelText("Spawn agent").closest("div.border-b");
    expect(spawnBar?.className).toContain("min-w-0");
  });

  it("renders agents as flat list when viewMode=flat", () => {
    state.sidebarViewMode = "flat";
    state.agents = [makeAgent({ agent_id: 1 }), makeAgent({ agent_id: 2 })];
    wrap(<AgentSidebar {...handlers} />);
    // Agents should render as rows
    expect(screen.getByTestId("row-1")).toBeTruthy();
    expect(screen.getByTestId("row-2")).toBeTruthy();
  });

  it("defaults to id descending; clicking the active key reverses direction", () => {
    state.sidebarViewMode = "flat";
    state.agents = [makeAgent({ agent_id: 2 }), makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    // Default sort is id descending — id=2 before id=1 with no interaction.
    let rows = screen.getAllByTestId(/^row-/);
    expect(rows[0].getAttribute("data-testid")).toBe("row-2");
    expect(rows[1].getAttribute("data-testid")).toBe("row-1");
    // Clicking the already-active "id" reverses to ascending (via the
    // sort popover — the keys are no longer inline in the toolbar).
    fireEvent.click(screen.getByLabelText("Sort by id descending"));
    fireEvent.click(screen.getByText("id"));
    rows = screen.getAllByTestId(/^row-/);
    expect(rows[0].getAttribute("data-testid")).toBe("row-1");
    expect(rows[1].getAttribute("data-testid")).toBe("row-2");
  });
});

describe("Header layout + search overlay (task #723)", () => {
  it("expanded header: Ava left, search before collapse (rightmost)", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    const header = screen.getByText("Ava").closest("header")!;
    const labels = Array.from(header.querySelectorAll("button")).map((b) => b.getAttribute("aria-label"));
    // Expanded-state top button order: search -> collapse (collapse rightmost); Ava title leftmost
    expect(labels.filter(Boolean)).toEqual(["Search agents", "Collapse sidebar"]);
    expect(screen.getByText("Ava")).toBeTruthy();
  });

  it("collapsed rail: expand on top, search below it, nav shortcuts stacked at the bottom", () => {
    state.sidebarCollapsed = true;
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    const aside = document.querySelector("aside")!;
    const labels = Array.from(aside.querySelectorAll("button")).map((b) => b.getAttribute("aria-label"));
    // Top (user ruling 2026-08-05 21:50): expand at the very top, search
    // directly below it — stacked vertically, never side by side. Bottom:
    // the statistics popover trigger + the four nav shortcuts, so the
    // collapsed rail is not a lone expand button.
    expect(labels.filter(Boolean)).toEqual([
      "Expand sidebar",
      "Search agents",
      "Statistics",
      "Memory graph",
      "Fleet",
      "Insights",
      "Control",
    ]);
    // Collapsed rail top buttons carry no border-b divider
    const searchBtn = screen.getByLabelText("Search agents");
    const expandBtn = screen.getByLabelText("Expand sidebar");
    expect(searchBtn.className).not.toContain("border-b");
    expect(expandBtn.className).not.toContain("border-b");
  });

  it("collapsed rail nav shortcut pushes the route", () => {
    state.sidebarCollapsed = true;
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByLabelText("Insights"));
    expect(pushSpy).toHaveBeenCalledWith("/insights");
  });

  it("clicking the search button opens the floating overlay", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.queryByLabelText("Search agent")).toBeNull();
    fireEvent.click(screen.getByLabelText("Search agents"));
    // The overlay input appears (autofocus)
    expect(screen.getByLabelText("Search agent")).toBeTruthy();
  });

  it("typing pushes the query to the store; picking a result selects + closes", () => {
    state.agents = [
      makeAgent({ agent_id: 1, label: "alpha" }),
      makeAgent({ agent_id: 2, label: "beta" }),
    ];
    state.setActiveId = vi.fn();
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByLabelText("Search agents"));
    // Empty query: the overlay lists all agents (result filtering is driven by AgentSidebar's
    // filteredAgents - see the "Search filtering" describe)
    expect(screen.getByTestId("overlay-result-1")).toBeTruthy();
    expect(screen.getByTestId("overlay-result-2")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Search agent"), { target: { value: "beta" } });
    expect(state.setSearchQuery).toHaveBeenCalledWith("beta");
    fireEvent.click(screen.getByTestId("overlay-result-2"));
    expect(state.setActiveId).toHaveBeenCalledWith(2);
    expect(screen.queryByLabelText("Search agent")).toBeNull();
  });

  it("Esc closes the overlay", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByLabelText("Search agents"));
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByLabelText("Search agent")).toBeNull();
  });

  it("clicking the dimmed backdrop closes the overlay", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByLabelText("Search agents"));
    fireEvent.click(screen.getByTestId("search-overlay-backdrop"));
    expect(screen.queryByLabelText("Search agent")).toBeNull();
  });

  // HomeShell owns the one-time app-entry reset. AgentSidebar can remount when
  // HomeLayout switches between its RRP and static frames, and that layout
  // transition must not reinterpret the user's collapse click as a new entry.
  it("mount with sidebarCollapsed=true preserves the active session choice", () => {
    state.sidebarCollapsed = true;
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    expect(state.setSidebarCollapsed).not.toHaveBeenCalled();
  });
});

describe("Search is overlay-only (task #750: sidebar list is NOT filtered)", () => {
  it("searchQuery empty → sidebar shows all agents", () => {
    state.searchQuery = "";
    state.agents = [
      makeAgent({ agent_id: 1, label: "alpha" }),
      makeAgent({ agent_id: 2, label: "beta" }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByTestId("row-1")).toBeTruthy();
    expect(screen.getByTestId("row-2")).toBeTruthy();
  });

  it("searchQuery matches a label → sidebar still shows every agent", () => {
    state.searchQuery = "alpha";
    state.agents = [
      makeAgent({ agent_id: 1, label: "alpha" }),
      makeAgent({ agent_id: 2, label: "beta" }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByTestId("row-1")).toBeTruthy();
    expect(screen.getByTestId("row-2")).toBeTruthy();
  });

  it("searchQuery matches an id → sidebar still shows every agent", () => {
    state.searchQuery = "2";
    state.agents = [
      makeAgent({ agent_id: 1, label: "alpha" }),
      makeAgent({ agent_id: 2, label: "beta" }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByTestId("row-1")).toBeTruthy();
    expect(screen.getByTestId("row-2")).toBeTruthy();
  });

  it("searchQuery matches nothing → sidebar still shows every agent", () => {
    state.searchQuery = "nonexistent";
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByTestId("row-1")).toBeTruthy();
  });

  it("overlay results are agent-id DESCENDING (task #750)", () => {
    state.agents = [
      makeAgent({ agent_id: 1, label: "alpha" }),
      makeAgent({ agent_id: 3, label: "gamma" }),
      makeAgent({ agent_id: 2, label: "beta" }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByLabelText("Search agents"));
    const results = screen
      .getAllByTestId(/^overlay-result-/)
      .map((el) => Number(el.getAttribute("data-testid")!.replace("overlay-result-", "")));
    expect(results).toEqual([3, 2, 1]);
  });

  it("overlay row shows `#id · label`, matching sidebar rows (task #750)", () => {
    state.agents = [makeAgent({ agent_id: 7, label: "worker" })];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByLabelText("Search agents"));
    expect(screen.getByTestId("overlay-result-7").textContent).toContain("#7 · worker");
  });
});

describe("agent status quick toggle (display.show_agent_status)", () => {
  it("defaults off → toolbar shows the 'Show agent status' toggle", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    const btn = screen.getByLabelText("Show agent status");
    expect(btn.getAttribute("aria-pressed")).toBe("false");
  });

  it("click toggle (off) → setSetting('display.show_agent_status', true)", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByLabelText("Show agent status"));
    expect(state.setSetting).toHaveBeenCalledWith("display.show_agent_status", true);
  });

  it("enabled → toggle reads 'Hide agent status'; click → setSetting(false)", () => {
    state.userSettings = { "display.show_agent_status": true };
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    const btn = screen.getByLabelText("Hide agent status");
    expect(btn.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(btn);
    expect(state.setSetting).toHaveBeenCalledWith("display.show_agent_status", false);
  });
});

describe("awaiting-reply indicator (notification.awaiting_reply)", () => {
  const notice: OpenNotice = {
    id: 1,
    title: "Q",
    content: null,
    priority: "P2",
    require_response: true,
    blocking: false,
    created_at: "2026-06-06T00:00:00Z",
  };

  it("default off → no waiting indicator even when agents have open notices", () => {
    state.agents = [
      makeAgent({ agent_id: 1, notices_awaiting_response: [notice] }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.queryByTitle(/waiting on you/)).toBeNull();
  });

  it("opted in → red-dot indicator with the cross-agent count", () => {
    state.userSettings = { "notification.awaiting_reply": true };
    state.agents = [
      makeAgent({ agent_id: 1, notices_awaiting_response: [notice] }),
      makeAgent({ agent_id: 2, notices_awaiting_response: [{ ...notice, id: 2 }] }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    // The indicator is a Link to /fleet with the waiting count
    expect(screen.getByRole("link", { name: /2/ })).toBeTruthy();
  });

  it("opted in but nothing waiting → no indicator", () => {
    state.userSettings = { "notification.awaiting_reply": true };
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.queryByRole("link")).toBeNull();
  });
});

describe("stable ID ordering (RCS: no resort on status / activity change)", () => {
  it("default sort is pinned to id descending", () => {
    expect(SIDEBAR_SORT_DEFAULT).toEqual({ key: "id", dir: "desc" });
  });

  it("status + last_active_at changes never reorder rows under the default sort", () => {
    state.sidebarViewMode = "flat";
    // last_active_at deliberately anti-correlated with id — under a
    // last-active sort these would order 1,2,3; the default must be 3,2,1.
    state.agents = [
      makeAgent({ agent_id: 1, status: "idling", last_active_at: "2026-05-15T03:00:00Z" }),
      makeAgent({ agent_id: 2, status: "idling", last_active_at: "2026-05-15T02:00:00Z" }),
      makeAgent({ agent_id: 3, status: "idling", last_active_at: "2026-05-15T01:00:00Z" }),
    ];
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const view = render(
      <QueryClientProvider client={qc}>
        <AgentSidebar {...handlers} />
      </QueryClientProvider>,
    );
    const order = () =>
      screen.getAllByTestId(/^row-/).map((r) => r.getAttribute("data-testid"));
    expect(order()).toEqual(["row-3", "row-2", "row-1"]);

    // Agent 1 wakes up: becomes running AND the most recently active.
    // The list must hold its ID order — a jumping row is a leaked signal.
    state.agents = [
      makeAgent({ agent_id: 1, status: "running", last_active_at: "2026-05-16T00:00:00Z" }),
      makeAgent({ agent_id: 2, status: "idling", last_active_at: "2026-05-15T02:00:00Z" }),
      makeAgent({ agent_id: 3, status: "idling", last_active_at: "2026-05-15T01:00:00Z" }),
    ];
    view.rerender(
      <QueryClientProvider client={qc}>
        <AgentSidebar {...handlers} />
      </QueryClientProvider>,
    );
    expect(order()).toEqual(["row-3", "row-2", "row-1"]);
  });
});
