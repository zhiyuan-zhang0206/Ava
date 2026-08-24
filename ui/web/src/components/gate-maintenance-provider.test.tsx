import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

import { GateMaintenanceProvider } from "./gate-maintenance-provider";

const { getGateMaintenanceState, reloadThroughGate, protectedClusterStatus } = vi.hoisted(() => ({
  getGateMaintenanceState: vi.fn(),
  reloadThroughGate: vi.fn(),
  protectedClusterStatus: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ api: { getClusterStatus: protectedClusterStatus } }));
vi.mock("@/lib/gate-maintenance", () => ({
  getGateMaintenanceState,
  reloadThroughGate,
  UI_UPDATE_QUERY_KEY: ["ui-update-state"],
}));

beforeEach(() => {
  getGateMaintenanceState.mockReset();
  reloadThroughGate.mockReset();
});

it("reloads an unauthenticated root for a persisted maintenance owner", async () => {
  getGateMaintenanceState.mockResolvedValue({ status: "updating", generation: "g1" });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  render(
    <QueryClientProvider client={client}>
      <GateMaintenanceProvider />
    </QueryClientProvider>,
  );

  await waitFor(() => expect(reloadThroughGate).toHaveBeenCalledTimes(1));
  expect(protectedClusterStatus).not.toHaveBeenCalled();
});
