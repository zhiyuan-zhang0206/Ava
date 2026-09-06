// GraphView render tests — the force-directed fleet graph.
//
// The d3-force layout is a real, time-driven simulation (a black-box physics
// loop on requestAnimationFrame); these tests don't pin exact coordinates. They
// drive the real simulation just far enough to settle node positions, then
// assert the SVG the component renders off those positions:
//   - nodes / edges paint (incl. the message / fork / spawn edge variants);
//   - hovering a node highlights it;
//   - single-clicking selects (no navigation); double-clicking opens the agent;
//   - empty graph shows the loading / empty / error placeholder.
//
// useFleetGraph and next/navigation are mocked so the graph is fed directly and
// router.push is observable. The pure structure helpers have their own tests in
// fleet-graph.test.ts.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { FleetGraph, FleetGraphEdge, FleetGraphNode } from "@/lib/types";
import type { FleetGraphResult } from "@/lib/use-fleet-graph";

import {
  mockSetSettingCalls,
  resetMockSettings,
} from "@/test-support/user-settings-mock";

import { GraphView } from "./graph-view";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

vi.mock("@/lib/use-user-settings", () => import("@/test-support/user-settings-mock"));

// useFleetGraph is exercised on its own in use-fleet-graph.test.ts; here we feed
// GraphView a fixed graph so the render is deterministic.
const useFleetGraph = vi.fn<() => FleetGraphResult>();
vi.mock("@/lib/use-fleet-graph", () => ({
  useFleetGraph: () => useFleetGraph(),
}));

function node(agent_id: number, over: Partial<FleetGraphNode> = {}): FleetGraphNode {
  return {
    agent_id,
    label: null,
    status: "running",
    liveness_state: "online",
    spawner: "user",
    machine: "test",
    node_score: 0,
    total_tokens: 0,
    ...over,
  };
}

function edge(
  from_agent: number,
  to_agent: number,
  event_type: FleetGraphEdge["event_type"],
  over: Partial<FleetGraphEdge> = {},
): FleetGraphEdge {
  return {
    from_agent,
    to_agent,
    event_type,
    weight: 1,
    event_count: 1,
    last_seen_at: "2026-06-17T00:00:00Z",
    ...over,
  };
}

