import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  AgentLane,
  ClusterCurves,
  ClusterLanes,
  ClusterMessages,
  RunTimelineResponse,
} from "@/lib/contracts/types";

const { getClusterCurves, getClusterLanes, getClusterMessages, getRunTimeline, useMediaQuery } = vi.hoisted(() => ({
  getClusterCurves: vi.fn<(query: unknown) => Promise<ClusterCurves>>(),
  getClusterLanes: vi.fn<(query: unknown, level?: number | null) => Promise<ClusterLanes>>(),
  getClusterMessages: vi.fn<(query: unknown) => Promise<ClusterMessages>>(),
  getRunTimeline: vi.fn<(agentId: number, options?: { from?: string; to?: string }) => Promise<RunTimelineResponse>>(),
  useMediaQuery: vi.fn(() => false),
}));

vi.mock("@/lib/layout/use-media-query", () => ({ useMediaQuery }));
vi.mock("@/lib/transport/api", () => ({
  api: { getClusterCurves, getClusterLanes, getClusterMessages, getRunTimeline },
}));

import { ClusterView } from "./cluster-view";

const T0 = Date.parse("2026-10-04T12:00:00Z");
const iso = (minutes: number) => new Date(T0 + minutes * 60_000).toISOString();
const WINDOW = { from: iso(0), to: iso(60) };

function lane(id: number, parent: number | null, depth: number, over: Partial<AgentLane> = {}): AgentLane {
  return {
    agent_id: id,
    parent,
    kind: parent === null ? "root" : "spawn",
    depth,
    status: "idling",
    spawned_at: iso(0),
    calls: 2,
    cost_usd: 0.5,
    nodes: [],
    bars: [],
    events: [],
    ...over,
  };
}

const LANES: ClusterLanes = {
  window: WINDOW,
  level: 2,
  auto_level: true,
  levels: [
    { level: 1, nodes: 40 },
    { level: 2, nodes: 6 },
  ],
  bin_seconds: 5,
  lanes: [
    lane(10, null, 0, {
      nodes: [
        { id: 1, level: 2, parent: null, start: iso(0), end: iso(30), summary: "Planned the work\nmore" },
        { id: 2, level: 2, parent: null, start: iso(30), end: iso(60), summary: "Reviewed results" },
      ],
      bars: [{ start: iso(5), end: iso(8), calls: 3, cost_usd: 0.2, input_tokens: 900, output_tokens: 90 }],
      events: [{ ts: iso(1), kind: "spawn" }],
    }),
    lane(20, 10, 1),
    lane(30, 20, 2),
    lane(40, null, 0),
  ],
};

const CURVES: ClusterCurves = {
  window: WINDOW,
  bucket_seconds: 60,
  agent_ids: [10, 20, 30, 40],
  unpriced_calls: 2,
  buckets: [
    {
      ts: iso(5),
      costs: [
        { agent_id: 10, calls: 2, cost_usd: 0.3 },
        { agent_id: 20, calls: 1, cost_usd: 0.1 },
      ],
      active_agents: 2,
      messages: 2,
      queue_samples: 1,
      queue_p50_seconds: 1.5,
      queue_p95_seconds: 1.5,
    },
  ],
};

const MESSAGES: ClusterMessages = {
  window: WINDOW,
  total: 5,
  truncated: true,
  edges: [
    { inbound_id: 1, sender: 10, receiver: 20, sent_at: iso(10), read_at: iso(11), preview: "do it" },
    { inbound_id: 2, sender: 30, receiver: 10, sent_at: iso(20), read_at: null, preview: "done" },
    { inbound_id: 3, sender: 20, receiver: 30, sent_at: iso(25), read_at: iso(26), preview: "inside" },
  ],
};

const RUN: RunTimelineResponse = {
  agent_id: 20,
  window: WINDOW,
  lifetime: WINDOW,
  nodes: [],
  units: [],
  events: [],
  requests: [],
};

