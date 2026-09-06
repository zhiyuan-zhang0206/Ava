// AgentRow tree-guide render tests — verify the 1px vertical line shows
// / hides correctly based on depth and ancestorsIsLast.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render as rtlRender,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { AgentRow } from "./agent-row";
import type { AgentRow as AgentRowType } from "@/lib/types";

const prefetchMocks = vi.hoisted(() => ({
  getAgentInspect: vi
    .fn<
      (
        agentId: number,
        hours?: number | null,
        sinceCompact?: boolean,
        signal?: AbortSignal,
      ) => Promise<unknown>
    >()
    .mockResolvedValue({}),
  getAgentInspectLive: vi
    .fn<(agentId: number, signal?: AbortSignal) => Promise<unknown>>()
    .mockResolvedValue({}),
}));
vi.mock("@/lib/api", () => ({
  api: {
    getSystemStatus: vi.fn().mockRejectedValue(new Error("no network in tests")),
    getSettings: vi.fn().mockRejectedValue(new Error("no network in tests")),
    putSetting: vi.fn().mockRejectedValue(new Error("no network in tests")),
    getAgentInspect: prefetchMocks.getAgentInspect,
    getAgentInspectLive: prefetchMocks.getAgentInspectLive,
  },
}));

const inspectorState = { open: false, hours: 24 as number | null };
vi.mock("@/lib/inspector-panel-store", () => ({
  useInspectorOpen: () => ({ open: inspectorState.open, toggle: vi.fn() }),
  useInspectorHours: () => ({
    inspectorHours: inspectorState.hours,
    setInspectorHours: vi.fn(),
  }),
}));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  inspectorState.open = false;
  inspectorState.hours = 24;
  prefetchMocks.getAgentInspect.mockClear();
  prefetchMocks.getAgentInspectLive.mockClear();
});

// Radix ContextMenu calls pointer-capture / scroll APIs that happy-dom
// doesn't implement; stub them so the menu can open in the test DOM.
beforeAll(() => {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.setPointerCapture = () => undefined;
  Element.prototype.releasePointerCapture = () => undefined;
  Element.prototype.scrollIntoView = () => undefined;
});

