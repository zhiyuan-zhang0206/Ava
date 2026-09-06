// InspectorPanel render + window-selector tests — verify the sections
// (shells / heartbeat / config overlay / cost) render from a mocked /inspect
// response, empty-section visibility, and that the header window selector re-queries
// with the chosen `hours`. Also covers mobile overlay rendering.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render as rtlRender,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { InspectorPanel, InspectorToggle } from "./inspector-panel";
import { formatAbsolute, formatRelative } from "@/lib/time";
import type { AgentInspect, AgentInspectLive, PageRow } from "@/lib/types";

// vi.hoisted so the mock fn is initialized before the hoisted vi.mock factory
// runs (the factory fires during the InspectorPanel import, before module-body
// consts would otherwise initialize).
const { getAgentInspect, getAgentInspectLive, listPages, resolveNotice } =
  vi.hoisted(() => ({
    getAgentInspect:
      vi.fn<
        (
          agentId: number,
          hours?: number | null,
          sinceCompact?: boolean,
          signal?: AbortSignal,
        ) => Promise<AgentInspect>
      >(),
    getAgentInspectLive:
      vi.fn<(agentId: number, signal?: AbortSignal) => Promise<AgentInspectLive>>(),
    // useAgentPages fetches the open-pages list; default to none so the Page
    // section stays hidden and these render tests stay focused on the
    // /inspect sections. The dedicated use-agent-pages.test.ts covers the fetch +
    // SSE fold.
    listPages: vi.fn<(agentId: number) => Promise<PageRow[]>>(() => Promise.resolve([])),
    // Notice resolve — default to success; the notice-reply tests drive it.
    resolveNotice: vi.fn(() => Promise.resolve({ status: "ok" })),
  }));
vi.mock("@/lib/api", () => ({
  api: { getAgentInspect, getAgentInspectLive, listPages, resolveNotice },
}));

// useAgentPages subscribes to the global SSE stream; stub it to a no-op so the
// panel renders without an <EventStreamProvider> (its page-fold behavior is
// covered in use-agent-pages.test.ts).
const streamHandlers = vi.hoisted(() => ({
  system: undefined as ((event: unknown) => void) | undefined,
  connection: undefined as ((event: { type: string }) => void) | undefined,
}));
vi.mock("@/lib/useEventStream", () => ({
  EventStreamProvider: ({ children }: { children: React.ReactNode }) => children,
  useEventStream: (
    onSystemEvent: (event: unknown) => void,
    onConnectionEvent: (event: { type: string }) => void,
  ) => {
    streamHandlers.system = onSystemEvent;
    streamHandlers.connection = onConnectionEvent;
  },
}));

const panelState = { open: true, hours: 24 as number | null };
const toggle = vi.fn(() => {
  panelState.open = !panelState.open;
});
const setInspectorHours = vi.fn((hours: number | null) => {
  panelState.hours = hours;
});
vi.mock("@/lib/inspector-panel-store", async () => {
  const React = await import("react");
  return {
    useInspectorOpen: () => ({ open: panelState.open, toggle }),
    useInspectorHours: () => {
      const [hours, setHours] = React.useState(panelState.hours);
      return {
        inspectorHours: hours,
        setInspectorHours: (next: number | null) => {
          setInspectorHours(next);
          setHours(next);
        },
      };
    },
  };
});

// Breakpoint: tests default to desktop (isLarge = true). R4 layer 4: the
// panel's breakpoint awareness goes through useBreakpoint.
const isLargeMock = vi.fn<() => boolean>(() => true);
vi.mock("@/lib/breakpoint", () => ({
  useBreakpoint: () => ({
    tier: isLargeMock() ? "xl" : "xs",
    isNarrow: !isLargeMock(),
    isLarge: isLargeMock(),
  }),
}));


beforeEach(() => {
  getAgentInspect.mockResolvedValue(fixture());
  getAgentInspectLive.mockResolvedValue(liveFixture());
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
  getAgentInspect.mockReset();
  getAgentInspectLive.mockReset();
  resolveNotice.mockClear();
  toggle.mockReset();
  panelState.open = true;
  panelState.hours = 24;
  setInspectorHours.mockClear();
  streamHandlers.system = undefined;
  streamHandlers.connection = undefined;
  isLargeMock.mockReturnValue(true);
});

function render(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function renderWithGlobalRetries(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: 3, retryDelay: 0 } },
  });
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function fixture(overrides: Partial<AgentInspect> = {}): AgentInspect {
  return {
    agent_id: 1,
    machine: "test-host",
    liveness_state: "online",
    last_probe_at: null,
    shells_available: true,
    window_hours: 24,
    since_compact: false,
    shells: [
      // created_at is offset from fixture-build time so the tick-computed
      // runtime lands on the same formatted span as uptime_seconds (8040s /
      // 660s) no matter when the test runs. The duration-asserting test
      // freezes Date via vi.setSystemTime for exact determinism. (created_at
      // itself is not rendered in the panel — runtime only.)
      {
        id: 0,
        name: "dev-server",
        created_at: new Date(Date.now() - 8040_000).toISOString(),
        uptime_seconds: 8040,
      },
      { id: 1, name: null, created_at: null, uptime_seconds: 45 },
      {
        id: 2,
        name: "watcher",
        created_at: new Date(Date.now() - 660_000).toISOString(),
        uptime_seconds: 660,
      },
    ],
    config_overlay: { llm_model: "claude-opus-4-8", auto_compact_fraction: 0.7 },
    cost: {
      cost_usd: 0.4213,
      unpriced_calls: 1,
      llm_calls: 142,
      tokens_in: 1_200_000,
      tokens_out: 84_000,
      tokens_cached: 1_100_000,
      tokens_reasoning: 5_000,
      cache_hit_pct: 91.7,
    },
    stats: {
      turn_total: 7,
      turn_ok: 6,
      turn_p50_seconds: 3.1,
      turn_p90_seconds: 9.4,
      turn_min_seconds: 1.2,
      turn_max_seconds: 41,
      exec_ok: 51,
      exec_failed: 2,
    },
    // Default heartbeat: a running agent that never paused — time-independent,
    // so the broad render tests stay deterministic. The dedicated heartbeat
    // describe drives the idle / paused / last-pause states with live offsets.
    heartbeat: { interval_s: 300, next_at: null, paused_until: null, heartbeat_pending: false, last_pause: null },
    // Default: no open notice.
    notice: null,
    tps: { lm_stage_tps: 42.5, agent_lifecycle_tps: 8.3 },
    activity: { active_seconds: 1800, alive_seconds: 3600, active_rate: 0.5, llm_seconds: 1200, exec_seconds: 450 },
    spawned_at: "2026-06-14T12:00:00Z",
    started_at: "2026-06-14T12:00:05Z",
    ...overrides,
  };
}

