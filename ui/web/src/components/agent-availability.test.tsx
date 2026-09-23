import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { AgentAvailability } from "./agent-availability";
import type { AgentRow } from "@/lib/types";

const getAgent = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ api: { getAgent } }));

const now = new Date().toISOString();
const agent = {
  agent_id: 6571,
  machine: "macbook-air",
  availability: {
    reason: "unknown",
    observed_at: now,
  },
} as AgentRow;

function show(selected: AgentRow = agent) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AgentAvailability agent={selected} />
    </QueryClientProvider>,
  );
}

afterEach(() => getAgent.mockReset());

it("shows a fresh host-down reason and machine diagnostics on the selected agent", async () => {
  getAgent.mockResolvedValue({ ...agent, availability: {
    reason: "host_unavailable",
    observed_at: now,
  } });
  show();
  expect(await screen.findByText(/Agent host unavailable; creation may be accepted/)).toBeTruthy();
  expect(screen.getByRole("link", { name: "Machine diagnostics" }).getAttribute("href"))
    .toBe("/insights/status");
});

it("shows the coarse admission refusal category without claiming a turn ran", async () => {
  getAgent.mockResolvedValue({ ...agent, availability: {
    reason: "admission_refused",
    admission_outcome: "publication_deferred",
    observed_at: now,
  } });
  show();
  expect(await screen.findByText("Host admission deferred by runtime publication")).toBeTruthy();
});

it("labels admission without claiming first-turn completion", async () => {
  getAgent.mockResolvedValue({ ...agent, availability: {
    reason: "admitted",
    observed_at: now,
  } });
  show();
  expect(await screen.findByText("Host admission observed; first turn completion is not confirmed"))
    .toBeTruthy();
});

it("drops a stale cached admission label when detail cannot refresh", () => {
  getAgent.mockReturnValue(new Promise(() => undefined));
  show({ ...agent, availability: {
    reason: "admitted",
    observed_at: "2026-09-24T00:00:00Z",
  } });
  expect(screen.getByText("Start availability unknown")).toBeTruthy();
});
