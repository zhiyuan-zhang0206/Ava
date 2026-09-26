// Status section tests — the tri-state (loading / quiet-error / data), the
// Services block (agent-runner table and current observations),
// code-drift display, and the merged Gateway card + daemon section.
//
// Renders <StatusPage /> directly (no Control page shell): useSectionVisible
// defaults true outside a provider, so the status poll enables and goes through
// its real react-query lifecycle. happy-dom + RTL + real QueryClient; mock at
// the api layer.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { SystemStatus } from "@/lib/types";

import StatusPage from "./page";

afterEach(cleanup);

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const STATUS_OK: SystemStatus = {
  services: {
    items: [
      { name: "gateway", label: "Gateway", online: true, pid: 123, detail: null },
      { name: "labeler", label: "Labeler", online: false, pid: null, detail: "crashed" },
      { name: "memory_indexer", label: "Memory Indexer", online: null, pid: null, detail: null },
    ],
  },
  cluster: {
    current_machine: "test-host",
    current_serve_gateway: true,
    current_serve_agent_runner: false,
    current_serve_observability_station: false,
    current_paused: false,
    machines: [
      {
        name: "test-host",
        serve_gateway: true,
        serve_agent_runner: false,
        serve_observability_station: false,
        identity_mismatch: false,
        is_staging: false,
        gateway_url: "http://10.0.0.1:8000",
        up_since_at: new Date(Date.now() - 30_000).toISOString(),
        online: true,
        paused: false,
        head_sha: "abc1234def",
        shell_count: 0,
        agent_count: 0,
        session_count: 0,
        agent_groups: [],
        resource: {
          ts: Date.now() - 60_000,
          cpu_pct: 12.5,
          mem_used_gb: 8,
          mem_total_gb: 16,
          mem_pct: 50,
          disk_used_gb: 100,
          disk_total_gb: 250,
          disk_pct: 40,
        },
        agent_host_online: null,
        supervisor_online: true,
      },
      {
        // offline: a failed probe clears HEAD, so head_sha is null
        name: "wsl",
        serve_gateway: false,
        serve_agent_runner: true,
        serve_observability_station: false,
        identity_mismatch: false,
        is_staging: false,
        gateway_url: "http://10.0.0.2:8000",
        up_since_at: new Date(Date.now() - 5 * 60_000).toISOString(),
        online: false,
        paused: null,
        head_sha: null,
        shell_count: 0,
        agent_count: 0,
        session_count: 0,
        agent_groups: [],
        resource: {
          ts: Date.now() - 60_000,
          cpu_pct: 12.5,
          mem_used_gb: 8,
          mem_total_gb: 16,
          mem_pct: 50,
          disk_used_gb: 100,
          disk_total_gb: 250,
          disk_pct: 40,
        },
        agent_host_online: null,
        supervisor_online: null,
      },
      {
        // online, with daemon health degraded
        name: "test-host-2",
        serve_gateway: false,
        serve_agent_runner: true,
        serve_observability_station: false,
        identity_mismatch: false,
        is_staging: false,
        gateway_url: "http://10.0.0.3:8000",
        up_since_at: new Date(Date.now() - 30_000).toISOString(),
        online: true,
        paused: false,
        head_sha: "999888777",
        shell_count: 2,
        agent_count: 4,
        session_count: 0,
        agent_groups: [],
        resource: null,
        agent_host_online: true,
        supervisor_online: false,
      },
    ],
  },
};

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, "getSystemStatus").mockResolvedValue(STATUS_OK);

});

describe("StatusPage polling", () => {
  it("does not refetch status before 15 seconds", async () => {
    vi.useFakeTimers();
    const view = wrap(<StatusPage />);
    try {
      await act(async () => {
        await Promise.resolve();
      });
      expect(api.getSystemStatus).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(14_999);
      });
      expect(api.getSystemStatus).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
      expect(api.getSystemStatus).toHaveBeenCalledTimes(2);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });
});