// AgentRow uses useQuery(["status"]) (machine badge) and useUserSettings
// (opt-in signals) — wrap in QueryClientProvider so the hooks work. The api
// module is mocked to REJECT: no real network in tests. On CI there is no
// gateway (fetches would spray ECONNREFUSED); locally a live gateway is
// worse — real prod settings would leak into the merged settings map and
// green-wash assertions that depend on the defaults. Tests that need
// non-default status / settings seed the query cache directly.
function render(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

// Render with the user-settings cache pre-seeded — for testing opt-in
// signals (status colors / activity line / awaiting-reply badge) that are
// hidden at their quiet defaults.
function renderWithSettings(ui: React.ReactElement, settings: Record<string, unknown>) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  qc.setQueryData(["user-settings"], settings);
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function ag(agent_id: number, overrides: Partial<AgentRowType> = {}): AgentRowType {
  return {
    agent_id,
    spawner: "user",
    fork_source_agent_id: null,
    status: "running",
    pid: null,
    spawned_at: "2026-05-07T00:00:00.000000Z",
    started_at: null,
    last_active_at: "2026-05-07T00:00:00.000000Z", last_inbound_at: "2026-05-07T00:00:00.000000Z",
    label: null,
    machine: "test-machine",
    supports_vision: true,
    liveness_state: "online",
    notices_awaiting_response: [],
    unread_notice_count: 0,
    heartbeat_paused_until: null,
    ...overrides,
  };
}

const noop = () => undefined;
const baseProps = {
  label: undefined,
  active: false,
  pending: undefined,
  wide: false,
  onSelect: noop,
  onTerminate: noop,
  onForceKill: noop,
  onRestart: noop,
  onResurrect: noop,
  onFork: noop,
  onRename: noop,
  onCompact: noop,
};

function guides(container: HTMLElement) {
  return Array.from(container.querySelectorAll('[data-testid="tree-guide"]'));
}

describe("AgentRow tree-guide rendering", () => {
  it("does not show an ownership fallback when observation is missing", () => {
    render(<AgentRow {...baseProps} agent={ag(1)} depth={0} ancestorsIsLast={[]} />);
    expect(screen.queryByText("owner unknown")).toBeNull();
  });

  it("does not show stale observation details", () => {
    render(<AgentRow {...baseProps} agent={ag(1, { observation: {
      machine_probe_at: new Date(Date.now() - 180_000).toISOString(),
      machine_probe_valid_until: new Date(Date.now() - 60_000).toISOString(),
      runtime_lease_expires_at: new Date(Date.now() + 60_000).toISOString(),
      runtime_owner: "unknown",
    } })} depth={0} ancestorsIsLast={[]} />);
    expect(screen.queryByText("probe stale")).toBeNull();
  });

  it("depth=0 (top-level): renders no guide bars", () => {
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(1)} depth={0} ancestorsIsLast={[]} />,
    );
    expect(guides(container)).toHaveLength(0);
  });

  it("depth=1 self not last: draws 1 bar at d=0", () => {
    // ancestorsIsLast = [parent.isLast, self.isLast].  parent may be
    // last or not — the line at d=0 is governed by self's position.
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(2)} depth={1} ancestorsIsLast={[false, false]} />,
    );
    const bars = guides(container);
    expect(bars).toHaveLength(1);
    expect(bars[0].getAttribute("data-depth")).toBe("0");
  });

  it("depth=1 parent is last but self is not: line still drawn (parent has children → line extends)", () => {
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(2)} depth={1} ancestorsIsLast={[true, false]} />,
    );
    const bars = guides(container);
    expect(bars).toHaveLength(1);
    expect(bars[0].getAttribute("data-depth")).toBe("0");
  });

  it("depth=1 self is last: bar still drawn (every child gets a line)", () => {
    // Every child at every depth gets a vertical bar — the lineage column
    // is contiguous; isLast does not suppress the bar.
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(3)} depth={1} ancestorsIsLast={[false, true]} />,
    );
    const bars = guides(container);
    expect(bars).toHaveLength(1);
    expect(bars[0].getAttribute("data-depth")).toBe("0");
  });

  it("depth=2 ancestorsIsLast=[false, false, true]: both d=0 and d=1 drawn (every child gets a line)", () => {
    // ancestorsIsLast = [grandparent.isLast, parent.isLast, self.isLast]
    // Every depth gets a bar — isLast does not suppress any line.
    const { container } = render(
      <AgentRow
        {...baseProps}
        agent={ag(4)}
        depth={2}
        ancestorsIsLast={[false, false, true]}
      />,
    );
    const bars = guides(container);
    expect(bars).toHaveLength(2);
    expect(bars[0].getAttribute("data-depth")).toBe("0");
    expect(bars[1].getAttribute("data-depth")).toBe("1");
  });

  it("depth=2 ancestorsIsLast=[true, false, false]: both d=0 and d=1 drawn (line extends through non-last children)", () => {
    // grandparent is last → but parent is not last, so the line at d=0
    // extends from grandparent down through this branch.  parent not last
    // and self not last → line at d=1 extends as well.
    const { container } = render(
      <AgentRow
        {...baseProps}
        agent={ag(5)}
        depth={2}
        ancestorsIsLast={[true, false, false]}
      />,
    );
    const bars = guides(container);
    expect(bars).toHaveLength(2);
    expect(bars[0].getAttribute("data-depth")).toBe("0");
    expect(bars[1].getAttribute("data-depth")).toBe("1");
  });
});

describe("AgentRow inline rename accessibility", () => {
  it("names the inline rename input", () => {
    render(
      <AgentRow
        {...baseProps}
        agent={ag(1)}
        label="Focus"
        depth={0}
        ancestorsIsLast={[]}
      />,
    );

    fireEvent.doubleClick(screen.getByText("Focus"));

    expect(screen.getByLabelText("Rename agent 1")).toBeTruthy();
  });
});

