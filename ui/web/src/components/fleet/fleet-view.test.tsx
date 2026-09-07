import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentRow , NoticeItem } from "@/lib/types";

// next/link needs no router in this isolated render — a plain anchor suffices.
// next/navigation: TaskGraph calls useRouter at its top level now (the graph
// mode's double-click → owner timeline path), so the layout tests need a stub.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));
vi.mock("next/link", () => ({
  default: ({ children, href, ...rest }: { children: ReactNode; href: string }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

// GraphView is exercised on its own in graph-view.test.tsx; stub it here so the
// FleetView layout/tab tests stay deterministic (no d3-force simulation, no
// /api/fleet/graph fetch).
vi.mock("@/components/fleet/graph-view", () => ({
  GraphView: () => <div data-testid="graph-view">graph</div>,
}));

// Breakpoint — tests default to desktop (isLarge = true). Individual tests
// override via isLargeMock.mockReturnValue(false). R4 layer 4: FleetView
// consumes useBreakpoint — the single breakpoint source.
const isLargeMock = vi.fn<() => boolean>(() => true);
vi.mock("@/lib/breakpoint", () => ({
  useBreakpoint: () => ({
    tier: isLargeMock() ? "xl" : "xs",
    isNarrow: !isLargeMock(),
    isLarge: isLargeMock(),
  }),
}));

// vitest+happy-dom has no localStorage by default — the view persists its mobile
// tab there, so provide a simple in-memory mock.
const localStorageMock = (() => {
  const store = new Map<string, string>();
  return {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  };
})();
Object.defineProperty(globalThis, "localStorage", { value: localStorageMock, writable: true });
Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
  configurable: true,
  value: vi.fn(),
});

// The live agent list is injected; the view itself is pure projection.
const agentsMock = vi.fn<() => AgentRow[]>();
vi.mock("@/lib/use-fleet-agents", () => ({ useFleetAgents: () => agentsMock() }));

// The embedded Inbox queue fetches notice history on mount; empty here.
vi.mock("@/lib/api", () => ({
  api: {
    getNotices: () =>
      Promise.resolve({ open: [], awaiting: [], resolved_page: [], next_cursor: null }),
  },
}));

// useInboxFeed needs EventStreamProvider; mock it away for fleet-view tests.
// useNotices — the queue's single data contract; inject per-test via
// useNoticesMock (default: empty).
interface TestNoticesFeed {
  open: NoticeItem[];
  awaiting: NoticeItem[];
  resolved: NoticeItem[];
  fetchNextPage: () => void;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  error: boolean;
  resolvedError: boolean;
  isLoading: boolean;
}
const useNoticesMock = vi.fn<() => TestNoticesFeed>(() => ({
  open: [],
  awaiting: [],
  resolved: [],
  fetchNextPage: () => undefined,
  hasNextPage: false,
  isFetchingNextPage: false,
  error: false,
  resolvedError: false,
  isLoading: false,
}));
vi.mock("@/lib/use-notices", () => ({ useNotices: () => useNoticesMock() }));

// useTasks (the queue's grouping join + the Task Graph) also needs
// EventStreamProvider; an empty registry keeps every queue entry flat.
vi.mock("@/lib/use-tasks", () => ({
  useTasks: () => ({ tasks: [], loading: false, error: false }),
}));

// The inbox's fleet-wide open-pages hook is SSE-backed (EventStreamProvider);
// stub it to no open pages for these layout tests.
vi.mock("@/lib/use-all-pages", () => ({ useAllPages: () => [] }));

// Queue-collapse / left-view are DB-backed user settings; the reactive mock
// keeps them deterministic + re-renders on setSetting (no React Query network).
vi.mock("@/lib/use-user-settings", () => import("@/test-support/user-settings-mock"));

import { mockSetSettingCalls, resetMockSettings } from "@/test-support/user-settings-mock";

import { BAR_HEIGHT_CLASS } from "@/lib/layout";
import { FleetView } from "./fleet-view";

beforeEach(() => resetMockSettings());
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  localStorage.clear();
  window.history.replaceState(null, "", "/fleet");
  isLargeMock.mockReturnValue(true); // reset to desktop default
});

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function makeAgent(over: Partial<AgentRow> & { agent_id: number }): AgentRow {
  return {
    spawner: "user",
    fork_source_agent_id: null,
    status: "running",
    pid: 1,
    spawned_at: "2026-06-06T00:00:00Z",
    started_at: "2026-06-06T00:00:00Z",
    last_active_at: "2026-06-06T00:00:00Z", last_inbound_at: "2026-06-06T00:00:00Z",
    label: null,
    machine: "test",
    supports_vision: true,
    notices_awaiting_response: [], unread_notice_count: 0,
    heartbeat_paused_until: null,
    liveness_state: "online",
    ...over,
  };
}