function liveFixture(overrides: Partial<AgentInspectLive> = {}): AgentInspectLive {
  const full = fixture(overrides);
  return {
    agent_id: full.agent_id,
    machine: full.machine,
    liveness_state: full.liveness_state,
    last_probe_at: full.last_probe_at,
    shells_available: full.shells_available,
    spawned_at: full.spawned_at,
    started_at: full.started_at,
    shells: full.shells,
    config_overlay: full.config_overlay,
    notice: full.notice,
    heartbeat: full.heartbeat,
  };
}

describe("InspectorPanel", () => {
  it("owns manual retry instead of inheriting the global automatic retry policy", async () => {
    getAgentInspectLive.mockRejectedValue(
      new Error("HTTP 503: inspector history query timed out; retry"),
    );
    renderWithGlobalRetries(<InspectorPanel agentId={1} />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Retry inspector" })).toBeTruthy(),
    );
    expect(getAgentInspectLive).toHaveBeenCalledTimes(1);
  });

  it("shows a failed cold load with an explicit retry action", async () => {
    getAgentInspectLive
      .mockRejectedValueOnce(new Error("HTTP 503: inspector history query timed out; retry"))
      .mockResolvedValueOnce(liveFixture());
    render(<InspectorPanel agentId={1} />);

    await waitFor(() =>
      expect(screen.getByText("HTTP 503: inspector history query timed out; retry")).toBeTruthy(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry inspector" }));
    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    expect(getAgentInspectLive).toHaveBeenCalledTimes(2);
  });

  it("never renders the previous agent while the new agent inspect is pending", async () => {
    let resolveAgentB: ((value: AgentInspectLive) => void) | undefined;
    getAgentInspectLive.mockImplementation((agentId) => {
      if (agentId === 1) {
        return Promise.resolve(
          liveFixture({
            agent_id: 1,
            shells: [
              {
                id: 91,
                name: "agent-a-private-shell",
                created_at: null,
                uptime_seconds: 10,
              },
            ],
            notice: {
              id: 92,
              title: "Agent A private notice",
              content: "Only Agent A may answer this",
              priority: "P1",
              require_response: true,
              blocking: true,
              created_at: "2026-06-14T12:00:00Z",
            },
          }),
        );
      }
      return new Promise<AgentInspectLive>((resolve) => {
        resolveAgentB = resolve;
      });
    });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = rtlRender(
      <QueryClientProvider client={qc}>
        <InspectorPanel agentId={1} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByText("agent-a-private-shell")).toBeTruthy());
    expect(screen.getByText("Agent A private notice")).toBeTruthy();
    expect(screen.getByRole("textbox")).toBeTruthy();

    view.rerender(
      <QueryClientProvider client={qc}>
        <InspectorPanel agentId={2} />
      </QueryClientProvider>,
    );
    await waitFor(() =>
      expect(getAgentInspectLive).toHaveBeenCalledWith(2, expect.any(AbortSignal)),
    );

    // B has not answered yet. React Query may supply A through
    // keepPreviousData, but no A-owned shell/notice/reply surface may render
    // under B's selected identity.
    expect(screen.queryByText("agent-a-private-shell")).toBeNull();
    expect(screen.queryByText("Agent A private notice")).toBeNull();
    expect(screen.queryByText("Only Agent A may answer this")).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();

    resolveAgentB?.(liveFixture({ agent_id: 2, shells: [] }));
    await waitFor(() => expect(screen.getByText("No open notice")).toBeTruthy());
    expect(screen.queryByText("Persistent shells")).toBeNull();
  });

  it("renders all sections from the /inspect response", async () => {
    // Freeze Date so the tick-computed runtime values are exact (created_at
    // offsets are relative to Date.now() at fixture build).
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-06-14T12:00:00Z"));
    // Rebuild BOTH fixtures on the frozen clock — beforeEach built them on
    // the real one, whose created_at offsets would render negative runtimes.
    getAgentInspect.mockResolvedValue(fixture());
    getAgentInspectLive.mockResolvedValue(liveFixture());
    render(<InspectorPanel agentId={1} />);

    // shells: count badge + named / unnamed / watcher rows with formatted uptime
    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    expect(screen.getByText("3")).toBeTruthy();
    expect(screen.getByText("dev-server")).toBeTruthy();
    expect(screen.getByText("(unnamed)")).toBeTruthy();
    expect(screen.getByText("watcher")).toBeTruthy();
    // runtime: bare value (no label), tick-computed from created_at (8040s /
    // 660s), probe snapshot when created_at is missing (45s)
    expect(screen.getByText("2h 14m")).toBeTruthy(); // 8040s
    expect(screen.getByText("45s")).toBeTruthy();
    expect(screen.getByText("11m")).toBeTruthy(); // 660s
    // created time is deliberately not shown in the panel (user correction
    // 2026-08-28) — runtime alone; the created/TTL detail lives on the
    // monitor page title bar.
    expect(screen.queryByText(/^\[\d{4}-\d{2}-\d{2} /)).toBeNull();
    // each shell row links to its monitor page /shell/{agentId}/{shellId}
    expect(screen.getByText("dev-server").closest("a")?.getAttribute("href")).toBe("/shell/1/0");
    expect(screen.getByText("watcher").closest("a")?.getAttribute("href")).toBe("/shell/1/2");

    // config overlay: string passthrough + JSON.stringify of a number
    expect(screen.getByText("llm_model")).toBeTruthy();
    expect(screen.getByText("claude-opus-4-8")).toBeTruthy();
    expect(screen.getByText("0.7")).toBeTruthy();

    // cost: 2×2 grid (the "Since spawn · N LLM calls" subtitle line was
    // removed — the grid already carries cost / calls / tokens / cache-hit)
    expect(screen.getByText("$0.4213")).toBeTruthy();
    expect(screen.getByText("1.20M / 84.0k")).toBeTruthy(); // tokens in/out combined
    expect(screen.getByText("142")).toBeTruthy(); // LLM calls
    expect(screen.getByText("91.70%")).toBeTruthy(); // cache hit

    // activity: TPS leads the grid, followed by LM / exec / idle
    const activitySection = screen.getByText("Activity").closest("section");
    expect(activitySection).not.toBeNull();
    expect(within(activitySection!).getByText("TPS")).toBeTruthy();
    expect(within(activitySection!).getByText("42.5")).toBeTruthy();
    expect(screen.queryByText("Active rate")).toBeNull();
    expect(screen.queryByText("50%")).toBeNull();
    expect(screen.queryByText("LLM stage")).toBeNull();
    expect(screen.getByText("20m")).toBeTruthy(); // 1200s LM
    expect(screen.getByText("8m")).toBeTruthy(); // 450s exec
    expect(screen.getByText("Idle")).toBeTruthy();
    expect(screen.getByText("30m")).toBeTruthy(); // 3600 - 1800 idle seconds

    // liveness (merged section, Task #1195): heartbeat icon + "Liveness"
    // title; the "every 5m" interval badge and the old "Last judged" cell are
    // dropped; the heartbeat cells stay
    expect(screen.getByText("Liveness")).toBeTruthy();
    expect(screen.queryByText("State")).toBeNull();
    expect(screen.queryByText("every 5m")).toBeNull();
    expect(screen.getByText("never paused")).toBeTruthy();
  });

  it("links the active agent to its run timeline", async () => {
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    expect(screen.getByRole("link", { name: "Open run timeline" }).getAttribute("href")).toBe(
      "/insights/run/1",
    );
  });

  it("renders no Alerts section (user ruling 2026-08-29)", async () => {
    // The full total / resolved / net split lives on the Grafana tiles; the
    // inspector no longer renders the fleet Alerts block.
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    expect(screen.queryByText("Alerts")).toBeNull();
  });

  it("renders the open page title and URL without a redundant Open badge", async () => {
    const page = {
      id: 7,
      agent_id: 1,
      name: "task-dashboard",
      port: 4173,
      title: "Task dashboard",
      serve_dir: null,
      url: "http://gateway.test/pages/7-task-dashboard/",
      created_at: "2026-08-24T12:00:00Z",
      closed_at: null,
    } satisfies PageRow;
    listPages.mockResolvedValueOnce([page]);

    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText(page.title)).toBeTruthy());
    const pageSection = screen.getByText("Page").closest("section");
    expect(pageSection).not.toBeNull();
    expect(within(pageSection!).getByText(page.url)).toBeTruthy();
    expect(within(pageSection!).queryByText("Open")).toBeNull();
  });

  it("hides the Page section when no page is open", async () => {
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(listPages).toHaveBeenCalled());
    expect(screen.queryByText("Page")).toBeNull();
  });

  it("renders section skeletons instead of a single loading line on a cold split load", () => {
    getAgentInspectLive.mockReturnValue(new Promise<AgentInspectLive>(() => undefined));
    getAgentInspect.mockReturnValue(new Promise<AgentInspect>(() => undefined));

    render(<InspectorPanel agentId={1} />);

    expect(screen.queryByText("Page")).toBeNull();
    expect(screen.getByLabelText("Persistent shells loading")).toBeTruthy();
    expect(screen.getByLabelText("Liveness loading")).toBeTruthy();
    expect(screen.getByLabelText("Configuration overlay loading")).toBeTruthy();
    expect(screen.getByLabelText("Cost loading")).toBeTruthy();
    expect(screen.getByLabelText("Activity loading")).toBeTruthy();
    expect(screen.getByLabelText("Notice loading")).toBeTruthy();
    expect(screen.queryByText("Loading…")).toBeNull();
  });

  it("keeps live sections visible while the slower windowed half is pending", async () => {
    getAgentInspect.mockReturnValue(new Promise<AgentInspect>(() => undefined));

    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    expect(screen.getByText("dev-server")).toBeTruthy();
    expect(screen.getByLabelText("Cost loading")).toBeTruthy();
    expect(screen.getByLabelText("Activity loading")).toBeTruthy();
    expect(screen.queryByText("$0.4213")).toBeNull();
  });

  it("replaces the prior window with skeletons while a new window is pending", async () => {
    getAgentInspect.mockImplementation((_agentId, hours) => {
      if (hours === 24) return Promise.resolve(fixture({ window_hours: 24 }));
      return new Promise<AgentInspect>(() => undefined);
    });
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("$0.4213")).toBeTruthy());

    fireEvent.change(screen.getByLabelText("Cost + activity window"), {
      target: { value: "1" },
    });

    await waitFor(() =>
      expect(getAgentInspect).toHaveBeenCalledWith(1, 1, false, expect.any(AbortSignal)),
    );
    expect(screen.queryByText("$0.4213")).toBeNull();
    expect(screen.getByLabelText("Cost loading")).toBeTruthy();
    expect(screen.getByText("dev-server")).toBeTruthy();
  });

  it("formats token counts with B/T tiers (task #824): 2176.67M → 2.18B", async () => {
    getAgentInspect.mockResolvedValue(
      fixture({
        cost: {
          cost_usd: 0.4213,
          unpriced_calls: 0,
          llm_calls: 142,
          tokens_in: 2_176_670_000,
          tokens_out: 3_420_000,
          tokens_cached: 0,
          tokens_reasoning: 0,
          cache_hit_pct: 0,
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    // ≥1000M rolls to B, <1000M stays M — the user-reported exact case.
    expect(screen.getByText("2.18B / 3.42M")).toBeTruthy();
  });

  it("formats token counts with the T tier past 1000B (task #824)", async () => {
    getAgentInspect.mockResolvedValue(
      fixture({
        cost: {
          cost_usd: 0.0,
          unpriced_calls: 0,
          llm_calls: 0,
          tokens_in: 1_500_000_000_000,
          tokens_out: 999,
          tokens_cached: 0,
          tokens_reasoning: 0,
          cache_hit_pct: 0,
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    expect(screen.getByText("1.50T / 999")).toBeTruthy();
  });

  it("marks snapshot-less calls as N unpriced under the cost", async () => {
    // Calls without a stored usage-time price snapshot contribute 0 cost;
    // the Cost cell's sub-line surfaces the count instead of hiding it.
    getAgentInspect.mockResolvedValue(
      fixture({
        cost: {
          cost_usd: 0.0073,
          unpriced_calls: 7,
          llm_calls: 8,
          tokens_in: 2_000_000,
          tokens_out: 0,
          tokens_cached: 2_000_000,
          tokens_reasoning: 0,
          cache_hit_pct: 100,
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    expect(screen.getByText("$0.0073")).toBeTruthy();
    expect(screen.getByText("7 unpriced")).toBeTruthy();
  });

  it.each([
    [0, "—"],
    [42.54, "42.5"],
    [5_834, "5834.0"],
  ] as const)("formats TPS %s as %s", async (tps, expected) => {
    getAgentInspect.mockResolvedValue(
      fixture({ tps: { lm_stage_tps: tps, agent_lifecycle_tps: 0 } }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Activity")).toBeTruthy());
    const activitySection = screen.getByText("Activity").closest("section");
    expect(activitySection).not.toBeNull();
    expect(within(activitySection!).getByText(expected)).toBeTruthy();
  });

  it("formats durations past 24h as Xd Yh (task #824): idle 24d 3h", async () => {
    // 24d 3h = 24*86400 + 3*3600 seconds of idle (alive − active).
    getAgentInspect.mockResolvedValue(
      fixture({
        activity: {
          active_seconds: 0,
          alive_seconds: 24 * 86_400 + 3 * 3_600,
          active_rate: 0,
          llm_seconds: 3_600,
          exec_seconds: 0,
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Activity")).toBeTruthy());
    // Idle cell shows the day tier; the sub-day cells keep hour/minute form.
    expect(screen.getByText("24d 3h")).toBeTruthy();
    // "1h" appears twice: the window-selector option AND the llm_seconds cell.
    expect(screen.getAllByText("1h").length).toBeGreaterThanOrEqual(2);
  });

  it("formats shell uptime past 24h as Xd Yh (task #824)", async () => {
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        shells: [{ id: 9, name: "long-shell", created_at: null, uptime_seconds: 24 * 86_400 + 3 * 3_600 }],
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("long-shell")).toBeTruthy());
    expect(screen.getByText("24d 3h")).toBeTruthy();
  });

  it("activity durations show em dashes when alive is 0 while TPS remains visible", async () => {
    getAgentInspect.mockResolvedValue(
      fixture({ activity: { active_seconds: 0, alive_seconds: 0, active_rate: 0, llm_seconds: 0, exec_seconds: 0 } }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Activity")).toBeTruthy());
    const activitySection = screen.getByText("Activity").closest("section");
    expect(activitySection).not.toBeNull();
    expect(within(activitySection!).getAllByText("—")).toHaveLength(3);
    expect(within(activitySection!).getByText("42.5")).toBeTruthy();
  });

  it("renders the notice section when agent has an open notice", async () => {
    const createdAt = "2026-06-15T13:30:00Z";
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        notice: {
          id: 1,
          title: "Approve deploy?",
          content: "Can we push to prod?",
          priority: "P0",
          require_response: true,
          blocking: true,
          created_at: createdAt,
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Notice")).toBeTruthy());
    // Priority badge
    expect(screen.getByText("P0")).toBeTruthy();
    // Blocking tag
    expect(screen.getByText("Blocking")).toBeTruthy();
    // Type label
    expect(screen.getByText("Decision")).toBeTruthy();
    // Created time
    expect(
      screen.getByText(`${formatRelative(createdAt)}, ${formatAbsolute(createdAt)}`),
    ).toBeTruthy();
    // Title
    expect(screen.getByText("Approve deploy?")).toBeTruthy();
    // Content preview
    expect(screen.getByText("Can we push to prod?")).toBeTruthy();
  });

  it("shows 'no open notice' when notice is null", async () => {
    getAgentInspectLive.mockResolvedValue(liveFixture({ notice: null }));
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("No open notice")).toBeTruthy());
  });

  it("invalidates both inspect query halves when notice SSE arrives", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    rtlRender(
      <QueryClientProvider client={queryClient}>
        <InspectorPanel agentId={1} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());

    act(() => {
      streamHandlers.system?.({ agent_id: 1, role: "notice_posted" });
    });

    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["agent-inspect-live", 1],
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["agent-inspect", 1] });
  });

  it("notice is an interactive reply surface before the Page section", async () => {
    const page = {
      id: 7,
      agent_id: 1,
      name: "task-dashboard",
      port: 4173,
      title: "Task dashboard",
      serve_dir: null,
      url: "http://gateway.test/pages/7-task-dashboard/",
      created_at: "2026-08-24T12:00:00Z",
      closed_at: null,
    } satisfies PageRow;
    listPages.mockResolvedValueOnce([page]);
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        notice: {
          id: 5,
          title: "Approve?",
          content: "ok?",
          priority: "P1",
          require_response: true,
          blocking: false,
          created_at: "2026-06-14T12:00:00Z",
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Notice")).toBeTruthy());
    // Interactive mirror (not the old read-only card): a reply box + Dismiss.
    expect(screen.getByRole("textbox")).toBeTruthy();
    expect(screen.getByText("Dismiss")).toBeTruthy();
    // Notice is pinned above the Page section at the top of panel content.
    const notice = screen.getByText("Notice");
    const pageTitle = await screen.findByText(page.title);
    expect(
      notice.compareDocumentPosition(pageTitle) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("resolving a notice re-enables the reply box when a new notice takes its place", async () => {
    // A require_response notice, then — after the resolve's refetch — a DIFFERENT
    // open notice (the agent, woken by the reply, immediately raised another).
    const mk = (id: number, title: string) => ({
      id,
      title,
      content: null,
      priority: "P1" as const,
      require_response: true,
      blocking: false,
      created_at: "2026-06-14T12:00:00Z",
    });
    getAgentInspectLive
      .mockResolvedValueOnce(liveFixture({ notice: mk(5, "First?") }))
      .mockResolvedValue(liveFixture({ notice: mk(6, "Second?") }));
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("First?")).toBeTruthy());
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "done" } });
    fireEvent.click(screen.getByLabelText("Send answer"));

    // onResolved invalidates the inspect query → refetch swaps in the new notice.
    await waitFor(() => expect(screen.getByText("Second?")).toBeTruthy());

    // The keyed remount gives the new notice a fresh reply surface: Send is
    // disabled while empty and ENABLES on input. Without key={notice.id} the
    // prior instance's sticky `pending` would keep it disabled forever.
    const send = screen.getByLabelText("Send answer");
    expect(send.hasAttribute("disabled")).toBe(true);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "next" } });
    expect(send.hasAttribute("disabled")).toBe(false);
  });

  it("shows FYI label when notice is not require_response", async () => {
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        notice: {
          id: 2,
          title: "Milestone reached",
          content: null,
          priority: "P2",
          require_response: false,
          blocking: false,
          created_at: "2026-06-14T12:00:00Z",
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("FYI")).toBeTruthy());
    expect(screen.queryByText("Blocking")).toBeNull();
    expect(screen.getByText("P2")).toBeTruthy();
    expect(screen.getByText("Milestone reached")).toBeTruthy();
  });

  it("hides available-but-empty shells and an empty config overlay", async () => {
    getAgentInspectLive.mockResolvedValue(liveFixture({ shells: [], config_overlay: {} }));
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    expect(screen.queryByText("Persistent shells")).toBeNull();
    expect(screen.queryByText("Configuration overlay")).toBeNull();
  });

  it("does not render observation details", async () => {
    getAgentInspectLive.mockResolvedValue(liveFixture());
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Birth")).toBeTruthy());
    expect(screen.queryByText("Observation")).toBeNull();
    expect(screen.queryByText(/Machine probe:/)).toBeNull();
  });

  it("does not report an unavailable shell observation as an empty set", async () => {
    getAgentInspectLive.mockResolvedValue(liveFixture({ shells: [], shells_available: false }));
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Shell observation unavailable")).toBeTruthy());
    expect(screen.getByText("Persistent shells")).toBeTruthy();
  });

  it("renders skill-list config keys in canonical dash spelling (display_name)", async () => {
    // The runtime stores skill lists in the underscore Python projection
    // (ava_code_worktree); the overlay must present the canonical dash form
    // (ava-code-worktree) while non-skill values stay untouched.
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        config_overlay: {
          skills_to_inject_into_system_prompt: ["ava_qa_inspection", "*"],
          llm_model: "claude-opus-4-8",
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Configuration overlay")).toBeTruthy());
    // The dd renders the JSON-serialized array; the underscore projection must
    // be gone and the canonical dash spelling present.
    expect(screen.getByText('["ava-qa-inspection","*"]')).toBeTruthy();
    expect(screen.queryByText(/ava_qa_inspection/)).toBeNull();
    expect(screen.getByText("claude-opus-4-8")).toBeTruthy();
  });

  it("window selector re-queries with the chosen hours", async () => {
    // First load defaults to 24h. Switching to 1h re-scopes the request.
    getAgentInspect.mockResolvedValueOnce(fixture({ window_hours: 24 }));
    getAgentInspect.mockResolvedValue(fixture({ window_hours: 1 }));
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    expect(getAgentInspect).toHaveBeenCalledWith(1, 24, false, expect.any(AbortSignal));
    expect(screen.getByLabelText<HTMLSelectElement>("Cost + activity window").value).toBe("24");

    fireEvent.change(screen.getByLabelText("Cost + activity window"), { target: { value: "1" } });

    await waitFor(() =>
      expect(getAgentInspect).toHaveBeenCalledWith(1, 1, false, expect.any(AbortSignal)),
    );
    // The select reflects the chosen window (the cost scope line was removed).
    await waitFor(() =>
      expect(screen.getByLabelText<HTMLSelectElement>("Cost + activity window").value).toBe("1"),
    );
  });

  it("Compact window selects since_compact instead of hours", async () => {
    // First load is 24h. After selecting Compact, the mock echoes since_compact=true.
    getAgentInspect.mockResolvedValueOnce(fixture());
    getAgentInspect.mockResolvedValue(fixture({ since_compact: true }));
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());

    fireEvent.change(screen.getByLabelText("Cost + activity window"), { target: { value: "-1" } });

    await waitFor(() =>
      expect(getAgentInspect).toHaveBeenCalledWith(1, null, true, expect.any(AbortSignal)),
    );
    await waitFor(() =>
      expect(screen.getByLabelText<HTMLSelectElement>("Cost + activity window").value).toBe("-1"),
    );
  });

  it("shows an error message when the fetch fails", async () => {
    getAgentInspectLive.mockRejectedValue(new Error("boom"));
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("boom")).toBeTruthy());
  });

  it("stale refresh: a failed background poll keeps the sections and shows the stale dot (no cold-error swap)", async () => {
    // First load succeeds; a later same-query poll fails. React Query keeps the
    // last data on a refetch error, so the panel must keep the sections + flag the
    // failure with the stale dot, never replace everything with the error text
    // (that is reserved for a cold miss with no data at all).
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    getAgentInspect.mockResolvedValueOnce(fixture());
    rtlRender(
      <QueryClientProvider client={qc}>
        <InspectorPanel agentId={1} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());

    // The next poll of the SAME query fails — data is retained, error is set.
    getAgentInspect.mockRejectedValue(new Error("refresh failed"));
    await act(async () => {
      await qc.refetchQueries({ queryKey: ["agent-inspect", 1, 24] });
    });

    await waitFor(() => expect(screen.getByLabelText("Live refresh failing")).toBeTruthy());
    expect(screen.getByText("Persistent shells")).toBeTruthy();
    expect(screen.queryByText("refresh failed")).toBeNull();
  });

  it("refuses an inspect payload whose agent_id does not match the selection", async () => {
    // A malformed/misrouted response must obey the same identity boundary as
    // keepPreviousData. Rendering it as plain text would still disclose one
    // agent's inspector under another selection.
    getAgentInspectLive.mockResolvedValue(
      liveFixture({ agent_id: Number.NaN }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(getAgentInspectLive).toHaveBeenCalled());
    expect(screen.queryByText("dev-server")).toBeNull();
    await waitFor(() => expect(screen.getByText("No data")).toBeTruthy());
  });

  it("renders shell rows as plain text when a shell id is invalid", async () => {
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        shells: [
          { id: Number.NaN, name: "bad-shell", created_at: null, uptime_seconds: 10 },
        ],
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("bad-shell")).toBeTruthy());
    // The row should NOT be wrapped in an anchor
    expect(screen.getByText("bad-shell").closest("a")).toBeNull();
  });
});

describe("InspectorPanel heartbeat cells (merged into Liveness, Task #1195)", () => {
  it("shows the projected next check-in when idle and un-paused", async () => {
    // ~4.5m ahead → "in 4m" (floor of a 4.5m delta), independent of timezone.
    const nextAt = new Date(Date.now() + 270_000).toISOString();
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        heartbeat: { interval_s: 300, next_at: nextAt, paused_until: null, heartbeat_pending: false, last_pause: null },
      }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    expect(screen.getByText("in 4m")).toBeTruthy();
    expect(screen.getByText("never paused")).toBeTruthy();
  });


  it("shows the active pause window when paused", async () => {
    const pausedUntil = new Date(Date.now() + 720_000).toISOString(); // 12m
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        heartbeat: { interval_s: 900, next_at: null, paused_until: pausedUntil, heartbeat_pending: false, last_pause: null },
      }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText(/^\d{1,2}:\d{2}/)).toBeTruthy());
    // the "every N" interval badge is gone from the merged section
    expect(screen.queryByText("every 15m")).toBeNull();
  });

  it("shows 'pending' when a check-in is already queued but unprocessed", async () => {
    // idle, un-paused, but a check-in inbound is already pending (the daemon
    // won't send another) — the panel surfaces "pending" rather than projecting a stale
    // past next_at, which is the reported "one hour ago" bug.
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        heartbeat: { interval_s: 300, next_at: null, paused_until: null, heartbeat_pending: true, last_pause: null },
      }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    expect(screen.getByText("pending")).toBeTruthy();
    expect(screen.getByText("never paused")).toBeTruthy();
  });


  it("shows 'due' when the projected check-in is in the past", async () => {
    // A restarting agent's idle clock runs on while the daemon skips it, so a
    // projected next_at can land in the past — the cell must render "due",
    // never "4m ago" for a *next* heartbeat (the "one hour ago" bug family).
    const nextAt = new Date(Date.now() - 240_000).toISOString(); // 4m overdue
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        heartbeat: { interval_s: 300, next_at: nextAt, paused_until: null, heartbeat_pending: false, last_pause: null },
      }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    expect(screen.getByText("due")).toBeTruthy();
  });

  it("shows an em dash for a running agent plus the last pause from history", async () => {
    const at = new Date(Date.now() - 330_000).toISOString(); // ~5.5m ago → "5m ago"
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        heartbeat: {
          interval_s: 300,
          next_at: null,
          paused_until: null,
          heartbeat_pending: false,
          last_pause: { at, duration_s: 1800 },
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    const livenessSection = screen.getByText("Liveness").closest("section");
    expect(livenessSection).not.toBeNull();
    expect(within(livenessSection!).getByText("—")).toBeTruthy();
    expect(screen.getByText("5m ago · 30m")).toBeTruthy();
  });
});