describe("AgentRow typography", () => {
  it("uses sans for the row and mono only for ID and time data", () => {
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(1)} depth={0} ancestorsIsLast={[]} />,
    );
    const row = container.querySelector("li button")!;
    const id = screen.getByText("#1");
    const time = row.querySelector("span.tabular-nums")!;

    expect(row.classList).toContain("font-sans");
    expect(row.classList).not.toContain("font-mono");
    expect(id.classList).toContain("font-mono");
    expect(time.classList).toContain("font-mono");
  });
});

describe("AgentRow action button visibility", () => {
  // Alive rows carry no on-row action buttons — restart / terminate moved
  // into the right-click context menu (covered by its own describe below).
  it("alive agent: no on-row restart / terminate buttons", () => {
    const { queryByLabelText } = render(
      <AgentRow {...baseProps} agent={ag(1)} depth={0} ancestorsIsLast={[]} />,
    );
    expect(queryByLabelText(/Restart Agent #1/)).toBeNull();
    expect(queryByLabelText(/Terminate Agent #1/)).toBeNull();
  });

  // Task #723 (user ruling): the agent-switch row is a list item, not a
  // button — the global `button:not(:disabled) { cursor: pointer }` rule
  // (#709) must not give it a hand cursor. Class contract: cursor-default.
  it("agent switch row keeps the default cursor (no hand cursor)", () => {
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(1)} depth={0} ancestorsIsLast={[]} />,
    );
    const row = container.querySelector("li button");
    expect(row).not.toBeNull();
    expect(row!.className).toContain("cursor-default");
  });

  // Resurrect stays always visible (not hover-gated): mobile has no hover,
  // and it is the single primary action for a dead agent.
  it("terminated agent: resurrect button visible without hover", () => {
    const dead: AgentRowType = { ...ag(2), status: "terminated" };
    const { getByLabelText } = render(
      <AgentRow {...baseProps} agent={dead} depth={0} ancestorsIsLast={[]} />,
    );
    expect(getByLabelText(/Resurrect Agent #2/).tagName).toBe("BUTTON");
  });
});

describe("AgentRow inspector prefetch", () => {
  function rowButton(container: HTMLElement): HTMLButtonElement {
    return container.querySelector("li > button")!;
  }

  it("keeps hover at zero inspect traffic while the panel is closed", async () => {
    vi.useFakeTimers();
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(1)} depth={0} ancestorsIsLast={[]} />,
    );

    fireEvent.mouseEnter(rowButton(container));
    await act(() => vi.advanceTimersByTimeAsync(400));

    expect(prefetchMocks.getAgentInspectLive).not.toHaveBeenCalled();
    expect(prefetchMocks.getAgentInspect).not.toHaveBeenCalled();
  });

  it("debounces open-panel hover, then prefetches both query halves", async () => {
    vi.useFakeTimers();
    inspectorState.open = true;
    inspectorState.hours = 1;
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(7)} depth={0} ancestorsIsLast={[]} />,
    );

    fireEvent.mouseEnter(rowButton(container));
    await act(() => vi.advanceTimersByTimeAsync(299));
    expect(prefetchMocks.getAgentInspectLive).not.toHaveBeenCalled();
    await act(() => vi.advanceTimersByTimeAsync(1));

    expect(prefetchMocks.getAgentInspectLive).toHaveBeenCalled();
    expect(prefetchMocks.getAgentInspectLive).toHaveBeenCalledWith(
      7,
      expect.any(AbortSignal),
    );
    expect(prefetchMocks.getAgentInspect).toHaveBeenCalledWith(
      7,
      1,
      false,
      expect.any(AbortSignal),
    );
  });

  it("cancels the debounce when the pointer leaves quickly", async () => {
    vi.useFakeTimers();
    inspectorState.open = true;
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(3)} depth={0} ancestorsIsLast={[]} />,
    );
    const row = rowButton(container);

    fireEvent.mouseEnter(row);
    fireEvent.mouseLeave(row);
    await act(() => vi.advanceTimersByTimeAsync(400));

    expect(prefetchMocks.getAgentInspectLive).not.toHaveBeenCalled();
    expect(prefetchMocks.getAgentInspect).not.toHaveBeenCalled();
  });

  it("aborts both prefetched requests when the pointer leaves after they start", async () => {
    vi.useFakeTimers();
    inspectorState.open = true;
    let liveSignal: AbortSignal | undefined;
    let windowedSignal: AbortSignal | undefined;
    prefetchMocks.getAgentInspectLive.mockImplementationOnce((_agentId, signal) => {
      liveSignal = signal;
      return new Promise(() => undefined);
    });
    prefetchMocks.getAgentInspect.mockImplementationOnce(
      (_agentId, _hours, _sinceCompact, signal) => {
        windowedSignal = signal;
        return new Promise(() => undefined);
      },
    );
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(4)} depth={0} ancestorsIsLast={[]} />,
    );
    const row = rowButton(container);

    fireEvent.mouseEnter(row);
    await act(() => vi.advanceTimersByTimeAsync(300));
    expect(liveSignal?.aborted).toBe(false);
    expect(windowedSignal?.aborted).toBe(false);
    fireEvent.mouseLeave(row);
    await act(async () => Promise.resolve());

    expect(liveSignal?.aborted).toBe(true);
    expect(windowedSignal?.aborted).toBe(true);
  });

  it("aborts a started hover prefetch when the inspector closes", async () => {
    vi.useFakeTimers();
    inspectorState.open = true;
    let liveSignal: AbortSignal | undefined;
    let windowedSignal: AbortSignal | undefined;
    prefetchMocks.getAgentInspectLive.mockImplementationOnce((_agentId, signal) => {
      liveSignal = signal;
      return new Promise(() => undefined);
    });
    prefetchMocks.getAgentInspect.mockImplementationOnce(
      (_agentId, _hours, _sinceCompact, signal) => {
        windowedSignal = signal;
        return new Promise(() => undefined);
      },
    );
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const view = rtlRender(
      <QueryClientProvider client={queryClient}>
        <AgentRow {...baseProps} agent={ag(5)} depth={0} ancestorsIsLast={[]} />
      </QueryClientProvider>,
    );

    fireEvent.mouseEnter(rowButton(view.container));
    await act(() => vi.advanceTimersByTimeAsync(300));
    expect(liveSignal?.aborted).toBe(false);
    expect(windowedSignal?.aborted).toBe(false);

    inspectorState.open = false;
    view.rerender(
      <QueryClientProvider client={queryClient}>
        <AgentRow {...baseProps} agent={ag(5)} depth={0} ancestorsIsLast={[]} />
      </QueryClientProvider>,
    );

    await act(async () => Promise.resolve());
    expect(liveSignal?.aborted).toBe(true);
    expect(windowedSignal?.aborted).toBe(true);
  });

  it("prefetches immediately on select while the inspector is open", async () => {
    inspectorState.open = true;
    const onSelect = vi.fn();
    const { container } = render(
      <AgentRow
        {...baseProps}
        onSelect={onSelect}
        agent={ag(9)}
        depth={0}
        ancestorsIsLast={[]}
      />,
    );

    fireEvent.click(rowButton(container));

    await waitFor(() => expect(prefetchMocks.getAgentInspectLive).toHaveBeenCalled());
    expect(prefetchMocks.getAgentInspect).toHaveBeenCalled();
    expect(onSelect).toHaveBeenCalledOnce();
  });
});

