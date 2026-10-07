import { render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

const preload = vi.hoisted(() => vi.fn());
vi.mock("@/components/content/python-code", () => ({ preloadPythonCodeHighlighter: preload }));

import { CodeHighlighterPreloader } from "./code-highlighter-preloader";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

it("prefetches the highlighter once requestIdleCallback fires, and cancels it on unmount", () => {
  let idleCallback: (() => void) | undefined;
  const requestIdleCallback = vi.fn((cb: () => void) => {
    idleCallback = cb;
    return 7;
  });
  const cancelIdleCallback = vi.fn();
  vi.stubGlobal("requestIdleCallback", requestIdleCallback);
  vi.stubGlobal("cancelIdleCallback", cancelIdleCallback);

  const { unmount } = render(<CodeHighlighterPreloader />);
  expect(requestIdleCallback).toHaveBeenCalledTimes(1);
  expect(preload).not.toHaveBeenCalled();

  // Idle callback fires only once the browser actually goes idle — not at
  // mount time.
  idleCallback?.();
  expect(preload).toHaveBeenCalledTimes(1);

  unmount();
  expect(cancelIdleCallback).toHaveBeenCalledWith(7);
});

it("falls back to a setTimeout delay when requestIdleCallback doesn't exist", () => {
  vi.useFakeTimers();
  vi.stubGlobal("requestIdleCallback", undefined);

  render(<CodeHighlighterPreloader />);
  expect(preload).not.toHaveBeenCalled();

  vi.advanceTimersByTime(2000);
  expect(preload).toHaveBeenCalledTimes(1);
});