describe("InspectorPanel birth (cell in the merged Liveness section, Task #1195)", () => {
  it("birth cell shows relative + absolute, anchored to spawned_at", async () => {
    // spawned_at is the agent's true birth (written once at spawn); started_at
    // is the per-process boot time refreshed on every restart. The birth cell
    // must display the former, so a restart / cluster update leaves it
    // unchanged. The two instants are a day apart so their absolute strings
    // can never collide in any runner timezone.
    const spawned = "2026-06-14T12:00:00Z";
    for (const started of ["2026-06-14T12:00:05Z", "2026-07-30T09:00:00Z"]) {
      cleanup();
      getAgentInspectLive.mockResolvedValue(
        liveFixture({ spawned_at: spawned, started_at: started }),
      );
      render(<InspectorPanel agentId={1} />);
      await waitFor(() => expect(screen.getByText("Birth")).toBeTruthy());
      // One line inside the cell: "relative, absolute" (user ruling 2026-08-24:
      // relative + absolute joined by a comma on a single line, e.g.
      // "3d ago, 2026-08-21 17:02:31 GMT+8"). Assert against the shared
      // helper's own output — not a reimplementation of it (tz audit, 2026-08:
      // the prior version of this test re-derived the formatting and asserted
      // against itself, so a regression in the component's actual formatting
      // call would never fail it).
      expect(
        screen.getByText(`${formatRelative(spawned)}, ${formatAbsolute(spawned)}`),
      ).toBeTruthy();
      // …anchored to the spawn time, not the (possibly refreshed) started_at.
      expect(
        screen.queryByText(`${formatRelative(started)}, ${formatAbsolute(started)}`),
      ).toBeNull();
    }
  });
});


