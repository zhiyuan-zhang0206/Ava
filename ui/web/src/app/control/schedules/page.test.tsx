// /control/schedules page tests — loading/error/empty/data states, NL draft
// (spawns writer + navigates), start/stop toggle, and delete with confirm.
//
// happy-dom + RTL + real QueryClient (mock at the api layer); next/navigation
// router is mocked.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { ScheduleSummary, ScheduleView } from "@/lib/types";

// Mock PythonCode — avoid running Prism syntax highlighting in tests
vi.mock("@/components/python-code", () => ({
  PythonCode: ({ code }: { code: string }) => (
    <pre data-testid="python-code">{code}</pre>
  ),
}));

import SchedulesPage from "./page";

const FULL: ScheduleView = {
  id: 1,
  name: "memory-arbiter",
  description: "consolidate nightly",
  command: "python schedule.py",
  enabled: true,
  status: "running",
  last_error: "boom traceback",
  created_at: "2026-06-01T00:00:00Z",
  updated_at: "2026-06-29T09:00:00Z",
  script: "print('hi')",
};

const pushSpy = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: pushSpy }) }));

afterEach(cleanup);
beforeEach(() => {
  vi.restoreAllMocks();
  pushSpy.mockReset();
});

function makeQc() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function wrap(ui: React.ReactElement) {
  return render(<QueryClientProvider client={makeQc()}>{ui}</QueryClientProvider>);
}

const SCHEDULE: ScheduleSummary = {
  id: 1,
  name: "memory-arbiter",
  description: "consolidate nightly",
  command: "python schedule.py",
  enabled: true,
  status: "running",
  last_error: null,
  created_at: "2026-06-01T00:00:00Z",
  updated_at: "2026-06-29T09:00:00Z",
};

