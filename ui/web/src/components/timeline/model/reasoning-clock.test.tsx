// The shared live clock: every live timeline clock rides one interval, the
// interval exists only while a clock is live, and the busy gate stops clocks
// whose live stamp outlived the turn.

import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BackendTimelineItem } from "@/lib/contracts/types";
import { isLiveCode, LiveClockGate, liveClockSubscriberCount, useNow } from "./reasoning-clock";

function Clock({ id, active }: { id: string; active: boolean }) {
  const now = useNow(active);
  return <span data-testid={id}>{now}</span>;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(1_000_000);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useNow shared ticker", () => {
  it("drives every live clock from one interval and the same timestamp", () => {
    render(
      <>
        <Clock id="a" active />
        <Clock id="b" active />
        <Clock id="c" active={false} />
      </>,
    );
    expect(liveClockSubscriberCount()).toBe(2);
    expect(vi.getTimerCount()).toBe(1);

    act(() => { vi.advanceTimersByTime(300); });
    expect(screen.getByTestId("a").textContent).toBe("1000300");
    expect(screen.getByTestId("b").textContent).toBe("1000300");
    // An inactive clock never subscribes and keeps its mount value.
    expect(screen.getByTestId("c").textContent).toBe("1000000");
  });

  it("removes the interval once the last live clock stops", () => {
    const { rerender, unmount } = render(<Clock id="a" active />);
    expect(vi.getTimerCount()).toBe(1);
    rerender(<Clock id="a" active={false} />);
    expect(liveClockSubscriberCount()).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
    rerender(<Clock id="a" active />);
    expect(vi.getTimerCount()).toBe(1);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("does not tick a live clock while the agent is not busy", () => {
    const { rerender } = render(
      <LiveClockGate value={false}>
        <Clock id="a" active />
      </LiveClockGate>,
    );
    expect(vi.getTimerCount()).toBe(0);
    act(() => { vi.advanceTimersByTime(500); });
    expect(screen.getByTestId("a").textContent).toBe("1000000");

    rerender(
      <LiveClockGate value>
        <Clock id="a" active />
      </LiveClockGate>,
    );
    act(() => { vi.advanceTimersByTime(200); });
    expect(screen.getByTestId("a").textContent).toBe("1000700");
  });
});

describe("isLiveCode", () => {
  const streaming: BackendTimelineItem = {
    item_id: "4.1",
    kind: "agent_code",
    source: null,
    payload: "x = 1",
    created_at: null,
    inbound_id: null,
    show_timestamp: true,
    codeStartedAt: 999_000,
  };

  it("is live while the code block streams", () => {
    expect(isLiveCode(streaming)).toBe(true);
  });

  it("stops once the committed duration lands, even if the stamp was never cleared", () => {
    // A missed exec_start / turn-end leaves codeStartedAt on the item.
    expect(isLiveCode({ ...streaming, code_elapsed_ms: 1_200 })).toBe(false);
  });
});