describe("InspectorPanel desktop", () => {
  beforeEach(() => {
    isLargeMock.mockReturnValue(true);
  });

  it("fills its resizable side panel without an overlay or backdrop", async () => {
    getAgentInspect.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    const aside = screen.getByRole("complementary");
    const classes = aside.className.split(" ");
    expect(classes).toContain("flex");
    expect(classes).toContain("w-full");
    expect(classes).toContain("border-l");
    expect(classes).not.toContain("fixed");
    expect(classes).not.toContain("absolute");
    expect(document.querySelector('div[aria-hidden="true"]')).toBeNull();
  });

  it("closes when clicking the X button", async () => {
    getAgentInspect.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Close inspector" }));
    expect(toggle).toHaveBeenCalledOnce();
  });

  it("Escape does not close the panel on desktop", async () => {
    getAgentInspect.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    fireEvent.keyDown(window, { key: "Escape" });
    expect(toggle).not.toHaveBeenCalled();
  });

  it("does not close when clicking outside the desktop side panel", async () => {
    getAgentInspect.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    fireEvent.mouseDown(document.body);
    expect(toggle).not.toHaveBeenCalled();
  });

  it("renders nothing while closed", () => {
    panelState.open = false;
    const { container } = render(<InspectorPanel agentId={1} />);
    expect(container.querySelector("aside")).toBeNull();
    expect(getAgentInspect).not.toHaveBeenCalled();
    expect(getAgentInspectLive).not.toHaveBeenCalled();
  });
});

