// InspectorPanel render + window-selector tests — verify the sections
// (shells / heartbeat / config overlay / cost) render from a mocked /inspect
// response, the empty states, and that the header window selector re-queries
// with the chosen `hours`. Also covers mobile overlay rendering.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render as rtlRender,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { InspectorPanel, InspectorToggle } from "./inspector-panel";
import type { AgentInspect } from "@/lib/types";

// vi.hoisted so the mock fn is initialized before the hoisted vi.mock factory
// runs (the factory fires during the InspectorPanel import, before module-body
// consts would otherwise initialize).
const { getAgentInspect, listPages, resolveNotice } = vi.hoisted(() => ({
  getAgentInspect:
    vi.fn<
      (agentId: number, hours?: number | null, sinceCompact?: boolean) => Promise<AgentInspect>
    >(),
  // useAgentPages fetches the open-pages list; default to none so the Page
  // section renders its empty state and these render tests stay focused on the
  // /inspect sections. The dedicated use-agent-pages.test.ts covers the fetch +
  // SSE fold.
  listPages: vi.fn(() => Promise.resolve([])),
  // Notice resolve — default to success; the notice-reply tests drive it.
  resolveNotice: vi.fn(() => Promise.resolve({ status: "ok" })),
}));
vi.mock("@/lib/api", () => ({
  api: { getAgentInspect, listPages, resolveNotice },
}));

// useAgentPages subscribes to the global SSE stream; stub it to a no-op so the
// panel renders without an <EventStreamProvider> (its page-fold behavior is
// covered in use-agent-pages.test.ts).
vi.mock("@/lib/useEventStream", () => ({
  EventStreamProvider: ({ children }: { children: React.ReactNode }) => children,
  useEventStream: () => undefined,
}));

const panelState = { open: true };
const toggle = vi.fn(() => {
  panelState.open = !panelState.open;
});
vi.mock("@/lib/inspector-panel-store", () => ({
  useInspectorOpen: () => ({ open: panelState.open, toggle }),
}));

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

afterEach(() => {
  cleanup();
  getAgentInspect.mockReset();
  resolveNotice.mockClear();
  toggle.mockReset();
  panelState.open = true;
  isLargeMock.mockReturnValue(true);
});