describe("FleetView (desktop)", () => {
  it("shows active/total counts and the graph surface", () => {
    agentsMock.mockReturnValue([
      makeAgent({ agent_id: 1, label: "lead", status: "idling" }),
      makeAgent({ agent_id: 2, spawner: "agent:1", label: "worker" }),
      makeAgent({ agent_id: 3, spawner: "agent:1", status: "terminated" }),
    ]);
    wrap(<FleetView />);
    // 2 alive (idling + running), 1 terminated
    expect(screen.getByText("2 active · 3 total")).toBeTruthy();
    expect(screen.getByTestId("graph-view")).toBeTruthy();
  });

  it("shows 0 active · 0 total when there are no agents", () => {
    agentsMock.mockReturnValue([]);
    wrap(<FleetView />);
    expect(screen.getByText("0 active · 0 total")).toBeTruthy();
  });

  it("root fills the pane width (flex-1) regardless of content (task #1066)", () => {
    // Regression: main is a row-flex landmark, so a root without flex-1
    // shrinks to its content max-width — clicking a short Inbox item
    // collapsed the whole fleet surface to the left ~60% of the viewport.
    agentsMock.mockReturnValue([]);
    const { container } = wrap(<FleetView />);
    const root = container.firstElementChild as HTMLElement;
    expect(root.classList.contains("flex-1")).toBe(true);
  });

  it("aligns the Agents/Tasks bar with the Inbox header (one shared height)", () => {
    agentsMock.mockReturnValue([makeAgent({ agent_id: 1 })]);
    wrap(<FleetView />);
    // Both bars must pin the SAME explicit height — their content heights
    // differ, so equal padding would still leave the two borders off by a
    // couple of pixels.
    const heightClass = (el: Element) => [...el.classList].find((c) => /^h-\d/.test(c));
    const tabBar = screen.getByText("Agents").closest("div")!;
    const inboxHeader = screen.getByText("Inbox").closest("header")!;
    expect(heightClass(tabBar)).toBeDefined();
    expect(heightClass(tabBar)).toBe(heightClass(inboxHeader));
    expect(heightClass(tabBar)).toBe(BAR_HEIGHT_CLASS);
  });

  it("links the header back to the conversation", () => {
    agentsMock.mockReturnValue([makeAgent({ agent_id: 1 })]);
    wrap(<FleetView />);
    const link = screen.getByLabelText("Back to conversation");
    expect(link.getAttribute("href")).toBe("/");
  });

  it("renders the unified Inbox panel (no Decisions/Reviews split)", () => {
    agentsMock.mockReturnValue([makeAgent({ agent_id: 1, unread_notice_count: 2 })]);
    wrap(<FleetView />);
    expect(screen.getByText("Inbox")).toBeTruthy();
    expect(screen.queryByText("Decisions")).toBeNull();
    expect(screen.queryByText("Reviews")).toBeNull();
  });

  it("reads agent_id from the route and anchors that agent's notice", async () => {
    window.history.replaceState(null, "", "/fleet?agent_id=405");
    agentsMock.mockReturnValue([
      makeAgent({ agent_id: 1, label: "lead" }),
      makeAgent({ agent_id: 405, label: "reviewer" }),
    ]);
    useNoticesMock.mockReturnValue({
      open: [
        {
          id: 1405,
          agent_id: 405,
          agent_label: "reviewer",
          title: "Review result",
          content: null,
          priority: "P3",
          require_response: false,
          blocking: false,
          created_at: "2026-06-06T00:00:00Z",
          updated_at: null,
          resolved_at: null,
          resolution: null,
          reply: null,
          task_id: null,
        },
      ],
      awaiting: [],
      resolved: [],
      fetchNextPage: () => undefined,
      hasNextPage: false,
      isFetchingNextPage: false,
      error: false,
      resolvedError: false,
      isLoading: false,
    });

    wrap(<FleetView />);

    const list = screen.getByRole("list", { name: /inbox queue/i });
    const row = within(list).getByText("Review result").closest('[data-testid="inbox-row"]');
    await waitFor(() => expect(row?.getAttribute("data-anchor-highlighted")).toBe("true"));
  });

  it("collapses the queue panel to a static handle and persists the choice", () => {
    agentsMock.mockReturnValue([
      makeAgent({
        agent_id: 1,
        notices_awaiting_response: [{ id: 1, title: "Q", content: null, priority: "P0", require_response: true, blocking: true, created_at: "2026-06-06T00:00:00Z" }],
      }),
    ]);
    wrap(<FleetView />);
    fireEvent.click(screen.getByLabelText("Collapse queue"));

    // Queue gone; only the static handle remains — and it carries NO dynamic
    // signal (no badge/count), even though a P0 notice is pending.
    expect(screen.queryByText("Inbox")).toBeNull();
    const handle = screen.getByLabelText("Expand queue");
    expect(handle.textContent).toBe("Queue");
    expect(mockSetSettingCalls()).toContainEqual({ key: "display.fleet_queue_collapsed", value: true });
  });

  it("expanding the collapsed queue restores the Inbox and persists", () => {
    agentsMock.mockReturnValue([makeAgent({ agent_id: 1 })]);
    wrap(<FleetView />);
    fireEvent.click(screen.getByLabelText("Collapse queue"));
    fireEvent.click(screen.getByLabelText("Expand queue"));
    expect(screen.getByText("Inbox")).toBeTruthy();
    expect(mockSetSettingCalls().at(-1)).toEqual({ key: "display.fleet_queue_collapsed", value: false });
  });

  it("restores the collapsed state from the DB setting on mount", () => {
    resetMockSettings({ "display.fleet_queue_collapsed": true });
    agentsMock.mockReturnValue([makeAgent({ agent_id: 1 })]);
    wrap(<FleetView />);
    expect(screen.queryByText("Inbox")).toBeNull();
    expect(screen.getByLabelText("Expand queue")).toBeTruthy();
  });
});

