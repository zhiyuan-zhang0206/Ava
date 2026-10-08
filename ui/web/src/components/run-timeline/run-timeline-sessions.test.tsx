import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  BuildProgressResponse,
  BuildRequest,
  BuildResponse,
  SessionsResponse,
} from "@/lib/contracts/types";

const { getAgentSessions, postUnderstandingBuild, getUnderstandingBuild } = vi.hoisted(() => ({
  getAgentSessions: vi.fn<(agentId: number) => Promise<SessionsResponse>>(),
  postUnderstandingBuild: vi.fn<(agentId: number, body: BuildRequest) => Promise<BuildResponse>>(),
  getUnderstandingBuild: vi.fn<(agentId: number, buildId: number) => Promise<BuildProgressResponse>>(),
}));

vi.mock("@/lib/transport/api", () => ({
  api: { getAgentSessions, postUnderstandingBuild, getUnderstandingBuild },
}));

import { RunTimelineSessions, sessionWindow } from "./run-timeline-sessions";

const sessionsResponse = (enabled = true): SessionsResponse => ({
  agent_id: 7,
  model: "model-x",
  understanding_enabled: enabled,
  cost_basis: "cold cache, full input price",
  sessions: [
    {
      number: 1,
      boundary_checkpoint_id: "c1",
      start: "2026-10-05T10:00:00Z",
      end: "2026-10-05T12:00:00Z",
      messages: 120,
      peak_input_tokens: 380_000,
      context_tokens: 380_000,
      generation_tokens: 1000,
      estimated: true,
      exact_fraction: 0.8,
      coverage: { status: "full", ratio: 1, covered_messages: 120, total_messages: 120 },
      estimate: { jobs: 0, input_tokens: 0, output_tokens: 0, cost_usd: 0 },
    },
    {
      number: 2,
      boundary_checkpoint_id: null,
      start: "2026-10-05T12:00:01Z",
      end: "2026-10-05T14:00:00Z",
      messages: 80,
      peak_input_tokens: 210_000,
      context_tokens: 210_000,
      generation_tokens: 1000,
      estimated: true,
      exact_fraction: 0.8,
      coverage: { status: "partial", ratio: 0.25, covered_messages: 20, total_messages: 80 },
      estimate: { jobs: 3, input_tokens: 900_000, output_tokens: 12_000, cost_usd: 4.5 },
    },
  ],
});

const buildResponse = (over: Partial<BuildResponse>): BuildResponse => ({
  agent_id: 7,
  dry_run: true,
  understanding_enabled: true,
  sessions: [2],
  jobs: [],
  queued: 0,
  merged: 0,
  estimate: { jobs: 3, input_tokens: 900_000, output_tokens: 12_000, cost_usd: 4.5 },
  cost_basis: "cold cache, full input price",
  build_id: null,
  rebuild_id: null,
  ...over,
});

const progress = (phase: BuildProgressResponse["phase"], status: "running" | "done"): BuildProgressResponse => ({
  build_id: 9,
  agent_id: 7,
  created_at: "2026-10-05T15:00:00Z",
  sessions: [2],
  phase,
  jobs: [
    {
      job_id: 31,
      session: 2,
      status,
      start_index: 0,
      end_index: 40,
      attempts: 1,
      error: null,
      calls: 2,
      input_tokens: 1000,
      cache_read_tokens: 0,
      output_tokens: 100,
      seconds: 3,
      cost_usd: 1.25,
    },
  ],
  rebuild: {
    id: 5,
    status: phase === "done" ? "done" : "pending",
    attempts: 0,
    error: null,
    leaves: 4,
    calls: 0,
    input_tokens: 0,
    cache_read_tokens: 0,
    output_tokens: 0,
    seconds: 0,
    cost_usd: null,
    levels: {},
  },
  cost_usd: 1.25,
});

function mount(props: Partial<React.ComponentProps<typeof RunTimelineSessions>> = {}) {
  const onZoom = vi.fn();
  const onBuildEnded = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RunTimelineSessions agentId={7} onZoom={onZoom} onBuildEnded={onBuildEnded} {...props} />
    </QueryClientProvider>,
  );
  return { onZoom, onBuildEnded };
}

const open = async () => {
  fireEvent.click(screen.getByTestId("run-timeline-sessions-toggle"));
  await screen.findAllByTestId("run-timeline-session-row");
};

