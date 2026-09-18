import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RunTimelineResponse, UserSettingListResponse } from "@/lib/types";

const { getRunTimeline, getSettings } = vi.hoisted(() => ({
  getRunTimeline: vi.fn<
    (
      agentId: number,
      options?: {
        from?: string;
        to?: string;
        level?: "turn" | "bucket";
        bucket?: string;
        session?: "compact" | "current";
      },
    ) => Promise<RunTimelineResponse>
  >(),
  getSettings: vi.fn<() => Promise<UserSettingListResponse>>(),
}));

vi.mock("@/lib/api", () => ({ api: { getRunTimeline, getSettings } }));

import RunTimelinePage from "./page";

const NOW = new Date("2026-09-05T14:26:00.000Z");

// P4-1 trail scope (#4023): a pending stretch gives the chart a focusable
// block, so a double click can push a crumb before an identity switch.
const pendingResponse: RunTimelineResponse = {
  agent_id: 42,
  window: { from: "2026-09-05T14:00:00.000Z", to: "2026-09-05T14:26:00.000Z" },
  meta: {
    n_turns: 1,
    wall_span_s: 1560,
    active_s: 4,
    tokens_in: 120,
    tokens_out: 12,
    cost_usd: 0.02,
    n_exec_failed: 0,
    n_compact: 1,
    n_restart: 0,
    fallback_turns: 0,
    unmatched_turns: 0,
  },
  rows: [
    {
      turn: 1,
      n_turns: 1,
      start: "2026-09-05T14:00:00.000Z",
      end: "2026-09-05T14:00:04.000Z",
      active_s: 4,
      trace_id: null,
      checkpoint_id: null,
      ok: true,
      llm: {
        calls: 1,
        in_total: 120,
        cache_read: 0,
        out_total: 12,
        reasoning: 0,
        latency_ms: 1500,
        cost_usd: 0.02,
        model: "deepseek-flash",
      },
      execs: [],
      anomalies: [],
      tags: [],
    },
  ],
  events: [],
  boundaries: {
    initialize_turn: 1,
    last_before_compact_turn: 1,
    post_window_turns: 0,
    has_activity_after_window: false,
  },
  pending: [{ start: "2026-09-05T14:05:00.000Z", end: "2026-09-05T14:20:00.000Z" }],
};

// P4-2b (#4023): the same shape with raw-context messages (chars sum 1000),
// so the character axis is available.
const messagesResponse: RunTimelineResponse = {
  ...pendingResponse,
  messages: [
    { key: "c.0", idx: 0, ts: null, kind: "prompt", source: null, chars: 400, parts: [{ kind: "prompt", chars: 400 }] },
    {
      key: "c.1",
      idx: 1,
      ts: "2026-09-05T14:05:00.000Z",
      kind: "ai",
      source: null,
      chars: 300,
      parts: [
        { kind: "think", chars: 100 },
        { kind: "text", chars: 200 },
      ],
    },
    { key: "c.2", idx: 2, ts: "2026-09-05T14:10:00.000Z", kind: "inbound", source: "user", chars: 100, parts: [{ kind: "inbound", chars: 100 }] },
    { key: "c.3", idx: 3, ts: "2026-09-05T14:20:00.000Z", kind: "exec", source: null, chars: 200, parts: [{ kind: "out", chars: 200 }] },
  ],
  messages_truncated: false,
};

function tickTexts(container: HTMLElement): (string | null)[] {
  return Array.from(container.querySelectorAll("[data-timeline-tick]"), (tick) => tick.textContent);
}