describe("FleetView (mobile)", () => {
  beforeEach(() => {
    isLargeMock.mockReturnValue(false);
  });

  it("renders the Agents / Tasks / Inbox mobile tabs; Agents shows the graph", () => {
    agentsMock.mockReturnValue([makeAgent({ agent_id: 1, label: "lead", status: "running" })]);
    wrap(<FleetView />);
    const tablist = screen.getByRole("tablist", { name: "Fleet sections" });
    expect(tablist.tagName).toBe("DIV");
    expect(screen.getByRole("tab", { name: /Agents/ })).toBeTruthy();
    expect(screen.getByRole("tab", { name: /Tasks/ })).toBeTruthy();
    expect(screen.getByRole("tab", { name: /Inbox/ })).toBeTruthy();
    expect(screen.queryByRole("tab", { name: /Decisions/ })).toBeNull();
    expect(screen.queryByRole("tab", { name: /Reviews/ })).toBeNull();
    expect(screen.getByTestId("graph-view")).toBeTruthy();
  });

  it("Inbox tab lands on the list; tapping a row opens detail; back returns to the list", () => {
    agentsMock.mockReturnValue([makeAgent({ agent_id: 1, label: "lead" })]);
    useNoticesMock.mockReturnValue({
      open: [],
      awaiting: [
        {
          id: 101,
          agent_id: 1,
          agent_label: "lead",
          title: "What next?",
          content: null,
          priority: "P3",
          require_response: true,
          blocking: false,
          created_at: "2026-06-06T00:00:00Z",
          updated_at: null,
          resolved_at: null,
          resolution: null,
          reply: null,
          task_id: null,
        },
      ],
      resolved: [],
      fetchNextPage: () => undefined,
      hasNextPage: false,
      isFetchingNextPage: false,
      error: false,
      resolvedError: false,
      isLoading: false,
    });
    wrap(<FleetView />);
    fireEvent.click(screen.getByRole("tab", { name: /Inbox/ }));
    expect(screen.getByRole("list", { name: /inbox queue/i })).toBeTruthy();
    expect(screen.getByText("What next?")).toBeTruthy();
    expect(screen.queryByLabelText("Back to list")).toBeNull();
    fireEvent.click(screen.getByText("What next?"));
    expect(screen.getByLabelText("Back to list")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Back to list"));
    expect(screen.getByRole("list", { name: /inbox queue/i })).toBeTruthy();
    expect(screen.queryByLabelText("Back to list")).toBeNull();
  });

  it("shows the combined Inbox badge on the mobile tab", () => {
    agentsMock.mockReturnValue([
      makeAgent({
        agent_id: 1,
        status: "running",
        notices_awaiting_response: [{ id: 1, title: "Q", content: null, priority: "P3", require_response: true, blocking: false, created_at: "2026-06-06T00:00:00Z" }],
        unread_notice_count: 3,
      }),
    ]);
    wrap(<FleetView />);
    // Inbox badge = 1 decision + 3 unread FYI = 4; Agents = 1 alive.
    expect(screen.getByRole("tab", { name: /Inbox 4/ })).toBeTruthy();
    expect(screen.getByRole("tab", { name: /Agents 1/ })).toBeTruthy();
  });

  it("persists mobile tab selection across remounts", () => {
    localStorage.setItem("ava.fleet.mobileTab", "inbox");
    agentsMock.mockReturnValue([makeAgent({ agent_id: 1, label: "lead", unread_notice_count: 1 })]);
    wrap(<FleetView />);
    // Should restore to the Inbox tab.
    expect(screen.getByRole("tab", { name: /Inbox/, selected: true })).toBeTruthy();
  });

  it("deep link #inbox selects the Inbox tab on mobile", () => {
    window.location.hash = "#inbox";
    agentsMock.mockReturnValue([makeAgent({ agent_id: 1, label: "lead" })]);
    wrap(<FleetView />);
    expect(screen.getByRole("tab", { name: /Inbox/, selected: true })).toBeTruthy();
    expect(screen.getByRole("list", { name: /inbox queue/i })).toBeTruthy();
    window.location.hash = "";
  });
});