beforeEach(() => {
  getAgentSessions.mockReset().mockResolvedValue(sessionsResponse());
  postUnderstandingBuild.mockReset();
  getUnderstandingBuild.mockReset();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("RunTimelineSessions list", () => {
  it("is collapsed and reads nothing until opened", () => {
    mount();
    expect(getAgentSessions).not.toHaveBeenCalled();
    expect(screen.queryByTestId("run-timeline-session-row")).toBeNull();
  });

  it("lists every session with range, messages, peak context, coverage and estimated cost", async () => {
    mount();
    await open();
    const [first, second] = screen.getAllByTestId("run-timeline-session-row");
    expect(first.textContent).toContain("#1");
    expect(first.textContent).toContain("120");
    expect(first.textContent).toContain("380.0k");
    expect(within(first).getByTestId("run-timeline-session-coverage").textContent).toBe("full 100%");
    expect(second.textContent).toContain("in progress");
    expect(within(second).getByTestId("run-timeline-session-coverage").textContent).toBe("partial 25%");
    expect(second.textContent).toContain("$4.50");
    expect(screen.getByTestId("run-timeline-sessions-toggle").textContent).toContain("(2)");
  });

  it("zooms the timeline to a session's extent", async () => {
    const { onZoom } = mount();
    await open();
    fireEvent.click(screen.getAllByTestId("run-timeline-session-zoom")[1]);
    expect(onZoom).toHaveBeenCalledWith(
      { from: "2026-10-05T12:00:01Z", to: "2026-10-05T14:00:00Z" },
      "Sessions 2",
    );
  });

  it("gives an instant session one second to zoom to", () => {
    expect(sessionWindow({ start: "2026-10-05T12:00:00.000Z", end: "2026-10-05T12:00:00.000Z" })).toEqual({
      from: "2026-10-05T12:00:00.000Z",
      to: "2026-10-05T12:00:01.000Z",
    });
    expect(sessionWindow({ start: null, end: null })).toBeNull();
  });

  it("warns when the understanding switch is off", async () => {
    getAgentSessions.mockResolvedValue(sessionsResponse(false));
    mount();
    await open();
    expect(screen.getByTestId("run-timeline-sessions-switch-off").textContent).toContain("will not run");
  });

  it("offers a retry when the list cannot be read", async () => {
    getAgentSessions.mockRejectedValueOnce(new Error("boom"));
    mount();
    fireEvent.click(screen.getByTestId("run-timeline-sessions-toggle"));
    fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
    await screen.findAllByTestId("run-timeline-session-row");
  });
});

describe("estimate and build", () => {
  it("estimates the checked sessions with a dry run and shows jobs and cost", async () => {
    postUnderstandingBuild.mockResolvedValue(buildResponse({}));
    mount();
    await open();
    expect(screen.getByTestId("run-timeline-sessions-estimate").hasAttribute("disabled")).toBe(true);
    fireEvent.click(screen.getByLabelText("Select session 2"));
    fireEvent.click(screen.getByTestId("run-timeline-sessions-estimate"));
    const result = await screen.findByTestId("run-timeline-sessions-estimate-result");
    expect(postUnderstandingBuild).toHaveBeenCalledWith(7, { sessions: [2], dry_run: true });
    expect(result.textContent).toContain("3 jobs");
    expect(result.textContent).toContain("900.0k input / 12.0k output");
    expect(result.textContent).toContain("$4.50");
    expect(result.textContent).toContain("cold cache, full input price");
  });

  it("builds only what was estimated: changing the selection asks for a new estimate", async () => {
    postUnderstandingBuild.mockResolvedValue(buildResponse({}));
    mount();
    await open();
    const build = screen.getByTestId("run-timeline-sessions-build");
    fireEvent.click(screen.getByLabelText("Select session 2"));
    expect(build.hasAttribute("disabled")).toBe(true);
    fireEvent.click(screen.getByTestId("run-timeline-sessions-estimate"));
    await screen.findByTestId("run-timeline-sessions-estimate-result");
    expect(build.hasAttribute("disabled")).toBe(false);
    fireEvent.click(screen.getByLabelText("Select session 1"));
    expect(build.hasAttribute("disabled")).toBe(true);
    expect(screen.queryByTestId("run-timeline-sessions-estimate-result")).toBeNull();
  });

  it("selects by time range", async () => {
    postUnderstandingBuild.mockResolvedValue(buildResponse({}));
    mount();
    await open();
    fireEvent.click(screen.getByLabelText("Time range"));
    fireEvent.change(screen.getByTestId("run-timeline-sessions-from"), { target: { value: "2026-10-05T12:30" } });
    fireEvent.click(screen.getByTestId("run-timeline-sessions-estimate"));
    await screen.findByTestId("run-timeline-sessions-estimate-result");
    const body = postUnderstandingBuild.mock.calls[0][1];
    expect(body.dry_run).toBe(true);
    expect(body.sessions).toBeUndefined();
    expect(body.from).toBe(new Date("2026-10-05T12:30").toISOString());
    expect(body.to).toBeNull();
  });

  it("says when everything chosen is already covered", async () => {
    postUnderstandingBuild.mockResolvedValue(
      buildResponse({ estimate: { jobs: 0, input_tokens: 0, output_tokens: 0, cost_usd: 0 } }),
    );
    mount();
    await open();
    fireEvent.click(screen.getByLabelText("Select session 1"));
    fireEvent.click(screen.getByTestId("run-timeline-sessions-estimate"));
    expect((await screen.findByTestId("run-timeline-sessions-estimate-result")).textContent).toContain(
      "already covered",
    );
  });

  it("shows a failed estimate", async () => {
    postUnderstandingBuild.mockRejectedValue(new Error("HTTP 422: no session intersects the time range"));
    mount();
    await open();
    fireEvent.click(screen.getByLabelText("Select session 1"));
    fireEvent.click(screen.getByTestId("run-timeline-sessions-estimate"));
    expect((await screen.findByRole("alert")).textContent).toContain("no session intersects");
  });

  it("submits for real, then polls the build until it ends and refreshes the timeline once", async () => {
    postUnderstandingBuild
      .mockResolvedValueOnce(buildResponse({}))
      .mockResolvedValueOnce(
        buildResponse({ dry_run: false, build_id: 9, rebuild_id: 5, queued: 3, merged: 1 }),
      );
    getUnderstandingBuild
      .mockResolvedValueOnce(progress("chunks", "running"))
      .mockResolvedValue(progress("done", "done"));
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { onBuildEnded } = mount();
    await open();
    fireEvent.click(screen.getByLabelText("Select session 2"));
    fireEvent.click(screen.getByTestId("run-timeline-sessions-estimate"));
    await screen.findByTestId("run-timeline-sessions-estimate-result");
    fireEvent.click(screen.getByTestId("run-timeline-sessions-build"));

    expect(await screen.findByTestId("run-timeline-sessions-progress")).toBeTruthy();
    expect(postUnderstandingBuild).toHaveBeenLastCalledWith(7, { sessions: [2], dry_run: false });
    expect(screen.getByTestId("run-timeline-sessions-progress").textContent).toContain("Build 9 queued: 3 new jobs, 1 merged");
    await waitFor(() => expect(screen.getByTestId("run-timeline-sessions-phase").textContent).toBe("Describing chunks"));
    expect(screen.getByTestId("run-timeline-sessions-job").textContent).toContain("running · $1.25");
    expect(onBuildEnded).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3100);
    });
    await waitFor(() => expect(screen.getByTestId("run-timeline-sessions-phase").textContent).toBe("Done"));
    expect(screen.getByTestId("run-timeline-sessions-total").textContent).toContain("$1.25");
    expect(onBuildEnded).toHaveBeenCalledTimes(1);

    const calls = getUnderstandingBuild.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(getUnderstandingBuild.mock.calls.length).toBe(calls);
    expect(onBuildEnded).toHaveBeenCalledTimes(1);
  });

  it("says the build waits when the understanding switch is off", async () => {
    postUnderstandingBuild
      .mockResolvedValueOnce(buildResponse({ understanding_enabled: false }))
      .mockResolvedValueOnce(
        buildResponse({ dry_run: false, understanding_enabled: false, build_id: 9, rebuild_id: 5, queued: 3 }),
      );
    getUnderstandingBuild.mockResolvedValue(progress("chunks", "running"));
    mount();
    await open();
    fireEvent.click(screen.getByLabelText("Select session 2"));
    fireEvent.click(screen.getByTestId("run-timeline-sessions-estimate"));
    await screen.findByTestId("run-timeline-sessions-estimate-result");
    fireEvent.click(screen.getByTestId("run-timeline-sessions-build"));
    expect((await screen.findByTestId("run-timeline-sessions-queued-off")).textContent).toContain(
      "Understanding is switched off, so this will not run",
    );
  });
});
