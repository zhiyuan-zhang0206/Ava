import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useThrottledStreaming } from "./use-throttled-streaming";

describe("useThrottledStreaming", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("leading edge: first value while live shows immediately", () => {
    const { result } = renderHook(
      ({ v, live }) => useThrottledStreaming(v, live, 100),
      { initialProps: { v: "a", live: true } },
    );
    expect(result.current).toBe("a");
  });

  it("holds the shown value within the interval, then flushes the latest on the trailing edge", () => {
    const { result, rerender } = renderHook(
      ({ v, live }) => useThrottledStreaming(v, live, 100),
      { initialProps: { v: "a", live: true } },
    );
    // a flushed at t=0 (leading). Within the window, updates are withheld.
    rerender({ v: "ab", live: true });
    rerender({ v: "abc", live: true });
    expect(result.current).toBe("a");
    // Trailing-edge timer fires with the LATEST value, not the one at schedule time.
    act(() => {
      vi.advanceTimersByTime(100);
    });
    expect(result.current).toBe("abc");
  });

  it("settle (live → false) shows the final value immediately, even mid-interval", () => {
    const { result, rerender } = renderHook(
      ({ v, live }) => useThrottledStreaming(v, live, 100),
      { initialProps: { v: "a", live: true } },
    );
    rerender({ v: "ab", live: true }); // withheld within window
    expect(result.current).toBe("a");
    rerender({ v: "abFINAL", live: false }); // settle
    expect(result.current).toBe("abFINAL");
  });

  it("not live from the start is a pass-through", () => {
    const { result, rerender } = renderHook(
      ({ v, live }) => useThrottledStreaming(v, live, 100),
      { initialProps: { v: "x", live: false } },
    );
    rerender({ v: "xy", live: false });
    expect(result.current).toBe("xy");
  });
});