describe("AgentRow machine badge", () => {
  // Single machine (<=1 online) → no badge; the badge only carries
  // information on multi-machine setups. Printing the same machine name
  // on 100 rows of a single-machine setup is just noise.
  it("status not loaded yet → not shown (degrades to single-machine behavior)", () => {
    const { queryByText } = render(
      <AgentRow {...baseProps} agent={ag(1)} depth={0} ancestorsIsLast={[]} />,
    );
    expect(queryByText("test-machine")).toBeNull();
  });

  // Multi-machine setup (>=2 machines): the badge shows when the user setting
  // is enabled; hides when disabled. Seed the React Query cache directly so the
  // component reads the desired state without live fetches.
  it("multi-machine + show_machine_name enabled → badge shown", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // Seed two machines into the status cache
    qc.setQueryData(["status"], {
      cluster: { machines: [{ name: "m1" }, { name: "m2" }] },
    });
    // Seed user setting enabled
    qc.setQueryData(["user-settings"], { "display.show_machine_name": true });
    const { queryByText } = rtlRender(
      <QueryClientProvider client={qc}>
        <AgentRow {...baseProps} agent={ag(1)} depth={0} ancestorsIsLast={[]} />
      </QueryClientProvider>,
    );
    expect(queryByText("test-machine")).toBeTruthy();
  });

  it("multi-machine + show_machine_name disabled → badge hidden", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(["status"], {
      cluster: { machines: [{ name: "m1" }, { name: "m2" }] },
    });
    qc.setQueryData(["user-settings"], { "display.show_machine_name": false });
    const { queryByText } = rtlRender(
      <QueryClientProvider client={qc}>
        <AgentRow {...baseProps} agent={ag(1)} depth={0} ancestorsIsLast={[]} />
      </QueryClientProvider>,
    );
    expect(queryByText("test-machine")).toBeNull();
  });

  it("multi-machine but agent.machine is 'unknown' → badge hidden", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(["status"], {
      cluster: { machines: [{ name: "m1" }, { name: "m2" }] },
    });
    qc.setQueryData(["user-settings"], { "display.show_machine_name": true });
    const { queryByText } = rtlRender(
      <QueryClientProvider client={qc}>
        <AgentRow
          {...baseProps}
          agent={ag(1, { machine: "unknown" })}
          depth={0}
          ancestorsIsLast={[]}
        />
      </QueryClientProvider>,
    );
    expect(queryByText("unknown")).toBeNull();
  });

  it("single machine + show_machine_name enabled → badge hidden (noise guard)", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(["status"], {
      cluster: { machines: [{ name: "m1" }] },
    });
    qc.setQueryData(["user-settings"], { "display.show_machine_name": true });
    const { queryByText } = rtlRender(
      <QueryClientProvider client={qc}>
        <AgentRow {...baseProps} agent={ag(1)} depth={0} ancestorsIsLast={[]} />
      </QueryClientProvider>,
    );
    expect(queryByText("test-machine")).toBeNull();
  });
});