describe("InspectorPanel mobile", () => {
  beforeEach(() => {
    isLargeMock.mockReturnValue(false);
  });

  it("renders as a full-screen overlay on mobile", async () => {
    getAgentInspect.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByLabelText("Close inspector")).toBeTruthy());
    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    const aside = screen.getByRole("complementary");
    expect(aside.parentElement?.className.split(" ")).toEqual(
      expect.arrayContaining(["fixed", "inset-0", "z-50", "flex"]),
    );
    expect(document.querySelector('div[aria-hidden="true"]')).toBeTruthy();
  });

  it("closes when clicking the backdrop", async () => {
    getAgentInspect.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    // Click the backdrop (the div with aria-hidden="true" inside the overlay)
    const backdrop = document.querySelector('[aria-hidden="true"]');
    expect(backdrop).toBeTruthy();
    fireEvent.click(backdrop!);
    expect(toggle).toHaveBeenCalled();
  });

  it("closes when clicking the X button (task #793 — the mobile overlay must be closable)", async () => {
    getAgentInspect.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    // The X in the mobile header is the only always-reachable way back to the
    // timeline: the overlay covers the page header (InspectorToggle) and the
    // backdrop sits behind the full-width panel.
    const btn = screen.getByRole("button", { name: "Close inspector" });
    fireEvent.click(btn);
    expect(toggle).toHaveBeenCalled();
  });

  it("header shows no agent id — the timeline header already shows it (task #709)", async () => {
    getAgentInspect.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    const header = screen.getByText("Inspector", { selector: "span" });
    expect(header.textContent).toBe("Inspector");
  });

  it("renders nothing while closed (overlay removed)", () => {
    panelState.open = false;
    const { container } = render(<InspectorPanel agentId={1} />);
    expect(container.querySelector("aside")).toBeNull();
    expect(getAgentInspect).not.toHaveBeenCalled();
    expect(getAgentInspectLive).not.toHaveBeenCalled();
  });

  it("multiple hidden inspector observers issue zero inspect requests", () => {
    panelState.open = false;
    render(
      <>
        <InspectorPanel agentId={1} />
        <InspectorPanel agentId={1} />
      </>,
    );
    expect(getAgentInspect).not.toHaveBeenCalled();
    expect(getAgentInspectLive).not.toHaveBeenCalled();
  });

  it("multiple open inspector observers share one initial request", async () => {
    getAgentInspect.mockResolvedValue(fixture());
    render(
      <>
        <InspectorPanel agentId={1} />
        <InspectorPanel agentId={1} />
      </>,
    );

    await waitFor(() => expect(screen.getAllByText("Persistent shells")).toHaveLength(2));
    expect(getAgentInspect).toHaveBeenCalledOnce();
    expect(getAgentInspectLive).toHaveBeenCalledOnce();
  });

  it("aborts both in-flight inspect requests when the panel closes", async () => {
    let windowedSignal: AbortSignal | undefined;
    let liveSignal: AbortSignal | undefined;
    getAgentInspect.mockImplementation((_agentId, _hours, _sinceCompact, signal) => {
      windowedSignal = signal;
      return new Promise<AgentInspect>(() => undefined);
    });
    getAgentInspectLive.mockImplementation((_agentId, signal) => {
      liveSignal = signal;
      return new Promise<AgentInspectLive>(() => undefined);
    });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = rtlRender(
      <QueryClientProvider client={qc}>
        <InspectorPanel agentId={1} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(windowedSignal).toBeDefined());
    expect(liveSignal).toBeDefined();

    panelState.open = false;
    view.rerender(
      <QueryClientProvider client={qc}>
        <InspectorPanel agentId={1} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(windowedSignal?.aborted).toBe(true));
    expect(liveSignal?.aborted).toBe(true);
  });

  it("aborts the previous agent's inspect request before switching", async () => {
    const signals = new Map<number, AbortSignal | undefined>();
    getAgentInspect.mockImplementation((agentId, _hours, _sinceCompact, signal) => {
      signals.set(agentId, signal);
      return new Promise<AgentInspect>(() => undefined);
    });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = rtlRender(
      <QueryClientProvider client={qc}>
        <InspectorPanel agentId={1} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(signals.get(1)).toBeDefined());

    view.rerender(
      <QueryClientProvider client={qc}>
        <InspectorPanel agentId={2} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(signals.get(2)).toBeDefined());
    expect(signals.get(1)?.aborted).toBe(true);
  });
});

