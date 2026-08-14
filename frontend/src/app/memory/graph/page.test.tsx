// Memory graph page tests: keep network at the API boundary and assert the page
// renders the graph counts, legend, and basic SVG nodes from seeded data.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
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
        id: "alpha.md",
        path: "alpha.md",
        title: "Alpha",
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
        description: null,
        tags: ["tech-ops"],
        primary_tag: "tech-ops",
        timestamp: null,
        ava_agent: null,
        ava_machine: null,
      },
    ],
    edges: [{ source: "alpha.md", target: "beta.md" }],
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
    expect(screen.getByText("1 link")).toBeTruthy();
    expect(screen.getByText("user-profile")).toBeTruthy();
    expect(screen.getByText("tech-ops")).toBeTruthy();
    expect(screen.getByTestId("memory-node-alpha.md")).toBeTruthy();
    expect(screen.getByTestId("memory-node-beta.md")).toBeTruthy();
  });

  it("error renders a quiet failure state", async () => {
    mockGetMemoryGraph.mockRejectedValue(new Error("gateway down"));
    wrap();

    await waitFor(() => expect(screen.getByText(/Couldn.t load memory graph/)).toBeTruthy());
    expect(screen.queryByText("Alpha")).toBeNull();
  });
});
