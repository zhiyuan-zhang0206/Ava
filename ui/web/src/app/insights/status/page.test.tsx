// Status section tests — the tri-state (loading / quiet-error / data), the
// Services block (agent-runner table + cluster-wide Update / Restart actions),
// pin/off-pin display, and the merged Gateway card + daemon section.
//
// Renders <StatusPage /> directly (no Control page shell): useSectionVisible
// defaults true outside a provider, so the status poll enables and goes through
// its real react-query lifecycle. happy-dom + RTL + real QueryClient; mock at
// the api layer.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    cluster_target_sha: "abc1234def",
    machines: [
      {
        name: "test-host",
        serve_gateway: true,
        serve_agent_runner: false,
        serve_observability_station: false,
        identity_mismatch: false,
        settle_waited_on: false,
        is_staging: false,
        gateway_url: "http://10.0.0.1:8000",
        up_since_at: new Date(Date.now() - 30_000).toISOString(),
        online: true,
        paused: false,
        head_sha: "abc1234def",
        on_pin: true,
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
        watchdog_online: true,
      },
      {
        // offline: a failed probe clears HEAD, so head_sha is null + on_pin null
        name: "wsl",
        serve_gateway: false,
        serve_agent_runner: true,
        serve_observability_station: false,
        identity_mismatch: false,
        settle_waited_on: false,
        is_staging: false,
        gateway_url: "http://10.0.0.2:8000",
        up_since_at: new Date(Date.now() - 5 * 60_000).toISOString(),
        online: false,
        paused: null,
        head_sha: null,
        on_pin: null,
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
        watchdog_online: null,
      },
      {
        // online but drifted off the pin → the off-pin case
        name: "test-host-2",
        serve_gateway: false,
        serve_agent_runner: true,
        serve_observability_station: false,
        identity_mismatch: false,
        settle_waited_on: false,
        is_staging: false,
        gateway_url: "http://10.0.0.3:8000",
        up_since_at: new Date(Date.now() - 30_000).toISOString(),
        online: true,
        paused: false,
        head_sha: "999888777",
        on_pin: false,
        shell_count: 2,
        agent_count: 4,
        session_count: 0,
        agent_groups: [],
        resource: null,
        agent_host_online: true,
        watchdog_online: false,
      },
    ],
  },
};

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, "getSystemStatus").mockResolvedValue(STATUS_OK);
  // ServicesPanel's preflight poll (api.checkClusterUpdate) auto-enables
  // whenever isGateway && visible — true for every STATUS_OK-backed render,
  // regardless of what a given test asserts on. Default it to the quiet
  // "up to date" shape here so every test gets it mocked; tests that care
  // about a specific behind/changed state still override via their own
  // vi.spyOn call below (runs after this and wins). Without this default,
  // any test that renders <StatusPage /> without its own override hits the
  // real (unmocked) api.checkClusterUpdate — a live fetch that silently
  // succeeds against a local prod gateway but ECONNREFUSEDs in CI (no
  // gateway there), the source of a flaky "Unhandled Errors" vitest exit.
  vi.spyOn(api, "checkClusterUpdate").mockResolvedValue({
    behind: 0,
    frontend_changed: false,
    backend_changed: false,
    needs_replay: false,
  });
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

  it("agent count + off-pin marker render per runner row", async () => {
    wrap(<StatusPage />);
    await waitFor(() => screen.getByTestId("agent-runners-card"));
    const table = screen.getByTestId("agent-runners-card");
    // test-host-2 runs 4 agents and sits off the cluster pin.
    expect(table.textContent).toMatch(/4/);
    expect(screen.getByText("off-pin")).toBeTruthy();
    expect(screen.getByText(/pinned to abc1234/)).toBeTruthy();
  });

  it("code drift outranks the pin verdict (⚠ + running sha)", async () => {
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
    expect(screen.queryByText("off-pin")).toBeNull();
  });

  it("settle_waited_on renders a settle-hold badge on that host only", async () => {
    // A deploy's settle hold names wsl. The badge is the lease's record of who it
    // waits for — orthogonal to the live pin/code verdicts, so it shows alongside
    // them rather than replacing them, and only on the named host.
    vi.spyOn(api, "getSystemStatus").mockResolvedValue({
      ...STATUS_OK,
      cluster: {
        ...STATUS_OK.cluster,
        machines: [
          STATUS_OK.cluster.machines[0],
          { ...STATUS_OK.cluster.machines[1], settle_waited_on: true },
        ],
      },
    });
    wrap(<StatusPage />);
    await waitFor(() => screen.getByTestId("agent-runners-card"));
    expect(screen.getAllByText("settle-hold")).toHaveLength(1);
    expect(screen.getByTestId("gateway-card-test-host").textContent).not.toContain("settle-hold");
  });

  it("gateway daemon probe failure → health 'degraded'", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue({
      ...STATUS_OK,
      cluster: {
        ...STATUS_OK.cluster,
        machines: [
          { ...STATUS_OK.cluster.machines[0], watchdog_online: false },
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

  // A failed rollout used to reach this page only as the amber pin/head mismatch
  // rendered a few lines up — a symptom several unrelated states share, which is
  // what left the 2026-07-30 operator reconstructing which one it was (#1012).
  it("a failed last update states the failure, the step, and what the pin did", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue({
      ...STATUS_OK,
      cluster: {
        ...STATUS_OK.cluster,
        last_update: {
          outcome: "incomplete",
          failed: true,
          target_sha: "8bdd3667aa",
          origin: "frontend",
          started_at: new Date("2026-07-30T21:10:00Z").toISOString(),
          ended_at: new Date("2026-07-30T21:13:00Z").toISOString(),
          failing_step: "the gateway was not serving, so Phase B never fanned out",
          pin_advanced: false,
        },
      },
    });
    wrap(<StatusPage />);
    await waitFor(() => screen.getByText("Services"));

    const banner = screen.getByRole("alert");
    expect(banner.textContent).toContain("Last update to 8bdd366 failed");
    expect(banner.textContent).toContain("Phase B never fanned out");
    // and it says the sha mismatch below is a consequence, not a second problem
    expect(banner.textContent).toContain("not a separate problem");
  });

  it("an orphaned update reports the death its own orchestration could not", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue({
      ...STATUS_OK,
      cluster: {
        ...STATUS_OK.cluster,
        last_update: {
          outcome: "orphaned",
          failed: true,
          target_sha: "8bdd3667aa",
          origin: "frontend",
          started_at: new Date("2026-07-30T21:10:00Z").toISOString(),
          ended_at: null,
          failing_step: null,
          pin_advanced: false,
        },
      },
    });
    wrap(<StatusPage />);
    await waitFor(() => screen.getByText("Services"));

    expect(screen.getByRole("alert").textContent).toContain(
      "died without reporting an outcome",
    );
  });

  it("the rollback anchor and the recovery that ran are both stated", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue({
      ...STATUS_OK,
      cluster: {
        ...STATUS_OK.cluster,
        cluster_last_known_good_sha: "7e571b49aa",
        last_update: {
          outcome: "orphaned",
          failed: true,
          target_sha: "8bdd3667aa",
          origin: "frontend",
          started_at: new Date("2026-07-30T21:10:00Z").toISOString(),
          ended_at: null,
          failing_step: null,
          observed_by: "rolled back 8bdd366 -> 7e571b4",
          pin_advanced: false,
        },
      },
    });
    wrap(<StatusPage />);
    await waitFor(() => screen.getByText("Services"));

    // the anchor rides beside the pin, so a pin that moved backwards is explained
    expect(screen.getByText(/last known good 7e571b4/)).toBeTruthy();
    const banner = screen.getByRole("alert");
    expect(banner.textContent).toContain("Since then: rolled back 8bdd366 -> 7e571b4");
    expect(banner.textContent).toContain("not drift");
  });

  // A recovery still has to be stated — that silence IS the 2026-07-30 bug — but
  // it asks nothing of the operator, so it must not read like a live fault.
  it("a recovered update says the cluster came back and is not styled as a fault", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue({
      ...STATUS_OK,
      cluster: {
        ...STATUS_OK.cluster,
        last_update: {
          outcome: "recovered",
          failed: true,
          target_sha: "8bdd3667aa",
          origin: "frontend",
          started_at: new Date("2026-07-30T21:10:00Z").toISOString(),
          ended_at: new Date("2026-07-30T21:13:00Z").toISOString(),
          failing_step: "gateway local update (rc=1): recovered to last-known-good",
          observed_by: "rolled back 8bdd366 -> 7e571b4",
          pin_advanced: false,
        },
      },
    });
    wrap(<StatusPage />);
    await waitFor(() => screen.getByText("Services"));

    const banner = screen.getByRole("alert");
    expect(banner.textContent).toContain("the cluster recovered");
    expect(banner.textContent).toContain("nothing to repair here");
    expect(banner.className).toContain("amber");
    expect(banner.className).not.toContain("destructive");
  });

  it("the banner names the rollout's own log when the record carries one", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue({
      ...STATUS_OK,
      cluster: {
        ...STATUS_OK.cluster,
        last_update: {
          outcome: "aborted",
          failed: true,
          target_sha: "8bdd3667aa",
          origin: "frontend",
          started_at: new Date("2026-07-30T21:10:00Z").toISOString(),
          ended_at: new Date("2026-07-30T21:13:00Z").toISOString(),
          failing_step: null,
          log_path: "/home/ava/.ava/logs/rollout-1785470000.log",
          pin_advanced: false,
        },
      },
    });
    wrap(<StatusPage />);
    await waitFor(() => screen.getByText("Services"));

    const banner = screen.getByRole("alert");
    expect(banner.textContent).toContain("/home/ava/.ava/logs/rollout-1785470000.log");
    // never the glob when the real path is known
    expect(banner.textContent).not.toContain("rollout-<epoch>.log");
  });

  it("a successful last update renders no banner at all", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue({
      ...STATUS_OK,
      cluster: {
        ...STATUS_OK.cluster,
        last_update: {
          outcome: "clean",
          failed: false,
          target_sha: "8bdd3667aa",
          origin: "frontend",
          started_at: new Date("2026-07-30T21:10:00Z").toISOString(),
          ended_at: new Date("2026-07-30T21:13:00Z").toISOString(),
          failing_step: null,
          pin_advanced: true,
        },
      },
    });
    wrap(<StatusPage />);
    await waitFor(() => screen.getByText("Services"));

    expect(screen.queryByRole("alert")).toBeNull();
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

  it("behind=0 → Update button disabled + 'Up to date'", async () => {
    vi.spyOn(api, "checkClusterUpdate").mockResolvedValue({
      behind: 0,
      frontend_changed: false,
      backend_changed: false,
      needs_replay: false,
    });
    wrap(<StatusPage />);
    await waitFor(() => screen.getByText("Services"));
    const updateBtn = await screen.findByRole("button", { name: /Up to date/ });
    expect((updateBtn as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/up to date with origin\/main/)).toBeTruthy();
  });

  it("behind>0 → Update enabled, shows count + which side restarts", async () => {
    vi.spyOn(api, "checkClusterUpdate").mockResolvedValue({
      behind: 3,
      frontend_changed: false,
      backend_changed: true,
      needs_replay: false,
    });
    wrap(<StatusPage />);
    await waitFor(() => screen.getByText("Services"));
    const updateBtn = await screen.findByRole("button", { name: /Update \(3\)/ });
    expect((updateBtn as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByText(/3 commits behind — Update restarts backend/)).toBeTruthy();
  });

  it("interrupted rollout → Replay update stays enabled at zero commits", async () => {
    vi.spyOn(api, "checkClusterUpdate").mockResolvedValue({
      behind: 0,
      frontend_changed: false,
      backend_changed: false,
      needs_replay: true,
    });
    const confirmSpy = vi.fn((_message?: string) => false);
    window.confirm = confirmSpy;
    wrap(<StatusPage />);
    await waitFor(() => screen.getByText("Services"));
    const updateBtn = await screen.findByRole("button", { name: "Replay update" });
    expect((updateBtn as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByText(/half-deployed state — needs replay/)).toBeTruthy();
    fireEvent.click(updateBtn);
    expect(confirmSpy).toHaveBeenCalledWith(
      expect.stringContaining("Replays the interrupted rollout and restarts all services."),
    );
    expect(confirmSpy.mock.calls[0]?.[0]).not.toContain("nothing");
  });

  it("Restart button triggers triggerClusterRestart (on confirm)", async () => {
    vi.spyOn(api, "checkClusterUpdate").mockResolvedValue({
      behind: 0,
      frontend_changed: false,
      backend_changed: false,
      needs_replay: false,
    });
    const restartSpy = vi
      .spyOn(api, "triggerClusterRestart")
      .mockResolvedValue({ session: "ava-cluster-restart", log: "/x" });
    window.confirm = vi.fn(() => true);
    wrap(<StatusPage />);
    await waitFor(() => screen.getByText("Services"));
    fireEvent.click(await screen.findByRole("button", { name: /Restart/ }));
    await waitFor(() => expect(restartSpy).toHaveBeenCalledTimes(1));
  });

  it("current_paused=true disables Update + Restart (a rollout is in flight)", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue({
      ...STATUS_OK,
      cluster: { ...STATUS_OK.cluster, current_paused: true, machines: [] },
    });
    vi.spyOn(api, "checkClusterUpdate").mockResolvedValue({
      behind: 2,
      frontend_changed: true,
      backend_changed: false,
      needs_replay: false,
    });
    wrap(<StatusPage />);
    const restartBtn = await screen.findByRole("button", { name: /Restart/i });
    // `Update (2)` exactly — a bare /Update/i would also match the
    // "Check for updates" re-check icon button.
    const updateBtn = await screen.findByRole("button", { name: /Update \(2\)/ });
    expect((restartBtn as HTMLButtonElement).disabled).toBe(true);
    expect((updateBtn as HTMLButtonElement).disabled).toBe(true);
  });

  it("re-check button re-fires the update check (no poll interval behind it)", async () => {
    const checkSpy = vi.spyOn(api, "checkClusterUpdate").mockResolvedValue({
      behind: 0,
      frontend_changed: false,
      backend_changed: false,
      needs_replay: false,
    });
    wrap(<StatusPage />);
    await waitFor(() => screen.getByText("Services"));
    const recheckBtn = await screen.findByRole("button", { name: "Check for updates" });
    expect(checkSpy).toHaveBeenCalledTimes(1);
    fireEvent.click(recheckBtn);
    await waitFor(() => expect(checkSpy).toHaveBeenCalledTimes(2));
  });

  it("current_orchestration=rollout disables both + shows Updating… before paused flips", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue({
      ...STATUS_OK,
      cluster: {
        ...STATUS_OK.cluster,
        current_paused: false,
        current_orchestration: "rollout",
        machines: [],
      },
    } as never);
    vi.spyOn(api, "checkClusterUpdate").mockResolvedValue({
      behind: 2,
      frontend_changed: true,
      backend_changed: false,
      needs_replay: false,
    });
    wrap(<StatusPage />);
    const updateBtn = await screen.findByRole("button", { name: /Updating/i });
    const restartBtn = await screen.findByRole("button", { name: /Restart/i });
    expect((updateBtn as HTMLButtonElement).disabled).toBe(true);
    expect((restartBtn as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/rollout in progress/i)).toBeTruthy();
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