describe("SchedulesPage", () => {
  it("shows loading spinner", () => {
    vi.spyOn(api, "listSchedules").mockReturnValue(new Promise<ScheduleSummary[]>(() => undefined));
    wrap(<SchedulesPage />);
    expect(document.querySelector(".animate-spin")).toBeTruthy();
  });

  it("shows error state", async () => {
    vi.spyOn(api, "listSchedules").mockRejectedValue(new Error("fail"));
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText(/Couldn't load schedules/)).toBeTruthy());
  });

  it("shows empty state", async () => {
    vi.spyOn(api, "listSchedules").mockResolvedValue([]);
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText(/No schedules configured/)).toBeTruthy());
  });

  it("renders a schedule with name + status", async () => {
    vi.spyOn(api, "listSchedules").mockResolvedValue([SCHEDULE]);
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText("memory-arbiter")).toBeTruthy());
    expect(screen.getByText("running")).toBeTruthy();
  });

  it("description cell carries a title attribute for the truncated full text", async () => {
    vi.spyOn(api, "listSchedules").mockResolvedValue([SCHEDULE]);
    wrap(<SchedulesPage />);
    const cell = await screen.findByText("consolidate nightly");
    expect(cell.getAttribute("title")).toBe("consolidate nightly");
  });

  it("draft: spawns a writer and navigates to the conversation", async () => {
    vi.spyOn(api, "listSchedules").mockResolvedValue([]);
    const draft = vi.spyOn(api, "draftSchedule").mockResolvedValue({ agent_id: 42 });
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText(/No schedules configured/)).toBeTruthy());

    const input = screen.getByPlaceholderText(/Describe a scheduled task/);
    fireEvent.change(input, { target: { value: "consolidate nightly" } });
    fireEvent.click(screen.getByRole("button", { name: "Describe" }));

    await waitFor(() => expect(draft).toHaveBeenCalledWith("consolidate nightly"));
    await waitFor(() => expect(pushSpy).toHaveBeenCalledWith("/"));
  });

  it("toggle: a running schedule stops", async () => {
    vi.spyOn(api, "listSchedules").mockResolvedValue([SCHEDULE]);
    const stop = vi.spyOn(api, "stopSchedule").mockResolvedValue({ ...SCHEDULE, script: "x", enabled: false });
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText("memory-arbiter")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(stop).toHaveBeenCalledWith(1));
  });

  it("delete: confirms then deletes", async () => {
    vi.spyOn(api, "listSchedules").mockResolvedValue([SCHEDULE]);
    const del = vi.spyOn(api, "deleteSchedule").mockResolvedValue({ status: "deleted" });
    window.confirm = vi.fn(() => true);
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText("memory-arbiter")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith(1));
  });

  it("toggle: a stopped schedule starts", async () => {
    const stopped: ScheduleSummary = { ...SCHEDULE, enabled: false, status: "stopped" };
    vi.spyOn(api, "listSchedules").mockResolvedValue([stopped]);
    const start = vi.spyOn(api, "startSchedule").mockResolvedValue({ ...FULL, enabled: false });
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText("memory-arbiter")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(start).toHaveBeenCalledWith(1));
  });

  it("restart: calls restartSchedule", async () => {
    vi.spyOn(api, "listSchedules").mockResolvedValue([SCHEDULE]);
    const restart = vi.spyOn(api, "restartSchedule").mockResolvedValue(FULL);
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText("memory-arbiter")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Restart" }));
    await waitFor(() => expect(restart).toHaveBeenCalledWith(1));
  });

  it("raw form: fills + creates a schedule", async () => {
    vi.spyOn(api, "listSchedules").mockResolvedValue([]);
    const create = vi.spyOn(api, "createSchedule").mockResolvedValue(FULL);
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText(/No schedules configured/)).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: /New \(raw\)/ }));
    fireEvent.change(screen.getByPlaceholderText("memory-arbiter"), {
      target: { value: "my-sched" },
    });
    const textareas = document.querySelectorAll("textarea");
    fireEvent.change(textareas[textareas.length - 1], { target: { value: "print(1)" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() =>
      expect(create).toHaveBeenCalledWith(expect.objectContaining({ name: "my-sched" })),
    );
  });

  it("expand: shows script, last_error, logs, and run history", async () => {
    vi.spyOn(api, "listSchedules").mockResolvedValue([SCHEDULE]);
    vi.spyOn(api, "getSchedule").mockResolvedValue(FULL);
    vi.spyOn(api, "scheduleLogs").mockResolvedValue({ source: "session", lines: ["log-line-1"] });
    vi.spyOn(api, "scheduleRuns").mockResolvedValue([
      { id: 7, ran_at: "2026-06-29T09:00:00Z", ok: true, agent_id: 5, note: "did the thing" },
    ]);
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText("memory-arbiter")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Expand" }));
    await waitFor(() => expect(screen.getByText("print('hi')")).toBeTruthy());
    expect(screen.getByText(/boom traceback/)).toBeTruthy();
    await waitFor(() => expect(screen.getByText("log-line-1")).toBeTruthy());
    expect(screen.getByText(/did the thing/)).toBeTruthy();
  });

  it("expand: an in-progress run renders as … (ok=null), an interrupted one as ✗", async () => {
    // QA P3-6: the run-history "…" path (ok IS NULL = genuinely in-progress)
    // must render — a regression here would leave the live-run state blank.
    vi.spyOn(api, "listSchedules").mockResolvedValue([SCHEDULE]);
    vi.spyOn(api, "getSchedule").mockResolvedValue(FULL);
    vi.spyOn(api, "scheduleLogs").mockResolvedValue({ source: "none", lines: [] });
    vi.spyOn(api, "scheduleRuns").mockResolvedValue([
      { id: 9, ran_at: "2026-06-30T09:00:00Z", ok: null, agent_id: null, note: null },
      { id: 8, ran_at: "2026-06-29T09:00:00Z", ok: false, agent_id: null, note: "interrupted" },
      { id: 7, ran_at: "2026-06-29T08:00:00Z", ok: true, agent_id: null, note: null },
    ]);
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText("memory-arbiter")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Expand" }));
    await waitFor(() => expect(screen.getByText(/…/)).toBeTruthy());
    expect(screen.getByText(/interrupted/)).toBeTruthy();
  });

  it("does not refetch expanded schedule logs before 15 seconds", async () => {
    vi.spyOn(api, "listSchedules").mockResolvedValue([SCHEDULE]);
    vi.spyOn(api, "getSchedule").mockResolvedValue(FULL);
    const logs = vi.spyOn(api, "scheduleLogs").mockResolvedValue({ source: "none", lines: [] });
    vi.spyOn(api, "scheduleRuns").mockResolvedValue([]);
    const view = wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText("memory-arbiter")).toBeTruthy());

    vi.useFakeTimers();
    try {
      fireEvent.click(screen.getByRole("button", { name: "Expand" }));
      await act(async () => {
        await Promise.resolve();
      });
      expect(logs).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(14_999);
      });
      expect(logs).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
      expect(logs).toHaveBeenCalledTimes(2);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("edit: opens the editor and saves a new script", async () => {
    vi.spyOn(api, "listSchedules").mockResolvedValue([SCHEDULE]);
    vi.spyOn(api, "getSchedule").mockResolvedValue(FULL);
    vi.spyOn(api, "scheduleLogs").mockResolvedValue({ source: "none", lines: [] });
    vi.spyOn(api, "scheduleRuns").mockResolvedValue([]);
    const update = vi.spyOn(api, "updateSchedule").mockResolvedValue({ ...FULL, script: "print(2)" });
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText("memory-arbiter")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Save" })).toBeTruthy());
    const textareas = document.querySelectorAll("textarea");
    fireEvent.change(textareas[textareas.length - 1], { target: { value: "print(2)" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
  });

  it("raw form cancel closes it; a disabled schedule shows (off)", async () => {
    const off: ScheduleSummary = { ...SCHEDULE, enabled: false, status: "stopped" };
    vi.spyOn(api, "listSchedules").mockResolvedValue([off]);
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText("(off)")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /New \(raw\)/ }));
    expect(screen.getByText("Create schedule")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByText("Create schedule")).toBeNull());
  });
});