describe("InspectorToggle", () => {
  it("renders and toggles on click", () => {
    render(<InspectorToggle />);
    const btn = screen.getByRole("button", { name: "Close inspector" });
    fireEvent.click(btn);
    expect(toggle).toHaveBeenCalledOnce();
  });

  it("chevron points left when open and right when closed (2026-08-24 ruling)", () => {
    render(<InspectorToggle />);
    const openButton = screen.getByRole("button", { name: "Close inspector" });
    const openChevrons = [...openButton.querySelectorAll("svg path")]
      .map((p) => p.getAttribute("d"))
      .filter((d) => d?.startsWith("m"));
    expect(openChevrons).toContain("m15 18-6-6 6-6");

    cleanup();
    panelState.open = false;
    render(<InspectorToggle />);
    const closedButton = screen.getByRole("button", { name: "Open inspector" });
    const closedChevrons = [...closedButton.querySelectorAll("svg path")]
      .map((p) => p.getAttribute("d"))
      .filter((d) => d?.startsWith("m"));
    expect(closedChevrons).toContain("m9 18 6-6-6-6");
  });

  it("does not prefetch while closed, even on pointer intent", () => {
    panelState.open = false;
    getAgentInspect.mockResolvedValue(fixture());
    render(<InspectorToggle />);
    const btn = screen.getByRole("button", { name: "Open inspector" });
    expect(getAgentInspect).not.toHaveBeenCalled();
    expect(getAgentInspectLive).not.toHaveBeenCalled();
    fireEvent.pointerEnter(btn);
    fireEvent.focus(btn);
    expect(getAgentInspect).not.toHaveBeenCalled();
    expect(getAgentInspectLive).not.toHaveBeenCalled();
  });
});