function renderView() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ClusterView root={10} window={WINDOW} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getClusterCurves.mockResolvedValue(CURVES);
  getClusterLanes.mockResolvedValue(LANES);
  getClusterMessages.mockResolvedValue(MESSAGES);
  getRunTimeline.mockResolvedValue(RUN);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ClusterView", () => {
  it("asks for the three reads of the root over the window and draws a lane per agent in tree order", async () => {
    renderView();
    const lanes = await screen.findAllByTestId("cluster-lane");
    expect(lanes.map((el) => /#\d+/.exec(el.textContent)?.[0])).toEqual(["#10", "#20", "#30", "#40"]);
    for (const read of [getClusterCurves, getClusterLanes, getClusterMessages]) {
      expect(read.mock.calls[0][0]).toMatchObject({ root: 10, from: WINDOW.from, to: WINDOW.to });
    }
    expect(screen.getByTestId("cluster-summary").textContent).toContain("4 agents");
  });

  it("draws the cost, active, message and queue curves", async () => {
    renderView();
    const cost = await screen.findByTestId("cluster-curve-cost");
    await waitFor(() => expect(cost.querySelectorAll("rect")).toHaveLength(2));
    expect(screen.getByTestId("cluster-curve-active").querySelectorAll("rect")).toHaveLength(1);
    expect(screen.getByTestId("cluster-curve-messages").querySelectorAll("rect")).toHaveLength(1);
    expect(screen.getByTestId("cluster-curve-queue").querySelectorAll("rect")).toHaveLength(2);
  });

  it("shows nodes, activity bars and lifecycle markers on the lane, with the node's first line", async () => {
    renderView();
    const first = (await screen.findAllByTestId("cluster-lane"))[0];
    expect(within(first).getAllByTestId("cluster-lane-node").map((el) => el.textContent)).toEqual([
      "Planned the work",
      "Reviewed results",
    ]);
    expect(within(first).getAllByTestId("cluster-lane-bar")).toHaveLength(1);
    expect(within(first).getAllByTestId("cluster-lane-event")).toHaveLength(1);
  });

  it("connects sender to receiver and leaves out a message inside a folded subtree", async () => {
    renderView();
    await screen.findAllByTestId("cluster-lane");
    await waitFor(() => expect(screen.getAllByTestId("cluster-edge")).toHaveLength(3));
    fireEvent.click(screen.getByRole("button", { name: "Fold the agents under agent 20" }));
    await waitFor(() => expect(screen.getAllByTestId("cluster-lane")).toHaveLength(3));
    // 20 -> 30 is now inside lane 20; 10 -> 20 and 30 -> 10 remain (30 drawn on 20).
    expect(screen.getAllByTestId("cluster-edge")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Unfold all" }));
    await waitFor(() => expect(screen.getAllByTestId("cluster-lane")).toHaveLength(4));
  });

  it("tells that messages were cut and that calls had no recorded cost", async () => {
    renderView();
    expect((await screen.findAllByRole("status")).map((el) => el.textContent)).toEqual([
      "Showing the first 3 of 5 messages in this window; zoom in to see the rest.",
      "2 LLM calls in this window have no recorded cost and are not in the cost curve.",
    ]);
  });

  it("re-reads the lanes at the chosen level", async () => {
    renderView();
    await screen.findAllByTestId("cluster-lane");
    fireEvent.change(screen.getByRole("combobox", { name: "Summary level" }), { target: { value: "1" } });
    await waitFor(() => expect(getClusterLanes.mock.calls.at(-1)?.[1]).toBe(1));
  });

  it("opens a lane into that agent's run timeline, one at a time", async () => {
    renderView();
    await screen.findAllByTestId("cluster-lane");
    fireEvent.click(screen.getByRole("button", { name: "Open the run timeline of agent 20" }));
    await screen.findByTestId("cluster-expansion");
    expect(getRunTimeline).toHaveBeenCalledWith(20, { from: WINDOW.from, to: WINDOW.to });
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Open the run timeline of agent 30" }));
    await waitFor(() => expect(getRunTimeline).toHaveBeenCalledWith(30, expect.anything()));
    await waitFor(() => expect(screen.getAllByTestId("cluster-expansion")).toHaveLength(1));
  });

  it("reports a failed load with the reason", async () => {
    getClusterLanes.mockRejectedValue(new Error("unknown agent IDs: [10]"));
    renderView();
    expect((await screen.findByRole("alert")).textContent).toContain("unknown agent IDs: [10]");
  });
});
