import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render as rtlRender, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RunTimelineResponse, UserSettingListResponse } from "@/lib/types";

const { getRunTimeline, getSettings } = vi.hoisted(() => ({
  getRunTimeline: vi.fn<
    (
      agentId: number,
      options?: {
        from?: string;
        to?: string;
        level?: "turn" | "bucket";
        bucket?: string;
        session?: "compact" | "current";
      },
    ) => Promise<RunTimelineResponse>
  >(),
  getSettings: vi.fn<() => Promise<UserSettingListResponse>>(),
}));

vi.mock("@/lib/api", () => ({ api: { getRunTimeline, getSettings } }));

import RunTimelinePage from "./page";

const NOW = new Date("2026-09-05T14:26:00.000Z");

function render() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return rtlRender(
    <QueryClientProvider client={queryClient}>
      <RunTimelinePage params={Promise.resolve({ agentId: "42" })} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
  getRunTimeline.mockReset();
  getRunTimeline.mockReturnValue(new Promise(() => undefined));
  getSettings.mockReset();
  getSettings.mockResolvedValue({ settings: [] });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("RunTimelinePage initial window", () => {
  it("does not request the timeline while settings are pending", async () => {
    getSettings.mockReturnValue(new Promise(() => undefined));

    const { getByRole } = render();

    await waitFor(
      () => {
        expect(getSettings).toHaveBeenCalledTimes(1);
        expect(
          getByRole("heading", { name: "Run timeline — agent 42" }),
        ).toBeTruthy();
      },
      { timeout: 500 },
    );
    vi.advanceTimersByTime(60_000);

    expect(getRunTimeline).not.toHaveBeenCalled();
  });

  it("requests the most recent two hours by default", async () => {
    render();

    await waitFor(() =>
      expect(getRunTimeline).toHaveBeenCalledWith(42, {
        from: "2026-09-05T12:26:00.000Z",
        to: "2026-09-05T14:26:00.000Z",
        session: "compact",
      }),
    );
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
  });

  it("requests the full session after the user resets the window", async () => {
    const { getByRole } = render();

    await waitFor(() => expect(getRunTimeline).toHaveBeenCalledTimes(1));

    fireEvent.click(getByRole("button", { name: "Reset window" }));

    await waitFor(() =>
      expect(getRunTimeline).toHaveBeenNthCalledWith(2, 42, { session: "compact" }),
    );
    expect(getRunTimeline).toHaveBeenCalledTimes(2);
    const resetOptions = getRunTimeline.mock.calls[1]?.[1];
    expect(resetOptions).not.toHaveProperty("from");
    expect(resetOptions).not.toHaveProperty("to");
  });

  it("uses the configured positive window duration", async () => {
    getSettings.mockResolvedValue({
      settings: [
        {
          key: "display.run_timeline_window_hours",
          value: 4,
          updated_at: "2026-09-05T14:00:00.000Z",
        },
      ],
    });

    render();

    await waitFor(() =>
      expect(getRunTimeline).toHaveBeenCalledWith(42, {
        from: "2026-09-05T10:26:00.000Z",
        to: "2026-09-05T14:26:00.000Z",
        session: "compact",
      }),
    );
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
  });

  it.each([0, -1, Number.NaN, "4"])("falls back to two hours for invalid value %s", async (value) => {
    getSettings.mockResolvedValue({
      settings: [
        {
          key: "display.run_timeline_window_hours",
          value,
          updated_at: "2026-09-05T14:00:00.000Z",
        },
      ],
    });

    render();

    await waitFor(() =>
      expect(getRunTimeline).toHaveBeenCalledWith(42, {
        from: "2026-09-05T12:26:00.000Z",
        to: "2026-09-05T14:26:00.000Z",
        session: "compact",
      }),
    );
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
  });
});