describe("InspectorPanel manual refresh", () => {
  it("the header refresh button re-fires the inspect fetch (no fast poll behind it)", async () => {
    getAgentInspect.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    const callsAfterOpen = getAgentInspect.mock.calls.length;
    const liveCallsAfterOpen = getAgentInspectLive.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "Refresh inspector data" }));
    await waitFor(() =>
      expect(getAgentInspect.mock.calls.length).toBe(callsAfterOpen + 1),
    );
    expect(getAgentInspectLive.mock.calls.length).toBe(liveCallsAfterOpen + 1);
  });
});


describe("InspectorPanel liveness (merged section, Task #1195)", () => {
  it("omits the redundant online state cell and the old last-judged cell", async () => {
    getAgentInspectLive.mockResolvedValue(
      liveFixture({ liveness_state: "online", last_probe_at: "2026-08-12T05:00:00Z" }),
    );
    const { container } = render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    expect(screen.queryByText("online")).toBeNull();
    expect(container.querySelector(".text-destructive")).toBeNull();
    // "Last judged" deleted (user ruling 2026-08-12, Task #1195)
    expect(screen.queryByText("Last judged")).toBeNull();
  });

  it("uses offline state without rendering a redundant state cell", async () => {
    getAgentInspectLive.mockResolvedValue(
      liveFixture({ liveness_state: "offline", last_probe_at: null }),
    );
    const { container } = render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    expect(screen.queryByText("offline")).toBeNull();
    expect(container.querySelector(".text-destructive")).not.toBeNull();
    expect(screen.queryByText("never")).toBeNull();
  });
});