function render(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function fixture(overrides: Partial<AgentInspect> = {}): AgentInspect {
  return {
    agent_id: 1,
    machine: "test-host",
    liveness_state: "online",
    last_probe_at: null,
    window_hours: null,
    since_compact: false,
    shells: [
      { id: 0, name: "dev-server", created_at: "2026-06-14T12:00:00Z", uptime_seconds: 8040 },
      { id: 1, name: null, created_at: null, uptime_seconds: 45 },
      { id: 2, name: "watcher", created_at: "2026-06-14T12:00:00Z", uptime_seconds: 660 },
    ],
    config_overlay: { llm_model: "claude-opus-4-8", auto_compact_fraction: 0.7 },
    cost: {
      cost_usd: 0.4213,
      unpriced_calls: 1,
      estimated_calls: 0,
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

describe("InspectorPanel", () => {
  it("renders all sections from the /inspect response", async () => {
    getAgentInspect.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);

    // shells: count badge + named / unnamed / watcher rows with formatted uptime
    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    expect(screen.getByText("3")).toBeTruthy();
    expect(screen.getByText("dev-server")).toBeTruthy();
    expect(screen.getByText("(unnamed)")).toBeTruthy();
    expect(screen.getByText("watcher")).toBeTruthy();
    expect(screen.getByText("2h 14m")).toBeTruthy(); // 8040s
    expect(screen.getByText("45s")).toBeTruthy();
    expect(screen.getByText("11m")).toBeTruthy(); // 660s
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

    // active rate: 1800/3600 = 50%, shown in 2×2 grid with LM / exec / idle
    const activeRateEls = screen.getAllByText("Active rate");
    expect(activeRateEls.length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("50%")).toBeTruthy();
    expect(screen.getByText("20m")).toBeTruthy(); // 1200s LM
    expect(screen.getByText("8m")).toBeTruthy(); // 450s exec
    expect(screen.getByText("Idle")).toBeTruthy();
    expect(screen.getByText("30m")).toBeTruthy(); // 3600 - 1800 idle seconds

    // liveness (merged section, Task #1195): heartbeat icon + "Liveness"
    // title; the "every 5m" interval badge and the old "Last judged" cell are
    // dropped; the heartbeat cells stay
    expect(screen.getByText("Liveness")).toBeTruthy();
    expect(screen.queryByText("every 5m")).toBeNull();
    expect(screen.getByText("never paused")).toBeTruthy();
  });

  it("formats token counts with B/T tiers (task #824): 2176.67M → 2.18B", async () => {
    getAgentInspect.mockResolvedValue(
      fixture({
        cost: {
          cost_usd: 0.4213,
          unpriced_calls: 0,
          estimated_calls: 0,
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
          estimated_calls: 0,
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

  it("marks read-time-priced legacy rows as N est. under the cost (task #1273)", async () => {
    // Rows written before the price snapshot shipped are priced at read time;
    // the Cost cell's sub-line surfaces the estimate instead of hiding it.
    getAgentInspect.mockResolvedValue(
      fixture({
        cost: {
          cost_usd: 0.0073,
          unpriced_calls: 0,
          estimated_calls: 7,
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
    expect(screen.getByText("7 est.")).toBeTruthy();
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

    await waitFor(() => expect(screen.getAllByText("Active rate").length).toBeGreaterThanOrEqual(1));
    // Idle cell shows the day tier; the sub-day cells keep hour/minute form.
    expect(screen.getByText("24d 3h")).toBeTruthy();
    // "1h" appears twice: the window-selector option AND the llm_seconds cell.
    expect(screen.getAllByText("1h").length).toBeGreaterThanOrEqual(2);
  });

  it("formats shell uptime past 24h as Xd Yh (task #824)", async () => {
    getAgentInspect.mockResolvedValue(
      fixture({
        shells: [{ id: 9, name: "long-shell", created_at: null, uptime_seconds: 24 * 86_400 + 3 * 3_600 }],
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("long-shell")).toBeTruthy());
    expect(screen.getByText("24d 3h")).toBeTruthy();
  });

  it("active rate shows an em dash and no blocked-% when alive is 0", async () => {
    getAgentInspect.mockResolvedValue(
      fixture({ activity: { active_seconds: 0, alive_seconds: 0, active_rate: 0, llm_seconds: 0, exec_seconds: 0 } }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getAllByText("Active rate").length).toBeGreaterThanOrEqual(1));
    // no-life branch: all four cells show em dash
    const dashes = screen.getAllByText("—");
    expect(dashes.length).toBeGreaterThanOrEqual(4);
  });

  it("renders the notice section when agent has an open notice", async () => {
    getAgentInspect.mockResolvedValue(
      fixture({
        notice: {
          id: 1,
          title: "Approve deploy?",
          content: "Can we push to prod?",
          priority: "P0",
          require_response: true,
          blocking: true,
          created_at: "2026-06-14T12:00:00Z",
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
    // Title
    expect(screen.getByText("Approve deploy?")).toBeTruthy();
    // Content preview
    expect(screen.getByText("Can we push to prod?")).toBeTruthy();
  });

  it("shows 'no open notice' when notice is null", async () => {
    getAgentInspect.mockResolvedValue(fixture({ notice: null }));
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("No open notice")).toBeTruthy());
  });

  it("notice is an interactive reply surface sitting below TPS", async () => {
    getAgentInspect.mockResolvedValue(
      fixture({
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
    // Rendered after the TPS section (bottom of the panel).
    const tps = screen.getByText("TPS");
    const notice = screen.getByText("Notice");
    expect(
      tps.compareDocumentPosition(notice) & Node.DOCUMENT_POSITION_FOLLOWING,
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
    getAgentInspect
      .mockResolvedValueOnce(fixture({ notice: mk(5, "First?") }))
      .mockResolvedValue(fixture({ notice: mk(6, "Second?") }));
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
    getAgentInspect.mockResolvedValue(
      fixture({
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

  it("renders empty states for no shells / no overrides", async () => {
    getAgentInspect.mockResolvedValue(fixture({ shells: [], config_overlay: {} }));
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("None open")).toBeTruthy());
    expect(screen.getByText("Defaults — no overrides")).toBeTruthy();
  });

  it("renders skill-list config keys in canonical dash spelling (display_name)", async () => {
    // The runtime stores skill lists in the underscore Python projection
    // (ava_code_worktree); the overlay must present the canonical dash form
    // (ava-code-worktree) while non-skill values stay untouched.
    getAgentInspect.mockResolvedValue(
      fixture({
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
    // First load: cumulative. After selecting 24h, the mock echoes window_hours=24.
    getAgentInspect.mockResolvedValueOnce(fixture());
    getAgentInspect.mockResolvedValue(fixture({ window_hours: 24 }));
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    expect(getAgentInspect).toHaveBeenCalledWith(1, null, false);

    fireEvent.change(screen.getByLabelText("Cost + activity window"), { target: { value: "24" } });

    await waitFor(() => expect(getAgentInspect).toHaveBeenCalledWith(1, 24, false));
    // The select reflects the chosen window (the cost scope line was removed).
    await waitFor(() =>
      expect(screen.getByLabelText<HTMLSelectElement>("Cost + activity window").value).toBe("24"),
    );
  });

  it("Compact window selects since_compact instead of hours", async () => {
    // First load: cumulative. After selecting Compact, the mock echoes since_compact=true.
    getAgentInspect.mockResolvedValueOnce(fixture());
    getAgentInspect.mockResolvedValue(fixture({ since_compact: true }));
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());

    fireEvent.change(screen.getByLabelText("Cost + activity window"), { target: { value: "-1" } });

    await waitFor(() => expect(getAgentInspect).toHaveBeenCalledWith(1, null, true));
    await waitFor(() =>
      expect(screen.getByLabelText<HTMLSelectElement>("Cost + activity window").value).toBe("-1"),
    );
  });

  it("shows an error message when the fetch fails", async () => {
    getAgentInspect.mockRejectedValue(new Error("boom"));
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
      await qc.refetchQueries({ queryKey: ["agent-inspect", 1, null] });
    });

    await waitFor(() => expect(screen.getByLabelText("Live refresh failing")).toBeTruthy());
    expect(screen.getByText("Persistent shells")).toBeTruthy();
    expect(screen.queryByText("refresh failed")).toBeNull();
  });

  it("renders shell rows as plain text when agent_id is invalid", async () => {
    // agent_id is NaN — the ShellRow guard should render a <span> not a <Link>
    getAgentInspect.mockResolvedValue(
      fixture({ agent_id: Number.NaN }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("dev-server")).toBeTruthy());
    // The row should NOT be wrapped in an anchor — closest("a") returns null
    expect(screen.getByText("dev-server").closest("a")).toBeNull();
  });

  it("renders shell rows as plain text when a shell id is invalid", async () => {
    getAgentInspect.mockResolvedValue(
      fixture({
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
    getAgentInspect.mockResolvedValue(
      fixture({
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
    getAgentInspect.mockResolvedValue(
      fixture({
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
    getAgentInspect.mockResolvedValue(
      fixture({
        heartbeat: { interval_s: 300, next_at: null, paused_until: null, heartbeat_pending: true, last_pause: null },
      }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    expect(screen.getByText("pending")).toBeTruthy();
    expect(screen.getByText("never paused")).toBeTruthy();
  });

  it("shows an em dash for a running agent plus the last pause from history", async () => {
    const at = new Date(Date.now() - 330_000).toISOString(); // ~5.5m ago → "5m ago"
    getAgentInspect.mockResolvedValue(
      fixture({
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
    expect(screen.getByText("—")).toBeTruthy();
    expect(screen.getByText("5m ago · 30m")).toBeTruthy();
  });
});

// Local-timezone replicas of the component's formatters — pin the Birth
// section to spawned_at without depending on the test runner's TZ.
function formatRelativeLocal(iso: string, now: Date = new Date()): string {
  const t = new Date(iso).getTime();
  const deltaSec = Math.round((t - now.getTime()) / 1000);
  const mag = Math.abs(deltaSec);
  if (mag < 60) return "now";
  const min = Math.floor(mag / 60);
  let span: string;
  if (min < 60) span = `${min}m`;
  else if (min < 60 * 24) span = `${Math.floor(min / 60)}h`;
  else if (min < 60 * 24 * 30) span = `${Math.floor(min / 60 / 24)}d`;
  else if (min < 60 * 24 * 365) span = `${Math.floor(min / 60 / 24 / 30)}mo`;
  else span = `${Math.floor(min / 60 / 24 / 365)}y`;
  return deltaSec > 0 ? `in ${span}` : `${span} ago`;
}

function formatAbsoluteLocal(iso: string): string {
  const d = new Date(iso);
  const y = d.getFullYear();
  const mo = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  const wd = d.toLocaleDateString("en-US", { weekday: "short" });
  const h = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  const s = String(d.getSeconds()).padStart(2, "0");
  return `${y}-${mo}-${day} ${wd} ${h}:${mi}:${s}`;
}

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
      getAgentInspect.mockResolvedValue(
        fixture({ spawned_at: spawned, started_at: started }),
      );
      render(<InspectorPanel agentId={1} />);
      await waitFor(() => expect(screen.getByText("Birth")).toBeTruthy());
      // Two lines inside the cell: relative on the value line, absolute below
      // (the old single-line "1mo ago, 2026-06-14 Sun 12:00:00" moved into
      // the 2×2 grid, Task #1195).
      expect(screen.getByText(formatRelativeLocal(spawned))).toBeTruthy();
      expect(screen.getByText(formatAbsoluteLocal(spawned))).toBeTruthy();
      // …anchored to the spawn time, not the (possibly refreshed) started_at.
      expect(screen.queryByText(formatAbsoluteLocal(started))).toBeNull();
    }
  });
});


describe("InspectorPanel mobile", () => {
  beforeEach(() => {
    isLargeMock.mockReturnValue(false);
  });

  it("renders as a full-screen overlay on mobile", async () => {
    getAgentInspect.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);

    // Mobile layout: full-screen overlay with an X close button.
    await waitFor(() => expect(screen.getByLabelText("Close inspector")).toBeTruthy());
    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
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

  it("Escape closes the floating panel (user ruling 2026-08-05)", async () => {
    getAgentInspect.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    fireEvent.keyDown(window, { key: "Escape" });
    expect(toggle).toHaveBeenCalled();
  });

  it("renders nothing while closed (floating panel leaves the layout)", () => {
    panelState.open = false;
    const { container } = render(<InspectorPanel agentId={1} />);
    expect(container.querySelector("aside")).toBeNull();
  });
});

describe("InspectorToggle", () => {
  it("renders and toggles on click", () => {
    render(<InspectorToggle />);
    const btn = screen.getByRole("button", { name: "Close inspector" });
    fireEvent.click(btn);
    expect(toggle).toHaveBeenCalledOnce();
  });

  it("arrow points up toward the top bar (task #1065)", () => {
    render(<InspectorToggle />);
    const btn = screen.getByRole("button", { name: "Close inspector" });
    const chevrons = [...btn.querySelectorAll("svg path")]
      .map((p) => p.getAttribute("d"))
      .filter((d) => d?.startsWith("m"));
    // PanelTopClose renders the up-chevron "m9 16 3-3 3 3"; PanelTopOpen
    // renders the down-chevron "m15 14-3 3-3-3" — the user-facing arrow must
    // point up (user ruling 8/6, re-affirmed 2026-08-08 #1065).
    expect(chevrons).toContain("m9 16 3-3 3 3");
    expect(chevrons).not.toContain("m15 14-3 3-3-3");
  });
});


describe("InspectorPanel liveness (merged section, Task #1195)", () => {
  it("renders the merged section with the state cell and no last-judged cell", async () => {
    getAgentInspect.mockResolvedValue(
      fixture({ liveness_state: "online", last_probe_at: "2026-08-12T05:00:00Z" }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    expect(screen.getAllByText("online").length).toBeGreaterThan(0);
    // "Last judged" deleted (user ruling 2026-08-12, Task #1195)
    expect(screen.queryByText("Last judged")).toBeNull();
  });

  it("renders offline state without the old never-judged fallback", async () => {
    getAgentInspect.mockResolvedValue(
      fixture({ liveness_state: "offline", last_probe_at: null }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    expect(screen.getAllByText("offline").length).toBeGreaterThan(0);
    expect(screen.queryByText("never")).toBeNull();
  });
});
