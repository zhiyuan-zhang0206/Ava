// Fleet route jumps (task #2909) — the deep links an inspector widget button
// produces. `?notice=` opens one notice's detail in the Inbox; `?task=`
// selects a task, mounts the Tasks surface, and must not rewrite the durable
// view preference; malformed params are inert.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentRow, NoticeItem, TaskRow } from "@/lib/types";
import type { TasksResult } from "@/lib/use-tasks";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));
vi.mock("next/link", () => ({
  default: ({ children, href, ...rest }: { children: React.ReactNode; href: string }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

// The two left-pane surfaces are stubbed to their identity — these tests
// assert WHICH surface is mounted and with which selection, not the canvas
// (graph-view.test / task-graph.test cover the canvases).
vi.mock("@/components/fleet/graph-view", () => ({
  GraphView: () => <div data-testid="graph-view" />,
}));
vi.mock("@/components/fleet/task-graph", () => ({
  TaskGraph: ({ selectedTaskId }: { selectedTaskId: number | null }) => (
    <div data-testid="task-graph" data-selected={String(selectedTaskId)} />
  ),
}));

const isLargeMock = vi.fn<() => boolean>(() => true);
vi.mock("@/lib/breakpoint", () => ({
  useBreakpoint: () => ({
    tier: isLargeMock() ? "xl" : "xs",
    isNarrow: !isLargeMock(),
    isLarge: isLargeMock(),
  }),
}));

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

const agentsMock = vi.fn<() => AgentRow[]>(() => [
  {
    agent_id: 7,
    spawner: "user",
    fork_source_agent_id: null,
    status: "idling",
    pid: 1,
    spawned_at: "2026-06-06T00:00:00Z",
    started_at: "2026-06-06T00:00:00Z",
    last_active_at: "2026-06-06T00:00:00Z",
    last_inbound_at: "2026-06-06T00:00:00Z",
    label: "worker",
    machine: "test",
    supports_vision: true,
    notices_awaiting_response: [],
    unread_notice_count: 0,
    heartbeat_paused_until: null,
    liveness_state: "online",
  },
]);
vi.mock("@/lib/use-fleet-agents", () => ({ useFleetAgents: () => agentsMock() }));

vi.mock("@/lib/api", () => ({
  api: {
    getNotices: () =>
      Promise.resolve({ open: [], awaiting: [], resolved_page: [], next_cursor: null }),
    resolveNotice: () => Promise.resolve({ status: "ok" }),
  },
}));

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

const useTasksMock = vi.fn<() => TasksResult>(() => ({ tasks: [], loading: false, error: false }));
vi.mock("@/lib/use-tasks", () => ({ useTasks: () => useTasksMock() }));

vi.mock("@/lib/use-all-pages", () => ({ useAllPages: () => [] }));
vi.mock("@/lib/use-user-settings", () => import("@/test-support/user-settings-mock"));

import { mockSetSettingCalls, resetMockSettings } from "@/test-support/user-settings-mock";

import { FleetView } from "./fleet-view";

function notice(over: Partial<NoticeItem> & { id: number; title: string }): NoticeItem {
  return {
    id: over.id,
    agent_id: over.agent_id ?? 7,
    agent_label: over.agent_label ?? "worker",
    title: over.title,
    content: null,
    priority: over.priority ?? "P2",
    require_response: over.require_response ?? true,
    blocking: false,
    created_at: over.created_at ?? "2026-06-14T00:00:00Z",
    updated_at: null,
    resolved_at: null,
    resolution: null,
    reply: null,
    task_id: over.task_id ?? null,
    expire_at: "2026-06-15T00:00:00Z",
  };
}

function task(over: Partial<TaskRow> & { id: number }): TaskRow {
  return {
    id: over.id,
    parent_id: over.parent_id ?? 1,
    title: over.title ?? `Task ${over.id}`,
    description: over.description ?? "",
    results: null,
    status: over.status ?? "in_progress",
    priority: over.priority ?? "P2",
    owner: over.owner ?? 7,
    created_by: "agent:7",
    created_at: over.created_at ?? "2026-06-14T00:00:00Z",
    updated_at: "2026-06-14T00:00:00Z",
    reminder_count: 0,
    last_reminded_at: null,
    remind_interval_seconds: null,
    owner_label: null,
  };
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

beforeEach(() => resetMockSettings());
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  localStorage.clear();
  window.history.replaceState(null, "", "/fleet");
  isLargeMock.mockReturnValue(true);
});

describe("fleet route jumps", () => {
  it("?task= selects the task and mounts the Tasks surface without persisting the view", async () => {
    useTasksMock.mockReturnValue({ tasks: [task({ id: 42, owner: 7 })], loading: false, error: false });
    window.history.replaceState(null, "", "/fleet?task=42");
    wrap(<FleetView />);
    await waitFor(() => {
      expect(screen.getByTestId("task-graph").getAttribute("data-selected")).toBe("42");
    });
    expect(screen.queryByTestId("graph-view")).toBeNull();
    // The jump reveals the view but does not rewrite the durable preference.
    expect(mockSetSettingCalls().some((c) => c.key === "display.fleet_left_view")).toBe(false);
  });

  it("without ?task= the default view stays the agent graph", () => {
    wrap(<FleetView />);
    expect(screen.getByTestId("graph-view")).toBeTruthy();
    expect(screen.queryByTestId("task-graph")).toBeNull();
  });

  it("?notice= opens that notice's detail (targeting a non-default row)", async () => {
    useNoticesMock.mockReturnValue({
      open: [],
      awaiting: [
        notice({ id: 7, title: "first notice", created_at: "2026-06-14T00:00:00Z" }),
        notice({ id: 8, title: "second notice", created_at: "2026-06-14T01:00:00Z" }),
      ],
      resolved: [],
      fetchNextPage: () => undefined,
      hasNextPage: false,
      isFetchingNextPage: false,
      error: false,
      resolvedError: false,
      isLoading: false,
    });
    window.history.replaceState(null, "", "/fleet?notice=8");
    wrap(<FleetView />);
    // The reply box (require_response notice) carries the selected notice's
    // title in its accessible name — the unambiguous "which detail is open".
    await waitFor(() => {
      expect(screen.getByRole("textbox", { name: /second notice/ })).toBeTruthy();
    });
  });

  it("without ?notice= the queue keeps its own default selection", async () => {
    useNoticesMock.mockReturnValue({
      open: [],
      awaiting: [notice({ id: 7, title: "first notice" })],
      resolved: [],
      fetchNextPage: () => undefined,
      hasNextPage: false,
      isFetchingNextPage: false,
      error: false,
      resolvedError: false,
      isLoading: false,
    });
    wrap(<FleetView />);
    await waitFor(() => {
      expect(screen.getByRole("textbox", { name: /first notice/ })).toBeTruthy();
    });
  });

  it("?task= on mobile switches to the tasks tab", async () => {
    isLargeMock.mockReturnValue(false);
    useTasksMock.mockReturnValue({ tasks: [task({ id: 42, owner: 7 })], loading: false, error: false });
    window.history.replaceState(null, "", "/fleet?task=42");
    wrap(<FleetView />);
    await waitFor(() => {
      expect(screen.getByTestId("task-graph").getAttribute("data-selected")).toBe("42");
    });
  });

  it("malformed route ids are inert (default view, no crash)", () => {
    window.history.replaceState(null, "", "/fleet?task=abc&notice=");
    wrap(<FleetView />);
    expect(screen.getByTestId("graph-view")).toBeTruthy();
    expect(screen.queryByTestId("task-graph")).toBeNull();
  });

  it("an unknown ?notice= id leaves the default selection alone", async () => {
    useNoticesMock.mockReturnValue({
      open: [],
      awaiting: [notice({ id: 7, title: "first notice" })],
      resolved: [],
      fetchNextPage: () => undefined,
      hasNextPage: false,
      isFetchingNextPage: false,
      error: false,
      resolvedError: false,
      isLoading: false,
    });
    window.history.replaceState(null, "", "/fleet?notice=999");
    wrap(<FleetView />);
    await waitFor(() => {
      expect(screen.getByRole("textbox", { name: /first notice/ })).toBeTruthy();
    });
  });
});