// A graph with: a central node (#1) wired by a spawn + a fork + several message
// edges (so every edge style paints), and an idling node (#2).
function renderGraph(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function queryNodeLabel(id: number): HTMLElement | null {
  return (
    screen
      .queryAllByText(`#${id}`)
      .find((element) => element.tagName.toLowerCase() === "tspan") ?? null
  );
}

function getNodeLabel(id: number): HTMLElement {
  const label = queryNodeLabel(id);
  if (!label) throw new Error(`node label #${id} not rendered`);
  return label;
}

function richGraph(): FleetGraph {
  const seen = "2026-06-17T00:00:00Z";
  const nodes: FleetGraphNode[] = [
    node(1, { label: "alpha", node_score: 50_000, total_tokens: 1_500_000 }),
    node(2, { status: "idling" }),
    node(3, {}),
    ...[4, 5, 6, 7, 8, 9].map((id) => node(id)),
  ];
  const edges: FleetGraphEdge[] = [
    edge(1, 2, "spawn"),
    edge(1, 3, "fork", { last_seen_at: seen }),
    ...[4, 5, 6, 7, 8, 9].map((id) =>
      edge(1, id, "message", { weight: 5, last_seen_at: seen }),
    ),
  ];
  return {
    nodes,
    edges,
    stale: false,
    truncated: false,
    telemetry_stale: false,
    snapshot_at: null,
  };
}

function ok(
  graph: Omit<FleetGraph, "stale" | "truncated" | "telemetry_stale" | "snapshot_at"> & {
    stale?: boolean;
    truncated?: boolean;
    telemetry_stale?: boolean;
    snapshot_at?: string | null;
  },
): FleetGraphResult {
  return {
    graph: { stale: false, truncated: false, telemetry_stale: false, snapshot_at: null, ...graph },
    loading: false,
    error: false,
  };
}

beforeEach(() => {
  push.mockReset();
  useFleetGraph.mockReset();
  resetMockSettings();
});

afterEach(cleanup);

describe("GraphView", () => {
  it("renders nodes and edges; selects on click, opens on double-click", async () => {
    useFleetGraph.mockReturnValue(ok(richGraph()));

    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />);

    // The simulation seeds positions asynchronously (tick -> rAF). Wait for the
    // node labels to land, which means positions populated and the SVG painted.
    const label = await waitFor(() => getNodeLabel(1), { timeout: 4000 });

    expect(screen.getByText("9 nodes · 8 edges")).toBeTruthy();

    // Single-click selects the node (toggle on); it does NOT navigate.
    const group = label.closest("g")!;
    fireEvent.click(group);
    expect(push).not.toHaveBeenCalled();

    // Double-click opens the agent's conversation.
    fireEvent.doubleClick(group);
    expect(push).toHaveBeenCalledWith("/?agent_id=1");
  });

  it("has only a reset zoom control (no +/- buttons)", async () => {
    useFleetGraph.mockReturnValue(ok(richGraph()));
    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />);
    await waitFor(() => getNodeLabel(1), { timeout: 4000 });

    expect(screen.getByLabelText("Reset zoom")).toBeTruthy();
    expect(screen.queryByLabelText("Zoom in")).toBeNull();
    expect(screen.queryByLabelText("Zoom out")).toBeNull();
  });

  it("explains status colors without an activity-score sizing legend", () => {
    useFleetGraph.mockReturnValue(ok(richGraph()));
    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />);

    const legend = screen.getByLabelText("Agent graph legend");
    for (const label of ["Running", "Idling", "Terminated", "Offline"]) {
      expect(legend.textContent).toContain(label);
    }
    expect(legend.textContent).not.toContain("size = activity score");
  });

  it("shows the hover card with full node identity even when zoom hides labels", async () => {
    // The old native <title> tooltip (whose appearance the browser deferred)
    // is gone; the instant hover card carries the identity instead — visible
    // at any zoom level.
    useFleetGraph.mockReturnValue(
      ok({ nodes: [node(1, { label: "alpha" }), node(2)], edges: [] }),
    );
    const { container } = renderGraph(
      <GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />,
    );
    const label = await waitFor(() => getNodeLabel(1), { timeout: 4000 });
    const group = label.closest("g")!;

    // No native <title> remains in the SVG — the delayed tooltip is gone.
    expect(container.querySelectorAll("svg title").length).toBe(0);

    const svg = container.querySelector("svg")!;
    Object.defineProperty(svg, "createSVGPoint", {
      value: () => ({ x: 0, y: 0, matrixTransform: () => ({ x: 200, y: 200 }) }),
    });
    for (let i = 0; i < 8; i += 1) {
      fireEvent.wheel(svg, { deltaY: 100, clientX: 200, clientY: 200 });
    }

    await waitFor(() => expect(queryNodeLabel(1)).toBeNull());

    fireEvent.mouseEnter(group);
    const card = await screen.findByRole("tooltip");
    expect(card.textContent).toContain("alpha");
    expect(card.textContent).toContain("#1");
  });

  it("selecting a node focuses it (non-identity transform); reset restores identity", async () => {
    useFleetGraph.mockReturnValue(ok(richGraph()));
    const { container } = renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />);

    // Node 2 has only one edge (→ node 1), so the neighbor bounding box is a
    // subset of the layout — zoom scale > 1 (non-identity).
    const label = await waitFor(() => getNodeLabel(2), { timeout: 4000 });
    // The content zoom layer is the only top-level <g> child of the <svg>.
    const zoomLayer = container.querySelector("svg > g")!;
    expect(zoomLayer.getAttribute("transform")).toBe("translate(0,0) scale(1)");

    // Clicking a node centers + zooms to include it and its neighbors.
    fireEvent.click(label.closest("g")!);
    await waitFor(() => {
      const t = zoomLayer.getAttribute("transform")!;
      expect(t).not.toBe("translate(0,0) scale(1)");
    });

    // Reset returns to the fit-to-content identity transform.
    fireEvent.click(screen.getByLabelText("Reset zoom"));
    await waitFor(() =>
      expect(zoomLayer.getAttribute("transform")).toBe("translate(0,0) scale(1)"),
    );
  });

  it("wheel zoom is not capped (scale can exceed the old 4x limit)", async () => {
    useFleetGraph.mockReturnValue(ok(richGraph()));
    const { container } = renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />);
    await waitFor(() => getNodeLabel(1), { timeout: 4000 });

    const svg = container.querySelector("svg")!;
    const zoomLayer = container.querySelector("svg > g")!;
    // happy-dom drops WheelEvent client coordinates and its SVGPoint lacks
    // matrixTransform, so supply the cursor location requested by the event.
    Object.defineProperty(svg, "createSVGPoint", {
      value: () => ({ x: 0, y: 0, matrixTransform: () => ({ x: 200, y: 200 }) }),
    });
    for (let i = 0; i < 12; i += 1) {
      fireEvent.wheel(svg, { deltaY: -100, clientX: 200, clientY: 200 });
    }

    await waitFor(() => {
      const scale = Number(zoomLayer.getAttribute("transform")?.match(/scale\(([^)]+)\)/)?.[1]);
      expect(scale).toBeGreaterThan(4);
    });
  });

  it("clicking an already-selected node is a no-op (does not deselect)", async () => {
    useFleetGraph.mockReturnValue(ok(richGraph()));
    const onSelect = vi.fn();

    // Render with agent 1 already selected.
    renderGraph(<GraphView selectedAgentId={1} onSelectAgent={onSelect} />);

    const label = await waitFor(() => getNodeLabel(1), { timeout: 4000 });
    const group = label.closest("g")!;

    // Click the already-selected node.
    fireEvent.click(group);
    // The click must not call onSelectAgent (no deselect, no re-select).
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("renders every edge gray (message and lineage alike)", async () => {
    // User ruling 2026-08-05: no blue message edges — one muted gray for all.
    useFleetGraph.mockReturnValue(
      ok({
        nodes: [node(1), node(2)],
        edges: [
          edge(1, 2, "spawn"),
          edge(1, 2, "message"),
        ],
      }),
    );
    const { container } = renderGraph(
      <GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />,
    );
    await waitFor(() => getNodeLabel(1), { timeout: 4000 });

    const svg = container.querySelector(
      'svg[aria-label="Fleet relationship graph"]',
    )!;
    const lines = svg.querySelectorAll("line");
    expect(lines.length).toBeGreaterThanOrEqual(2);
    for (const l of lines) {
      expect(l.getAttribute("class")).toBe("text-muted-foreground");
    }
  });

  it("scales message-edge opacity across the graph's weight range", async () => {
    useFleetGraph.mockReturnValue(
      ok({
        nodes: [node(1), node(2), node(3)],
        edges: [
          edge(1, 2, "message", { weight: 1 }),
          edge(1, 3, "message", { weight: 2 }),
        ],
      }),
    );
    const { container } = renderGraph(
      <GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />,
    );
    await waitFor(() => getNodeLabel(1), { timeout: 4000 });

    const opacities = Array.from(
      container.querySelectorAll('svg[aria-label="Fleet relationship graph"] line'),
      (line) => Number(line.getAttribute("stroke-opacity")),
    ).sort((a, b) => a - b);
    expect(opacities).toHaveLength(2);
    expect(opacities[0]).toBeCloseTo(0.545);
    expect(opacities[1]).toBeCloseTo(0.85);
  });

  it("scales edge thickness by weight by default", async () => {
    useFleetGraph.mockReturnValue(
      ok({
        nodes: [node(1), node(2), node(3)],
        edges: [
          edge(1, 2, "message", { weight: 1 }),
          edge(1, 3, "message", { weight: 4 }),
        ],
      }),
    );
    const { container } = renderGraph(
      <GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />,
    );
    await waitFor(() => getNodeLabel(1), { timeout: 4000 });

    const widths = Array.from(
      container.querySelectorAll('svg[aria-label="Fleet relationship graph"] line'),
      (line) => Number(line.getAttribute("stroke-width")),
    ).sort((a, b) => a - b);
    expect(widths[0]).toBeCloseTo(1.8);
    expect(widths[1]).toBeCloseTo(3);
  });

  it("renders uniform base widths when edge-weight thickness is off", async () => {
    resetMockSettings({ "display.graph_edge_weight": false });
    useFleetGraph.mockReturnValue(
      ok({
        nodes: [node(1), node(2), node(3)],
        edges: [
          edge(1, 2, "message", { weight: 1 }),
          edge(1, 3, "message", { weight: 4 }),
        ],
      }),
    );
    const { container } = renderGraph(
      <GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />,
    );
    await waitFor(() => getNodeLabel(1), { timeout: 4000 });

    const widths = Array.from(
      container.querySelectorAll('svg[aria-label="Fleet relationship graph"] line'),
      (line) => Number(line.getAttribute("stroke-width")),
    );
    expect(widths).toEqual([0.6, 0.6]);
  });

  it("persists the edge-weight thickness toggle in user settings", async () => {
    useFleetGraph.mockReturnValue(ok(richGraph()));
    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />);
    fireEvent.click(screen.getByLabelText("Graph layout settings"));

    const toggle = await screen.findByRole("switch", {
      name: "Scale edge thickness by weight",
    });
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(toggle);
    expect(mockSetSettingCalls()).toContainEqual({
      key: "display.graph_edge_weight",
      value: false,
    });

    fireEvent.click(screen.getByText("Reset all"));
    expect(mockSetSettingCalls()).toContainEqual({
      key: "display.graph_edge_weight",
      value: true,
    });
    expect(toggle.getAttribute("aria-checked")).toBe("true");
  });

  it("drops terminated nodes and their edges before rendering (task #1104)", async () => {
    // User ruling 2026-08-09: terminated agents never appear in the agents
    // graph — the component filters by liveness FIRST (mirroring the sidebar
    // tree), so even a backend leak of a terminated row cannot paint it or
    // its edges. A live node whose lineage partner is terminated renders
    // without that edge.
    useFleetGraph.mockReturnValue(
      ok({
        nodes: [
          node(1, { label: "live" }),
          node(2, { label: "dead", status: "terminated" }),
          node(3, { label: "idling", status: "idling" }),
        ],
        edges: [
          edge(2, 1, "spawn"), // touches the terminated node — dropped
          edge(1, 3, "spawn"), // live-live — kept
        ],
      }),
    );
    const { container } = renderGraph(
      <GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />,
    );
    await waitFor(() => getNodeLabel(1), { timeout: 4000 });

    expect(queryNodeLabel(2)).toBeNull(); // terminated never renders
    expect(getNodeLabel(3)).toBeTruthy(); // idling is live
    expect(screen.getByText("2 nodes · 1 edges")).toBeTruthy();
    const svg = container.querySelector(
      'svg[aria-label="Fleet relationship graph"]',
    )!;
    expect(svg.querySelectorAll("line").length).toBe(1); // only live-live edge
  });

  it("shows rendered-set counts and the empty state when every node is terminated", () => {
    useFleetGraph.mockReturnValue(
      ok({
        nodes: [node(1, { status: "terminated" }), node(2, { status: "terminated" })],
        edges: [edge(1, 2, "spawn")],
      }),
    );

    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />);

    expect(screen.getByText("0 nodes · 0 edges")).toBeTruthy();
    expect(screen.getByText("No agents to graph.")).toBeTruthy();
  });

  it("renders an offline projected transition node in muted gray", async () => {
    useFleetGraph.mockReturnValue(
      ok({
        nodes: [
          node(1, {
            label: "offline-transition",
            status: "idling",
            liveness_state: "offline",
          }),
        ],
        edges: [],
        stale: false,
      }),
    );
    const { container } = renderGraph(
      <GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />,
    );

    const label = await waitFor(() => getNodeLabel(1), { timeout: 4000 });
    const nodeGroup = label.closest("g")!;
    expect(nodeGroup.querySelector("circle")?.getAttribute("class")).toContain(
      "text-muted-foreground",
    );
    // The hover card spells the projected transition out as "Offline".
    fireEvent.mouseEnter(nodeGroup);
    const card = await screen.findByRole("tooltip");
    expect(card.textContent).toContain("Offline");
    expect(container.querySelectorAll("svg title").length).toBe(0);
  });

  it("shows the stale snapshot age for a non-empty fallback graph", () => {
    const snapshotAt = new Date(Date.now() - 12 * 60 * 1000).toISOString();
    useFleetGraph.mockReturnValue(
      ok({
        nodes: [node(1)],
        edges: [],
        stale: true,
        snapshot_at: snapshotAt,
      }),
    );

    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />);

    expect(screen.getByRole("status").textContent).toBe("Stale — snapshot from 12m ago");
  });

  it("does not flag a fresh graph as stale", () => {
    useFleetGraph.mockReturnValue(ok({ nodes: [node(1)], edges: [], stale: false }));

    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />);

    expect(screen.queryByRole("status")).toBeNull();
  });

  it("shows a telemetry warning without labeling a fresh graph stale", () => {
    useFleetGraph.mockReturnValue(
      ok({ nodes: [node(1)], edges: [], telemetry_stale: true }),
    );

    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />);

    expect(screen.getByRole("status").textContent).toBe(
      "Telemetry degraded — updates may lag",
    );
  });

  it("marks a fresh graph whose Loki edge response was truncated", () => {
    useFleetGraph.mockReturnValue(ok({ nodes: [node(1)], edges: [], truncated: true }));

    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />);

    expect(screen.getByText("Truncated — edge limit reached")).toBeTruthy();
  });

  it("merges multiple lineage kinds per pair into one edge (no duplicate React keys)", async () => {
    // The backend returns separate edges per event kind (spawn / fork /
    // resurrect) for the same pair; GraphView collapses them to the shared
    // "lineage" kind. Without merging, the same (from,to,kind) pair yields
    // duplicate React keys, which make reconciliation orphan <line> nodes on
    // every layout tick — the reported "extra dangling edges" bug.
    useFleetGraph.mockReturnValue(
      ok({
        nodes: [node(1), node(2), node(3)],
        edges: [
          edge(1, 2, "spawn"),
          edge(1, 2, "resurrect"), // same pair, second lineage kind
          edge(1, 3, "spawn"),
          edge(1, 3, "fork"), // same pair — fork must keep its dashed styling
        ],
      }),
    );
    const { container } = renderGraph(
      <GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />,
    );

    await waitFor(() => getNodeLabel(1), { timeout: 4000 });

    const svg = container.querySelector(
      'svg[aria-label="Fleet relationship graph"]',
    )!;
    const lines = svg.querySelectorAll("line");
    // 2 pairs -> 2 lineage lines (not 4, and not an accumulating pile).
    expect(lines.length).toBe(2);
    // The spawn+fork pair keeps fork styling; the spawn+resurrect pair does not.
    expect(svg.querySelectorAll('line[stroke-dasharray="4 3"]').length).toBe(1);
  });

  it("shows the hover card instantly on mouseenter and hides it on mouseleave", async () => {
    useFleetGraph.mockReturnValue(
      ok({ nodes: [node(1, { label: "alpha", node_score: 12_345_678 })], edges: [] }),
    );
    const { container } = renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />);
    const label = await waitFor(() => getNodeLabel(1), { timeout: 4000 });
    const group = label.closest("g")!;

    // The card is present synchronously — no delay timer, no debounce
    // (the old native title waited on the browser's tooltip timer).
    // Pin a roomy non-capped geometry: the beside-node card hides the moment
    // the pointer leaves the node. (jsdom's all-zero boxes land every card
    // in the height-capped state, where a grace window legitimately delays
    // the hide so the scrollable content stays reachable.)
    const svg = container.querySelector('svg[aria-label="Fleet relationship graph"]')!;
    const canvas = svg.parentElement!;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 0, width: 600, height: 400 }),
    );
    vi.spyOn(group, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 100, y: 100, width: 36, height: 36 }),
    );
    const restoreCard = mockCardSize(200, 100);
    try {
      fireEvent.mouseEnter(group);
      expect(screen.queryByRole("tooltip")).not.toBeNull();
      const card = screen.getByRole("tooltip");
      expect(card.textContent).toContain("alpha");
      expect(card.textContent).toContain("Agent #1");
      expect(card.textContent).toContain("Running");
      expect(card.textContent).toContain("Activity score: 12.35M");

      // Leaving the node dismisses the card.
      fireEvent.mouseLeave(group);
      expect(screen.queryByRole("tooltip")).toBeNull();
    } finally {
      restoreCard();
    }
  });

  it("empty graph (not loading, no error) shows the empty placeholder", () => {
    useFleetGraph.mockReturnValue({
      graph: {
        nodes: [],
        edges: [],
        stale: false,
        truncated: false,
        telemetry_stale: false,
        snapshot_at: null,
      },
      loading: false,
      error: false,
    });

    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />);

    expect(screen.getByText("No agents to graph.")).toBeTruthy();
    expect(screen.getByText("0 nodes · 0 edges")).toBeTruthy();
  });

  it("empty graph while loading shows the loading placeholder", () => {
    useFleetGraph.mockReturnValue({
      graph: {
        nodes: [],
        edges: [],
        stale: false,
        truncated: false,
        telemetry_stale: false,
        snapshot_at: null,
      },
      loading: true,
      error: false,
    });

    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />);

    expect(screen.getByText("Loading...")).toBeTruthy();
  });

  it("error (endpoint absent / gateway down) shows the unavailable placeholder", () => {
    useFleetGraph.mockReturnValue({
      graph: {
        nodes: [],
        edges: [],
        stale: false,
        truncated: false,
        telemetry_stale: false,
        snapshot_at: null,
      },
      loading: false,
      error: true,
    });

    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />);

    expect(screen.getByText("Graph unavailable.")).toBeTruthy();
  });
});