describe("AgentRow status dot gating (display.show_agent_status)", () => {
  // The status dot is a rounded-full span directly in the row button; when the
  // setting is off it is not rendered at all (hidden entirely, not decolored).
  function statusDot(container: HTMLElement): HTMLElement | null {
    return container.querySelector("li button > span.rounded-full");
  }

  it("default (setting off): no status dot rendered at all", () => {
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(1, { status: "running" })} depth={0} ancestorsIsLast={[]} />,
    );
    expect(statusDot(container)).toBeNull();
  });

  it("default (setting off): no dot for any status (no leaked dynamic signal)", () => {
    const a = render(
      <AgentRow {...baseProps} agent={ag(1, { status: "running" })} depth={0} ancestorsIsLast={[]} />,
    );
    expect(statusDot(a.container)).toBeNull();
    cleanup();
    const b = render(
      <AgentRow {...baseProps} agent={ag(1, { status: "idling" })} depth={0} ancestorsIsLast={[]} />,
    );
    expect(statusDot(b.container)).toBeNull();
  });

  it("enabled: colored status dot", () => {
    const { container } = renderWithSettings(
      <AgentRow {...baseProps} agent={ag(1, { status: "running" })} depth={0} ancestorsIsLast={[]} />,
      { "display.show_agent_status": true },
    );
    const d = statusDot(container);
    expect(d).toBeTruthy();
    expect(d!.className).toContain("bg-sky-500");
  });

  it("offline: grey dot shown even with the status opt-in off (Task #1174)", () => {
    // A machine drop / dead process must never masquerade as present under
    // the quiet default — the offline dot is presence, not a dynamic signal.
    const { container } = render(
      <AgentRow
        {...baseProps}
        agent={ag(1, { status: "idling", liveness_state: "offline" })}
        depth={0}
        ancestorsIsLast={[]}
      />,
    );
    const d = statusDot(container);
    expect(d).toBeTruthy();
    expect(d!.className).toContain("bg-muted-foreground/50");
  });

  it("offline: grey dot overrides the status color and carries a tooltip", () => {
    const { container } = renderWithSettings(
      <AgentRow
        {...baseProps}
        agent={ag(1, { status: "idling", liveness_state: "offline" })}
        depth={0}
        ancestorsIsLast={[]}
      />,
      { "display.show_agent_status": true },
    );
    const d = statusDot(container);
    expect(d).toBeTruthy();
    expect(d!.className).toContain("bg-muted-foreground/50");
    expect(d!.className).not.toContain("bg-emerald-500");
    expect(d!.getAttribute("title")).toBeTruthy();
  });

  it("online: unaffected by the liveness field (status color as before)", () => {
    const { container } = renderWithSettings(
      <AgentRow {...baseProps} agent={ag(1, { status: "idling" })} depth={0} ancestorsIsLast={[]} />,
      { "display.show_agent_status": true },
    );
    const d = statusDot(container);
    expect(d!.className).toContain("bg-emerald-500");
  });
});

