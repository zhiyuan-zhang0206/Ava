// Memory graph page tests: keep network at the API boundary and assert the page
// renders the graph counts, legend, and basic SVG nodes from seeded data.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { MemoryGraphResponse } from "@/lib/types";

const { mockGetMemoryGraph } = vi.hoisted(() => ({
  mockGetMemoryGraph: vi.fn<() => Promise<MemoryGraphResponse>>(),
}));
vi.mock("@/lib/api", () => ({ api: { getMemoryGraph: mockGetMemoryGraph } }));

import MemoryGraphPage from "./page";

afterEach(cleanup);

function seed(): MemoryGraphResponse {
  return {
    nodes: [
      {
        id: "/",
        path: "/",
        title: "memory",
        kind: "folder",
        description: null,
        tags: [],
        primary_tag: "",
        timestamp: null,
        ava_agent: null,
        ava_machine: null,
      },
      {
        id: "alpha.md",
        path: "alpha.md",
        title: "Alpha",
        kind: "note",
        description: "First note",
        tags: ["user-profile", "tech-ops"],
        primary_tag: "user-profile",
        timestamp: "2026-06-18T10:00:00Z",
        ava_agent: "7",
        ava_machine: "test-host",
      },
      {
        id: "beta.md",
        path: "beta.md",
        title: "Beta",
        kind: "note",
        description: null,
        tags: ["tech-ops"],
        primary_tag: "tech-ops",
        timestamp: null,
        ava_agent: null,
        ava_machine: null,
      },
    ],
    edges: [
      { source: "alpha.md", target: "beta.md", kind: "reference" },
      { source: "alpha.md", target: "/", kind: "containment" },
      { source: "beta.md", target: "/", kind: "containment" },
    ],
    warnings: [],
  };
}

function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryGraphPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("Memory graph page", () => {
  it("data renders counts, legend, and graph nodes", async () => {
    mockGetMemoryGraph.mockResolvedValue(seed());
    wrap();

    await waitFor(() => expect(screen.getByText("Alpha")).toBeTruthy());

    expect(screen.getByText("2 notes")).toBeTruthy();
    expect(screen.getByText("1 folder")).toBeTruthy();
    expect(screen.getByText("3 edges")).toBeTruthy();
    expect(screen.getByText("user-profile")).toBeTruthy();
    expect(screen.getByText("tech-ops")).toBeTruthy();
    expect(screen.getByTestId("memory-node-alpha.md")).toBeTruthy();
    expect(screen.getByTestId("memory-node-beta.md")).toBeTruthy();
    expect(screen.getByTestId("memory-node-/")).toBeTruthy();
  });

  it("renders containment edges solid and reference edges weak (dashed)", async () => {
    mockGetMemoryGraph.mockResolvedValue(seed());
    const { container } = wrap();
    await waitFor(() => screen.getByText("Alpha"), { timeout: 4000 });

    const graphSvg = container.querySelector(
      'svg[aria-label="Memory note graph"]',
    )!;
    const lines = graphSvg.querySelectorAll("line");
    const dashed = [...lines].filter((line) =>
      line.hasAttribute("stroke-dasharray"),
    );
    // The one cross-reference edge is dashed and thin; the two containment
    // edges stay solid.
    expect(dashed).toHaveLength(1);
    expect(dashed[0].getAttribute("stroke-dasharray")).toBe("2 4");
    expect(lines.length - dashed.length).toBe(2);
  });

  it("wheel zoom is not capped (scale can exceed the old 4x limit)", async () => {
    mockGetMemoryGraph.mockResolvedValue(seed());
    const { container } = wrap();
    await waitFor(() => screen.getByText("Alpha"), { timeout: 4000 });

    const svg = container.querySelector('svg[aria-label="Memory note graph"]')!;
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

  it("error renders a quiet failure state", async () => {
    mockGetMemoryGraph.mockRejectedValue(new Error("gateway down"));
    wrap();

    await waitFor(() => expect(screen.getByText(/Couldn.t load memory graph/)).toBeTruthy());
    expect(screen.queryByText("Alpha")).toBeNull();
  });
});
