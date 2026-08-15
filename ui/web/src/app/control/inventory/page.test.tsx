// /control/inventory page tests — loading/error tri-state + the
// cross-machine matrix render (per-machine columns, the N/M-enabled summary
// chip), the not-installed "—" cell, the can't-enable disabled cell + its
// reason, a cell click -> putInventory with the right body + machine, the
// query invalidating on settle, and the per-section install entry (each posts
// its own kind, and stays usable when the read failed).
//
// happy-dom + RTL + real QueryClient (mock at the api.{getInventory,
// putInventory} layer so useQuery / useMutation goes through its full
// lifecycle).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { InventoryAggregate, InventoryWriteResult } from "@/lib/types";

import InventoryPage from "./page";

const pushSpy = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: pushSpy }) }));

afterEach(cleanup);
// Each test spies on the shared `api` object — restore between tests so spy
// call history (and stubbed impls) don't leak across cases.
beforeEach(() => {
  vi.restoreAllMocks();
  pushSpy.mockReset();
});

function makeQc() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function wrap(ui: React.ReactElement, qc?: QueryClient) {
  const client = qc ?? makeQc();
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

const H1 = "h1";
const H2 = "h2";

// ava_code: present+enabled on h1, present+disabled on h2 -> 1/2 enabled.
// only_h1: present on h1 only -> its h2 cell is "not installed".
// mcp "needs_display": present+disabled on h1 with can_enable:false.
const AGGREGATE: InventoryAggregate = {
  machines: [H1, H2],
  unreachable: [],
  plugins: [
    {
      name: "ava_code",
      kind: "plugin",
      description: "Code execution plugin",
      hosts: {
        [H1]: { present: true, enabled: true, can_enable: null, reason: null },
        [H2]: { present: true, enabled: false, can_enable: null, reason: null },
      },
    },
    {
      name: "only_h1",
      kind: "plugin",
      description: "Only on h1",
      hosts: {
        [H1]: { present: true, enabled: false, can_enable: null, reason: null },
        [H2]: { present: false, enabled: false, can_enable: null, reason: null },
      },
    },
  ],
  mcp_servers: [
    {
      name: "needs_display",
      kind: "mcp",
      description: "Headed MCP server",
      hosts: {
        [H1]: {
          present: true,
          enabled: false,
          can_enable: false,
          reason: "requires a display",
        },
        [H2]: { present: false, enabled: false, can_enable: null, reason: null },
      },
    },
  ],
};

const OK_RESULT: InventoryWriteResult = {
  applied: true,
  plugin_results: {},
  mcp_results: {},
};

describe("InventoryPage tri-state", () => {
  it("loading shows a spinner placeholder", () => {
    vi.spyOn(api, "getInventory").mockReturnValue(
      new Promise(() => undefined /* never resolves */),
    );
    wrap(<InventoryPage />);
    // The spinner is an svg with the animate-spin class.
    expect(document.querySelector(".animate-spin")).toBeTruthy();
  });

  it("error shows a quiet line, not the raw error", async () => {
    vi.spyOn(api, "getInventory").mockRejectedValue(new Error("network down"));
    wrap(<InventoryPage />);
    // Both the Plugins and MCP matrices surface the same quiet line (one query,
    // two sections), so assert presence via getAllByText.
    await waitFor(() => expect(screen.getAllByText(/Couldn't load inventory/).length).toBeGreaterThan(0));
    expect(screen.queryByText(/network down/)).toBeNull();
  });
});

describe("InventoryPage render", () => {
  it("renders both section headers + every item row", async () => {
    vi.spyOn(api, "getInventory").mockResolvedValue(AGGREGATE);
    wrap(<InventoryPage />);
    await waitFor(() => screen.getByText("ava_code"));
    expect(screen.getByText("Plugins")).toBeTruthy();
    expect(screen.getByText("MCP servers")).toBeTruthy();
    expect(screen.getByText("only_h1")).toBeTruthy();
    expect(screen.getByText("needs_display")).toBeTruthy();
  });

  it("machine columns appear as headers", async () => {
    vi.spyOn(api, "getInventory").mockResolvedValue(AGGREGATE);
    wrap(<InventoryPage />);
    await waitFor(() => screen.getByText("ava_code"));
    // Each machine appears as a header cell.
    expect(screen.getAllByText(H1).length).toBeGreaterThan(0);
    expect(screen.getAllByText(H2).length).toBeGreaterThan(0);
  });

  it("N/M enabled chip reflects present+enabled over present", async () => {
    vi.spyOn(api, "getInventory").mockResolvedValue(AGGREGATE);
    wrap(<InventoryPage />);
    await waitFor(() => screen.getByTestId("summary-ava_code"));
    // ava_code: enabled on h1, disabled on h2, present on both -> 1/2.
    expect(screen.getByTestId("summary-ava_code").textContent).toBe("1/2 enabled");
    // only_h1: present on h1 only, disabled -> 0/1.
    expect(screen.getByTestId("summary-only_h1").textContent).toBe("0/1 enabled");
  });

  it("not-installed cell shows — for a host lacking the item", async () => {
    vi.spyOn(api, "getInventory").mockResolvedValue(AGGREGATE);
    wrap(<InventoryPage />);
    await waitFor(() => screen.getByText("only_h1"));
    // only_h1 has no h1/h2 toggle for the h2 column (not installed there).
    expect(screen.queryByTestId(`cell-only_h1-${H2}`)).toBeNull();
    // A "—" placeholder is rendered for the not-installed cell.
    expect(screen.getAllByLabelText("Not installed on this host").length).toBeGreaterThan(0);
  });

  it("can't-enable cell is disabled and shows its reason as title", async () => {
    vi.spyOn(api, "getInventory").mockResolvedValue(AGGREGATE);
    wrap(<InventoryPage />);
    const cell = await waitFor(() =>
      screen.getByTestId<HTMLButtonElement>(`cell-needs_display-${H1}`),
    );
    expect(cell.disabled).toBe(true);
    expect(cell.getAttribute("aria-label")).toMatch(/requires a display/);
  });
});

describe("InventoryPage toggle (putInventory)", () => {
  it("clicking an enabled cell calls putInventory with {plugins:{name:false}} + machine", async () => {
    vi.spyOn(api, "getInventory").mockResolvedValue(AGGREGATE);
    const putSpy = vi.spyOn(api, "putInventory").mockResolvedValue(OK_RESULT);
    wrap(<InventoryPage />);
    // ava_code is enabled on h1 -> clicking turns it OFF.
    const cell = await waitFor(() => screen.getByTestId(`cell-ava_code-${H1}`));
    fireEvent.click(cell);
    await waitFor(() => expect(putSpy).toHaveBeenCalled());
    expect(putSpy).toHaveBeenCalledWith({ plugins: { ava_code: false } }, H1);
  });

  it("clicking a disabled cell calls putInventory to enable it on that host", async () => {
    vi.spyOn(api, "getInventory").mockResolvedValue(AGGREGATE);
    const putSpy = vi.spyOn(api, "putInventory").mockResolvedValue(OK_RESULT);
    wrap(<InventoryPage />);
    // ava_code is disabled on h2 -> clicking turns it ON.
    const cell = await waitFor(() => screen.getByTestId(`cell-ava_code-${H2}`));
    fireEvent.click(cell);
    await waitFor(() =>
      expect(putSpy).toHaveBeenCalledWith({ plugins: { ava_code: true } }, H2),
    );
  });

  it("an mcp cell builds an mcp_servers body, not a plugins body", async () => {
    // Use an aggregate where the mcp server is enabled on h1 so its cell is
    // clickable (turning it OFF works even when can_enable is false).
    const agg: InventoryAggregate = {
      ...AGGREGATE,
      mcp_servers: [
        {
          name: "needs_display",
          kind: "mcp",
          description: "Headed MCP server",
          hosts: {
            [H1]: { present: true, enabled: true, can_enable: false, reason: "requires a display" },
            [H2]: { present: false, enabled: false, can_enable: null, reason: null },
          },
        },
      ],
    };
    vi.spyOn(api, "getInventory").mockResolvedValue(agg);
    const putSpy = vi.spyOn(api, "putInventory").mockResolvedValue(OK_RESULT);
    wrap(<InventoryPage />);
    const cell = await waitFor(() =>
      screen.getByTestId<HTMLButtonElement>(`cell-needs_display-${H1}`),
    );
    // Enabled mcp item is clickable even with can_enable:false.
    expect(cell.disabled).toBe(false);
    fireEvent.click(cell);
    await waitFor(() =>
      expect(putSpy).toHaveBeenCalledWith({ mcp_servers: { needs_display: false } }, H1),
    );
  });

  it("a settled write invalidates the inventory query", async () => {
    vi.spyOn(api, "getInventory").mockResolvedValue(AGGREGATE);
    vi.spyOn(api, "putInventory").mockResolvedValue(OK_RESULT);
    const qc = makeQc();
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    wrap(<InventoryPage />, qc);
    const cell = await waitFor(() => screen.getByTestId(`cell-ava_code-${H1}`));
    fireEvent.click(cell);
    await waitFor(() => {
      const keys = invalidateSpy.mock.calls.map(([opts]) => opts?.queryKey);
      const hit = keys.some((k) => Array.isArray(k) && k[0] === "inventory");
      expect(hit).toBe(true);
    });
  });

  it("enable-all fans out one putInventory per installed+enableable host", async () => {
    vi.spyOn(api, "getInventory").mockResolvedValue(AGGREGATE);
    const putSpy = vi.spyOn(api, "putInventory").mockResolvedValue(OK_RESULT);
    wrap(<InventoryPage />);
    // ava_code: enabled on h1 (skipped — already target), disabled on h2 ->
    // exactly one PUT for h2.
    const btn = await waitFor(() => screen.getByTestId("enable-all-ava_code"));
    fireEvent.click(btn);
    await waitFor(() => expect(putSpy).toHaveBeenCalledTimes(1));
    expect(putSpy).toHaveBeenCalledWith({ plugins: { ava_code: true } }, H2);
  });
});

// Both sections render on this page, so these also pin that each entry carries
// its OWN kind — the bug a shared component invites.
describe("InventoryPage install entries", () => {
  it("the plugins entry posts kind=plugin and opens the installer's conversation", async () => {
    vi.spyOn(api, "getInventory").mockResolvedValue(AGGREGATE);
    const draft = vi.spyOn(api, "draftPackage").mockResolvedValue({ agent_id: 61 });
    wrap(<InventoryPage />);

    fireEvent.change(await screen.findByPlaceholderText(/Describe a plugin you want/), {
      target: { value: "review my PRs" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Find and install a plugin" }));

    await waitFor(() => expect(draft).toHaveBeenCalledWith("plugin", "review my PRs"));
    await waitFor(() => expect(pushSpy).toHaveBeenCalledWith("/"));
  });

  it("the MCP entry posts kind=mcp", async () => {
    vi.spyOn(api, "getInventory").mockResolvedValue(AGGREGATE);
    const draft = vi.spyOn(api, "draftPackage").mockResolvedValue({ agent_id: 62 });
    wrap(<InventoryPage />);

    fireEvent.change(await screen.findByPlaceholderText(/Describe a tool you want to reach/), {
      target: { value: "query our Linear issues" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Find and install an MCP server" }));

    await waitFor(() => expect(draft).toHaveBeenCalledWith("mcp", "query our Linear issues"));
  });

  it("both entries survive a failed inventory read", async () => {
    vi.spyOn(api, "getInventory").mockRejectedValue(new Error("network down"));
    wrap(<InventoryPage />);
    await waitFor(() =>
      expect(screen.getAllByText(/Couldn't load inventory/).length).toBeGreaterThan(0),
    );
    expect(screen.getByRole("button", { name: "Find and install a plugin" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Find and install an MCP server" })).toBeTruthy();
  });
});