describe("AgentRow time display (display.time_mode)", () => {
  // The timestamp is the tabular-nums span in the row button.
  function timeCell(container: HTMLElement): HTMLElement | null {
    return container.querySelector("li button span.tabular-nums");
  }

  it("default (last_active): timestamp rendered", () => {
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(1, { status: "idling" })} depth={0} ancestorsIsLast={[]} />,
    );
    expect(timeCell(container)).toBeTruthy();
  });

  it("hidden: no timestamp rendered", () => {
    const { container } = renderWithSettings(
      <AgentRow {...baseProps} agent={ag(1, { status: "idling" })} depth={0} ancestorsIsLast={[]} />,
      { "display.time_mode": "hidden" },
    );
    expect(timeCell(container)).toBeNull();
  });
});

describe("AgentRow awaiting-reply badge gating (notification.awaiting_reply)", () => {
  const notice = {
    id: 7,
    title: "What next?",
    content: null,
    priority: "P1" as const,
    require_response: true,
    blocking: false,
    created_at: "2026-05-07T00:00:00Z",
  };

  it("default (setting off): no badge even with open notices", () => {
    const { queryByTitle } = render(
      <AgentRow
        {...baseProps}
        agent={ag(1, { notices_awaiting_response: [notice] })}
        depth={0}
        ancestorsIsLast={[]}
      />,
    );
    expect(queryByTitle(/waiting on you/)).toBeNull();
  });

  it("enabled: badge shows the count", () => {
    const { getByText } = renderWithSettings(
      <AgentRow
        {...baseProps}
        agent={ag(1, { notices_awaiting_response: [notice] })}
        depth={0}
        ancestorsIsLast={[]}
      />,
      { "notification.awaiting_reply": true },
    );
    expect(getByText("1").textContent).toBe("1");
  });
});