/** Mock the hover card's offsetWidth/offsetHeight (the role="tooltip"
 *  element) at the given size; every other element keeps its real values.
 *  Returns a restore function. */
function mockCardSize(width: number, height: number): () => void {
  const proto = HTMLElement.prototype;
  const widthDesc = Object.getOwnPropertyDescriptor({ offsetWidth: 0 }, "offsetWidth");
  const heightDesc = Object.getOwnPropertyDescriptor({ offsetHeight: 0 }, "offsetHeight");
  const isCard = function (this: HTMLElement) {
    return this.getAttribute("role") === "tooltip";
  };
  Object.defineProperty(proto, "offsetWidth", {
    configurable: true,
    get(this: HTMLElement) {
      if (isCard.call(this)) return width;
      // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment -- DOM descriptor getters are untyped in lib.dom; narrowed immediately below
      const real = widthDesc?.get?.call(this);
      return typeof real === "number" ? real : 0;
    },
  });
  Object.defineProperty(proto, "offsetHeight", {
    configurable: true,
    get(this: HTMLElement) {
      if (isCard.call(this)) return height;
      // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment -- DOM descriptor getters are untyped in lib.dom; narrowed immediately below
      const real = heightDesc?.get?.call(this);
      return typeof real === "number" ? real : 0;
    },
  });
  return () => {
    if (widthDesc) Object.defineProperty(proto, "offsetWidth", widthDesc);
    if (heightDesc) Object.defineProperty(proto, "offsetHeight", heightDesc);
  };
}