function render() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return rtlRender(
    <QueryClientProvider client={queryClient}>
      <RunTimelinePage params={Promise.resolve({ agentId: "42" })} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
  getRunTimeline.mockReset();
  getRunTimeline.mockReturnValue(new Promise(() => undefined));
  getSettings.mockReset();
  getSettings.mockResolvedValue({ settings: [] });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("RunTimelinePage initial window", () => {
  it("does not request the timeline while settings are pending", async () => {
    getSettings.mockReturnValue(new Promise(() => undefined));

    const { getByRole } = render();

    await waitFor(
      () => {
        expect(getSettings).toHaveBeenCalledTimes(1);
        expect(
          getByRole("heading", { name: "Run timeline — agent 42" }),
        ).toBeTruthy();
      },
      { timeout: 500 },
    );
    vi.advanceTimersByTime(60_000);

    expect(getRunTimeline).not.toHaveBeenCalled();
  });

  it("requests the most recent thirty minutes by default", async () => {
    render();

    await waitFor(() =>
      expect(getRunTimeline).toHaveBeenCalledWith(42, {
        from: "2026-09-05T13:56:00.000Z",
        to: "2026-09-05T14:26:00.000Z",
        session: "compact",
      }),
    );
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
  });

  it("requests the full session after the user resets the window", async () => {
    const { getByRole } = render();

    await waitFor(() => expect(getRunTimeline).toHaveBeenCalledTimes(1));

    fireEvent.click(getByRole("button", { name: "Reset window" }));

    await waitFor(() =>
      expect(getRunTimeline).toHaveBeenNthCalledWith(2, 42, { session: "compact" }),
    );
    expect(getRunTimeline).toHaveBeenCalledTimes(2);
    const resetOptions = getRunTimeline.mock.calls[1]?.[1];
    expect(resetOptions).not.toHaveProperty("from");
    expect(resetOptions).not.toHaveProperty("to");
  });

  it("uses the configured positive window duration", async () => {
    getSettings.mockResolvedValue({
      settings: [
        {
          key: "display.run_timeline_window_hours",
          value: 4,
          updated_at: "2026-09-05T14:00:00.000Z",
        },
      ],
    });

    render();

    await waitFor(() =>
      expect(getRunTimeline).toHaveBeenCalledWith(42, {
        from: "2026-09-05T10:26:00.000Z",
        to: "2026-09-05T14:26:00.000Z",
        session: "compact",
      }),
    );
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
  });

  it.each([0, -1, Number.NaN, "4"])("falls back to thirty minutes for invalid value %s", async (value) => {
    getSettings.mockResolvedValue({
      settings: [
        {
          key: "display.run_timeline_window_hours",
          value,
          updated_at: "2026-09-05T14:00:00.000Z",
        },
      ],
    });

    render();

    await waitFor(() =>
      expect(getRunTimeline).toHaveBeenCalledWith(42, {
        from: "2026-09-05T13:56:00.000Z",
        to: "2026-09-05T14:26:00.000Z",
        session: "compact",
      }),
    );
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
  });
});

describe("compare entry", () => {
  it("preselects this agent for the compare view", async () => {
    const { getByRole } = render();

    await waitFor(() =>
      expect(getByRole("link", { name: "Compare agents" }).getAttribute("href")).toBe(
        "/insights/compare?agents=42",
      ),
    );
  });
});

describe("trail scope (P4-1)", () => {
  it("clears the focus trail when the session changes", async () => {
    getRunTimeline.mockResolvedValue(pendingResponse);
    const { getByRole, queryByTestId } = render();

    fireEvent.doubleClick(
      await screen.findByRole("button", { name: "Pending layer segment" }),
    );
    expect(await screen.findByTestId("timeline-crumbs")).toBeTruthy();

    fireEvent.click(getByRole("button", { name: "Current session" }));

    await waitFor(() => expect(queryByTestId("timeline-crumbs")).toBeNull());
  });

  it("clears the focus trail when the resolved agentId changes in place", async () => {
    getRunTimeline.mockResolvedValue(pendingResponse);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { rerender, queryByTestId } = rtlRender(
      <QueryClientProvider client={queryClient}>
        <RunTimelinePage params={Promise.resolve({ agentId: "42" })} />
      </QueryClientProvider>,
    );

    fireEvent.doubleClick(
      await screen.findByRole("button", { name: "Pending layer segment" }),
    );
    expect(await screen.findByTestId("timeline-crumbs")).toBeTruthy();

    rerender(
      <QueryClientProvider client={queryClient}>
        <RunTimelinePage params={Promise.resolve({ agentId: "43" })} />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(queryByTestId("timeline-crumbs")).toBeNull());
  });
});

describe("context axis (P4-2b)", () => {
  it("disables the characters axis without message data", async () => {
    getRunTimeline.mockResolvedValue(pendingResponse);
    const { getByRole } = render();

    await screen.findByLabelText("Run timeline chart");
    const contextButton = getByRole("button", { name: "Characters" }) as HTMLButtonElement;
    expect(contextButton.disabled).toBe(true);
    expect(contextButton.title).toBe("No message data in this window");
  });

  it("switches to the character axis and drives its viewport with zero refetches", async () => {
    getRunTimeline.mockResolvedValue(messagesResponse);
    const { container, getByRole } = render();

    await waitFor(() =>
      expect((getByRole("button", { name: "Characters" }) as HTMLButtonElement).disabled).toBe(false),
    );
    fireEvent.click(getByRole("button", { name: "Characters" }));
    expect(getByRole("button", { name: "Characters" }).getAttribute("aria-pressed")).toBe("true");
    // Switching the projection is a pure view change: no request.
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
    expect(tickTexts(container)).toEqual(["0", "200", "400", "600", "800", "1.0k"]);

    // +/- drive the local char viewport on the context axis: still no request.
    fireEvent.click(getByRole("button", { name: "Zoom in" }));
    expect(tickTexts(container)).toEqual(["200", "300", "400", "500", "600", "700", "800"]);
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
  });

  it("pushes a character-range crumb on strip double-click and clears it on reset", async () => {
    getRunTimeline.mockResolvedValue(messagesResponse);
    const { container, getByRole, queryByTestId } = render();

    await waitFor(() =>
      expect((getByRole("button", { name: "Characters" }) as HTMLButtonElement).disabled).toBe(false),
    );
    fireEvent.click(getByRole("button", { name: "Characters" }));

    // Message c.1 spans chars 400-700; the focus pads by half its width.
    fireEvent.doubleClick(screen.getAllByTestId("strip-message-button")[1]);
    const crumbs = await screen.findByTestId("timeline-crumbs");
    expect(crumbs.textContent).toContain("Message 1");
    expect(crumbs.textContent).toContain("250\u2013850 chars");
    expect(tickTexts(container)).toEqual(["300", "400", "500", "600", "700", "800"]);

    // "Back to the full axis" is a pure viewport reset on the context axis.
    fireEvent.click(getByRole("button", { name: "Reset window" }));
    await waitFor(() => expect(queryByTestId("timeline-crumbs")).toBeNull());
    expect(tickTexts(container)).toEqual(["0", "200", "400", "600", "800", "1.0k"]);
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
  });

  it("clamps the character viewport when the message total shrinks in a same-window refresh", async () => {
    getRunTimeline.mockResolvedValue(messagesResponse);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { container, getByRole } = rtlRender(
      <QueryClientProvider client={queryClient}>
        <RunTimelinePage params={Promise.resolve({ agentId: "42" })} />
      </QueryClientProvider>,
    );

    await waitFor(() =>
      expect((getByRole("button", { name: "Characters" }) as HTMLButtonElement).disabled).toBe(false),
    );
    fireEvent.click(getByRole("button", { name: "Characters" }));
    fireEvent.doubleClick(screen.getAllByTestId("strip-message-button")[1]);
    await screen.findByTestId("timeline-crumbs");
    expect(tickTexts(container)).toEqual(["300", "400", "500", "600", "700", "800"]);

    // The refresh keeps the window but drops the last two messages (chars sum
    // 1000 to 700): the viewport clamps and the char-range crumb stays.
    const firstCall = getRunTimeline.mock.calls[0] as
      | [number, { from?: string; to?: string }]
      | undefined;
    const options = firstCall?.[1] ?? {};
    queryClient.setQueryData(
      ["run-timeline", 42, options.from ?? null, options.to ?? null, "compact", "turn"],
      { ...messagesResponse, messages: messagesResponse.messages?.slice(0, 2) },
    );

    await waitFor(() =>
      expect(tickTexts(container)).toEqual(["100", "200", "300", "400", "500", "600", "700"]),
    );
    expect(screen.getByTestId("timeline-crumbs").textContent).toContain("Message 1");
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
  });

  it("keeps the characters axis disabled in bucket mode without message data", async () => {
    getSettings.mockResolvedValue({
      settings: [
        {
          key: "display.run_timeline_window_hours",
          value: 24,
          updated_at: "2026-09-05T14:00:00.000Z",
        },
      ],
    });
    getRunTimeline.mockResolvedValue({
      ...pendingResponse,
      rows: [{ ...pendingResponse.rows[0], turn: null, n_turns: 12 }],
    });
    const { getByRole } = render();

    await waitFor(() =>
      expect(getRunTimeline).toHaveBeenCalledWith(42, expect.objectContaining({ level: "bucket" })),
    );
    await screen.findByLabelText("Run timeline chart");
    const contextButton = getByRole("button", { name: "Characters" }) as HTMLButtonElement;
    expect(contextButton.disabled).toBe(true);
  });

  it("resets the character viewport and trail when the data window changes", async () => {
    getRunTimeline.mockResolvedValueOnce(messagesResponse).mockResolvedValueOnce({
      ...messagesResponse,
      window: { from: "2026-09-05T13:26:00.000Z", to: "2026-09-05T14:26:00.000Z" },
    });
    const { container, getByRole, queryByTestId } = render();

    await waitFor(() =>
      expect((getByRole("button", { name: "Characters" }) as HTMLButtonElement).disabled).toBe(false),
    );
    fireEvent.click(getByRole("button", { name: "Characters" }));
    fireEvent.doubleClick(screen.getAllByTestId("strip-message-button")[1]);
    await screen.findByTestId("timeline-crumbs");
    expect(tickTexts(container)).toEqual(["300", "400", "500", "600", "700", "800"]);

    // A window preset is a data control: it refetches, and the new data
    // resets the view to the full axis and clears the char-range trail.
    fireEvent.click(getByRole("button", { name: "1h" }));
    await waitFor(() => expect(getRunTimeline).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(queryByTestId("timeline-crumbs")).toBeNull());
    expect(tickTexts(container)).toEqual(["0", "200", "400", "600", "800", "1.0k"]);
  });
});