describe("StatusPage states", () => {
  it("loading state shows a spinner (animate-spin)", async () => {
    vi.spyOn(api, "getSystemStatus").mockReturnValue(
      new Promise(() => undefined /* never resolves */),
    );
    const { container } = wrap(<StatusPage />);
    await waitFor(() => container.querySelector(".animate-spin"));
    expect(container.querySelector(".animate-spin")).toBeTruthy();
  });

  it("error state shows a quiet retry line, not the raw error", async () => {
    vi.spyOn(api, "getSystemStatus").mockRejectedValue(new Error("status 500"));
    wrap(<StatusPage />);
    await waitFor(() => screen.getByText(/Couldn't reach the gateway/));
    expect(screen.queryByText(/status 500/)).toBeNull();
  });
});

describe("StatusPage Services and Gateway sections", () => {
  it("keeps agent runners under Services and the gateway card under Gateway", async () => {
    wrap(<StatusPage />);
    await waitFor(() => screen.getByText("Services"));
    expect(screen.getByText(/this host: test-host \(gateway\)/)).toBeTruthy();
    // The gateway card names its host and marks it as this host.
    const card = screen.getByTestId("gateway-card-test-host");
    expect(card.textContent).toMatch(/test-host/);
    expect(card.textContent).toMatch(/\(this host\)/);
    expect(card.textContent).toMatch(/running/);
    expect(card.textContent).toMatch(/healthy/);
    // Both agent-runners are table rows.
    const table = screen.getByTestId("agent-runners-card");
    expect(table.textContent).toMatch(/wsl/);
    expect(table.textContent).toMatch(/test-host-2/);
    expect(document.getElementById("status-services")?.contains(table)).toBe(true);
    expect(document.getElementById("status-gateway")?.contains(card)).toBe(true);
  });

  it("staging machines carry a staging badge but stay roster-visible", async () => {
    const staged = structuredClone(STATUS_OK);
    staged.cluster.machines = staged.cluster.machines.map((m) =>
      m.name === "wsl" ? { ...m, is_staging: true } : m,
    );
    vi.spyOn(api, "getSystemStatus").mockResolvedValue(staged);

    wrap(<StatusPage />);
    await waitFor(() => screen.getByTestId("agent-runners-card"));
    const table = screen.getByTestId("agent-runners-card");
    // the staging runner is still listed (visible), with its badge
    expect(table.textContent).toMatch(/wsl/);
    expect(table.textContent).toMatch(/staging/);
    // the non-staging runners carry no badge
    const nonStaged = screen.getByTestId("gateway-card-test-host").textContent;
    expect(nonStaged).not.toContain("staging");
  });

  it("statuses: online runner green 'online', unreachable runner 'offline'", async () => {
    wrap(<StatusPage />);
    await waitFor(() => screen.getByTestId("agent-runners-card"));
    const table = screen.getByTestId("agent-runners-card");
    expect(table.textContent).toMatch(/online/);
    // wsl is offline with no stopped_at → a crash, not a deliberate stop.
    expect(table.textContent).toMatch(/offline/);
    expect(screen.queryByText("stopped")).toBeNull();
  });

  it("reachable runner with unknown status is amber, never green", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue({
      ...STATUS_OK,
      cluster: {
        ...STATUS_OK.cluster,
        machines: STATUS_OK.cluster.machines.map((machine) =>
          machine.name === "wsl" ? { ...machine, online: true, paused: null } : machine,
        ),
      },
    });
    wrap(<StatusPage />);

    const unknown = await screen.findByText("status unknown");
    expect(unknown.className).toContain("text-amber");
    expect(unknown.className).not.toContain("text-green");
  });

  it("stopped_at set → deliberate 'stopped', not 'offline'", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue({
      ...STATUS_OK,
      cluster: {
        ...STATUS_OK.cluster,
        machines: [
          STATUS_OK.cluster.machines[0],
          {
            ...STATUS_OK.cluster.machines[1],
            stopped_at: new Date(Date.now() - 60_000).toISOString(),
          },
        ],
      },
    });
    wrap(<StatusPage />);
    await waitFor(() => screen.getByTestId("agent-runners-card"));
    expect(screen.getByText("stopped")).toBeTruthy();
  });

  it("a live host's stamp is titled 'Up since', matching what it renders", async () => {
    // up_since_at is a boot/announce stamp, so both surfaces must say "up since"
    // rather than "last seen" — the CLI already did, this page did not (#981).
    wrap(<StatusPage />);
    await waitFor(() => screen.getByTestId("agent-runners-card"));
    expect(screen.queryByText("Last seen")).toBeNull();
    expect(screen.getAllByText("Up since").length).toBeGreaterThan(0);
  });

  it("agent count renders per runner row", async () => {
    wrap(<StatusPage />);
    await waitFor(() => screen.getByTestId("agent-runners-card"));
    const table = screen.getByTestId("agent-runners-card");
    // test-host-2 runs 4 agents.
    expect(table.textContent).toMatch(/4/);
  });

  it("never renders a historical cluster pin or known-good anchor", async () => {
    // Nothing writes the pin any more; a payload still carrying frozen values
    // (an older gateway) must not present them as the cluster's current target.
    vi.spyOn(api, "getSystemStatus").mockResolvedValue({
      ...STATUS_OK,
      cluster: {
        ...STATUS_OK.cluster,
        cluster_target_sha: "0123456789",
        cluster_last_known_good_sha: "fedcba9876",
        machines: [
          { ...STATUS_OK.cluster.machines[0], on_pin: false },
          STATUS_OK.cluster.machines[1],
        ],
      } as unknown as SystemStatus["cluster"],
    });
    wrap(<StatusPage />);
    await waitFor(() => screen.getByTestId("gateway-card-test-host"));
    expect(screen.queryByText(/0123456/)).toBeNull();
    expect(screen.queryByText(/fedcba9/)).toBeNull();
    expect(screen.queryByText("off-pin")).toBeNull();
  });

  it("code drift renders ⚠ + the running sha", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue({
      ...STATUS_OK,
      cluster: {
        ...STATUS_OK.cluster,
        machines: [
          {
            ...STATUS_OK.cluster.machines[0],
            running_sha: "111222333",
          },
          STATUS_OK.cluster.machines[1],
        ],
      },
    });
    wrap(<StatusPage />);
    await waitFor(() => screen.getByTestId("gateway-card-test-host"));
    expect(screen.getByText(/⚠1112223/)).toBeTruthy();
  });

  it("gateway daemon probe failure → health 'degraded'", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue({
      ...STATUS_OK,
      cluster: {
        ...STATUS_OK.cluster,
        machines: [
          { ...STATUS_OK.cluster.machines[0], supervisor_online: false },
        ],
      },
    });
    wrap(<StatusPage />);
    await waitFor(() => screen.getByTestId("gateway-card-test-host"));
    expect(screen.getByText("degraded")).toBeTruthy();
  });

  it("current_paused=true → top paused marker", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue({
      ...STATUS_OK,
      cluster: { ...STATUS_OK.cluster, current_paused: true },
    });
    wrap(<StatusPage />);
    await waitFor(() => screen.getByText("Services"));
    expect(screen.getByText(/· paused/)).toBeTruthy();
  });

  it("machines empty → empty-state copy", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue({
      ...STATUS_OK,
      cluster: { ...STATUS_OK.cluster, machines: [] },
    });
    wrap(<StatusPage />);
    await waitFor(() => screen.getByText("Services"));
    expect(screen.getByText(/no host has run/)).toBeTruthy();
  });

  it("gateway-only host does not render the no-host or agent-runners states", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue({
      ...STATUS_OK,
      cluster: {
        ...STATUS_OK.cluster,
        machines: [STATUS_OK.cluster.machines[0]],
      },
    });
    wrap(<StatusPage />);
    await waitFor(() => screen.getByTestId("gateway-card-test-host"));
    expect(screen.queryByText(/no host has run/)).toBeNull();
    expect(screen.queryByTestId("agent-runners-card")).toBeNull();
    expect(screen.getByTestId("gateway-card-test-host")).toBeTruthy();
  });

  it("single-box host (both flags) appears as the gateway card AND a runner row", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue({
      ...STATUS_OK,
      cluster: {
        ...STATUS_OK.cluster,
        current_machine: "test-host",
        current_serve_gateway: true,
        current_serve_agent_runner: true,
        current_serve_observability_station: false,
        machines: [
          { ...STATUS_OK.cluster.machines[0], name: "test-host", serve_gateway: true, serve_agent_runner: true },
          { ...STATUS_OK.cluster.machines[1], name: "wsl", serve_gateway: false, serve_agent_runner: true },
        ],
      },
    });
    wrap(<StatusPage />);
    await waitFor(() => screen.getByText("Services"));
    expect(screen.getByTestId("gateway-card-test-host")).toBeTruthy();
    expect(screen.getByTestId("agent-runners-card").textContent).toMatch(/test-host/);
    expect(
      screen.getByText(/this host: test-host \(gateway \+ agent-runner\)/),
    ).toBeTruthy();
  });

  it("shows observations without mutable-checkout deployment controls", async () => {
    wrap(<StatusPage />);
    await waitFor(() => screen.getByText("Services"));
    expect(screen.getByTestId("agent-runners-card")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /update|restart|check for/i })).toBeNull();
  });

});

describe("StatusPage gateway section", () => {
  it("all three online states (true/false/null) render", async () => {
    wrap(<StatusPage />);
    await waitFor(() => screen.getByText("Labeler"));
    expect(screen.getByRole("heading", { name: "Gateway", level: 3 })).toBeTruthy();
    expect(screen.getAllByText("Gateway").length).toBeGreaterThan(0);
    expect(screen.getByText("Labeler")).toBeTruthy();
    expect(screen.getByText("Memory Indexer")).toBeTruthy();
    expect(screen.getByText("PID 123")).toBeTruthy();
    expect(screen.getByText("crashed")).toBeTruthy();
  });

  it("services empty → shows 'No service data'", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue({
      ...STATUS_OK,
      services: { items: [] },
    });
    wrap(<StatusPage />);
    await waitFor(() => screen.getByText("No service data"));
  });
});

describe("StatusPage sub-anchors", () => {
  it("each block carries its nav anchor id (Services / Gateway)", async () => {
    wrap(<StatusPage />);
    await waitFor(() => screen.getByText("Services"));
    for (const id of ["status-services", "status-gateway"]) {
      expect(document.getElementById(id), id).toBeTruthy();
    }
  });
});