describe("AgentRow right-click context menu", () => {
  function openMenu(row: HTMLElement) {
    fireEvent.contextMenu(row);
  }

  it("alive agent: menu shows Restart / Terminate / Fork / Fork with prompt / Kill", () => {
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(1)} depth={0} ancestorsIsLast={[]} />,
    );
    openMenu(container.querySelector("li")!);
    expect(screen.getByText("Restart")).toBeTruthy();
    expect(screen.getByText("Terminate")).toBeTruthy();
    expect(screen.getByText("Fork")).toBeTruthy();
    expect(screen.getByText("Fork with prompt…")).toBeTruthy();
    expect(screen.getByText("Kill")).toBeTruthy();
    // Resurrect items are terminated-only
    expect(screen.queryByText("Resurrect")).toBeNull();
  });

  it("terminated agent: menu shows Resurrect + Resurrect with prompt, no Fork / Kill", () => {
    const dead: AgentRowType = { ...ag(2), status: "terminated" };
    const { container } = render(
      <AgentRow {...baseProps} agent={dead} depth={0} ancestorsIsLast={[]} />,
    );
    openMenu(container.querySelector("li")!);
    expect(screen.getByText("Resurrect")).toBeTruthy();
    expect(screen.getByText("Resurrect with prompt…")).toBeTruthy();
    expect(screen.queryByText("Kill")).toBeNull();
    expect(screen.queryByText("Fork")).toBeNull();
  });

  // happy-dom has no window.confirm — stub it per test (unstubbed in afterEach).
  it("Kill calls onForceKill only after the confirm is accepted", () => {
    const onForceKill = vi.fn();
    const confirmMock = vi.fn().mockReturnValue(true);
    vi.stubGlobal("confirm", confirmMock);
    const { container } = render(
      <AgentRow
        {...baseProps}
        onForceKill={onForceKill}
        agent={ag(1)}
        depth={0}
        ancestorsIsLast={[]}
      />,
    );
    openMenu(container.querySelector("li")!);
    fireEvent.click(screen.getByText("Kill"));
    expect(confirmMock).toHaveBeenCalledOnce();
    expect(onForceKill).toHaveBeenCalledOnce();
  });

  it("Kill does nothing when the confirm is dismissed", () => {
    const onForceKill = vi.fn();
    const confirmMock = vi.fn().mockReturnValue(false);
    vi.stubGlobal("confirm", confirmMock);
    const { container } = render(
      <AgentRow
        {...baseProps}
        onForceKill={onForceKill}
        agent={ag(1)}
        depth={0}
        ancestorsIsLast={[]}
      />,
    );
    openMenu(container.querySelector("li")!);
    fireEvent.click(screen.getByText("Kill"));
    expect(confirmMock).toHaveBeenCalledOnce();
    expect(onForceKill).not.toHaveBeenCalled();
  });

  it("Terminate menu item calls onTerminate (graceful)", () => {
    const onTerminate = vi.fn();
    const { container } = render(
      <AgentRow
        {...baseProps}
        onTerminate={onTerminate}
        agent={ag(1)}
        depth={0}
        ancestorsIsLast={[]}
      />,
    );
    openMenu(container.querySelector("li")!);
    fireEvent.click(screen.getByText("Terminate"));
    expect(onTerminate).toHaveBeenCalledOnce();
  });

  it("quick Fork calls onFork with no prompt (undefined)", () => {
    const onFork = vi.fn();
    const { container } = render(
      <AgentRow
        {...baseProps}
        onFork={onFork}
        agent={ag(1)}
        depth={0}
        ancestorsIsLast={[]}
      />,
    );
    openMenu(container.querySelector("li")!);
    fireEvent.click(screen.getByText("Fork"));
    expect(onFork).toHaveBeenCalledOnce();
    expect(onFork).toHaveBeenCalledWith(); // no prompt arg
  });

  // The "... with prompt" items open a PromptDialog whose submit routes to
  // onFork / onResurrect. The dialog's own logic (submit/disabled/trim) is
  // covered in agent-prompt-dialog.test.tsx; the menu→dialog focus handoff
  // is a Radix interaction not exercised here (it recurses under happy-dom;
  // it works in a real browser — marked NOT tested in the PR).
});
