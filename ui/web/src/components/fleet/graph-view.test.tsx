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

import { resetMockSettings } from "@/test-support/user-settings-mock";

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
  return { nodes, edges, stale: false, truncated: false };
}

function ok(
  graph: Omit<FleetGraph, "stale" | "truncated"> & { stale?: boolean; truncated?: boolean },
): FleetGraphResult {
  return { graph: { stale: false, truncated: false, ...graph }, loading: false, error: false };
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
    const label = await waitFor(() => screen.getByText("#1"), { timeout: 4000 });

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
    await waitFor(() => screen.getByText("#1"), { timeout: 4000 });

    expect(screen.getByLabelText("Reset zoom")).toBeTruthy();
    expect(screen.queryByLabelText("Zoom in")).toBeNull();
    expect(screen.queryByLabelText("Zoom out")).toBeNull();
  });

  it("selecting a node focuses it (non-identity transform); reset restores identity", async () => {
    useFleetGraph.mockReturnValue(ok(richGraph()));
    const { container } = renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />);

    // Node 2 has only one edge (→ node 1), so the neighbor bounding box is a
    // subset of the layout — zoom scale > 1 (non-identity).
    const label = await waitFor(() => screen.getByText("#2"), { timeout: 4000 });
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

  it("clicking an already-selected node is a no-op (does not deselect)", async () => {
    useFleetGraph.mockReturnValue(ok(richGraph()));
    const onSelect = vi.fn();

    // Render with agent 1 already selected.
    renderGraph(<GraphView selectedAgentId={1} onSelectAgent={onSelect} />);

    const label = await waitFor(() => screen.getByText("#1"), { timeout: 4000 });
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
    await waitFor(() => screen.getByText("#1"), { timeout: 4000 });

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
    await waitFor(() => screen.getByText("#1"), { timeout: 4000 });

    const opacities = Array.from(
      container.querySelectorAll('svg[aria-label="Fleet relationship graph"] line'),
      (line) => Number(line.getAttribute("stroke-opacity")),
    ).sort((a, b) => a - b);
    expect(opacities).toHaveLength(2);
    expect(opacities[0]).toBeCloseTo(0.545);
    expect(opacities[1]).toBeCloseTo(0.85);
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
    await waitFor(() => screen.getByText("#1"), { timeout: 4000 });

    expect(screen.queryByText("#2")).toBeNull(); // terminated never renders
    expect(screen.getByText("#3")).toBeTruthy(); // hibernating is live
    const svg = container.querySelector(
      'svg[aria-label="Fleet relationship graph"]',
    )!;
    expect(svg.querySelectorAll("line").length).toBe(1); // only live-live edge
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

    const label = await waitFor(() => screen.getByText("#1"), { timeout: 4000 });
    const nodeGroup = label.closest("g")!;
    expect(nodeGroup.querySelector("circle")?.getAttribute("class")).toContain(
      "text-muted-foreground",
    );
    expect(container.querySelector("title")?.textContent).toContain("Offline");
  });

  it("flags a non-empty graph served as stale", () => {
    useFleetGraph.mockReturnValue(ok({ nodes: [node(1)], edges: [], stale: true }));

    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />);

    expect(screen.getByRole("status").textContent).toBe("Stale — last known graph");
  });

  it("does not flag a fresh graph as stale", () => {
    useFleetGraph.mockReturnValue(ok({ nodes: [node(1)], edges: [], stale: false }));

    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />);

    expect(screen.queryByRole("status")).toBeNull();
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

    await waitFor(() => screen.getByText("#1"), { timeout: 4000 });

    const svg = container.querySelector(
      'svg[aria-label="Fleet relationship graph"]',
    )!;
    const lines = svg.querySelectorAll("line");
    // 2 pairs -> 2 lineage lines (not 4, and not an accumulating pile).
    expect(lines.length).toBe(2);
    // The spawn+fork pair keeps fork styling; the spawn+resurrect pair does not.
    expect(svg.querySelectorAll('line[stroke-dasharray="4 3"]').length).toBe(1);
  });

  it("empty graph (not loading, no error) shows the empty placeholder", () => {
    useFleetGraph.mockReturnValue({ graph: { nodes: [], edges: [], stale: false, truncated: false }, loading: false, error: false });

    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />);

    expect(screen.getByText("No agents to graph.")).toBeTruthy();
    expect(screen.getByText("0 nodes · 0 edges")).toBeTruthy();
  });

  it("empty graph while loading shows the loading placeholder", () => {
    useFleetGraph.mockReturnValue({ graph: { nodes: [], edges: [], stale: false, truncated: false }, loading: true, error: false });

    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />);

    expect(screen.getByText("Loading...")).toBeTruthy();
  });

  it("error (endpoint absent / gateway down) shows the unavailable placeholder", () => {
    useFleetGraph.mockReturnValue({ graph: { nodes: [], edges: [], stale: false, truncated: false }, loading: false, error: true });

    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />);

    expect(screen.getByText("Graph unavailable.")).toBeTruthy();
  });
});