describe("InspectorPanel agent switch (task #1939)", () => {
  it("refetches inspect data on hot switch-back while open", async () => {
    getAgentInspectLive.mockImplementation((id) => Promise.resolve(liveFixture({ agent_id: id })));
    getAgentInspect.mockImplementation((id) => Promise.resolve(fixture({ agent_id: id })));
    // Mirror the app's global 5min staleTime: with the default 0 every key
    // change would refetch anyway and the regression (a hot switch-back
    // serves the previous visit's cache with no refetch) would be invisible.
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 5 * 60_000 } },
    });
    const { rerender } = rtlRender(
      <QueryClientProvider client={qc}>
        <InspectorPanel agentId={1} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(getAgentInspectLive).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(getAgentInspect).toHaveBeenCalledTimes(1));

    // Switch to a cold agent: the new key fetches on its own.
    rerender(
      <QueryClientProvider client={qc}>
        <InspectorPanel agentId={2} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(getAgentInspectLive).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(getAgentInspect).toHaveBeenCalledTimes(2));

    // Hot switch-back (agent 1's cache is not yet stale): without the
    // invalidate-on-transition the mounted observer would keep the previous
    // visit's cached snapshot and never refetch until the next 60s interval
    // tick.
    rerender(
      <QueryClientProvider client={qc}>
        <InspectorPanel agentId={1} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(getAgentInspectLive).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(getAgentInspect).toHaveBeenCalledTimes(3));
  });
});
