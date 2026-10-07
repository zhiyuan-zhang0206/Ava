import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const copy = vi.hoisted(() => vi.fn().mockResolvedValue(true));
vi.mock("@/lib/format/clipboard", () => ({ copyToClipboard: copy }));

import {
  PythonCode,
  __resetHighlighterCacheForTests,
  __setHighlighterImportForTests,
  preloadPythonCodeHighlighter,
} from "./python-code";

// python-code.tsx caches the highlighter chunk in module-level state
// (resolvedHighlight / highlightPromise) so a real prefetch survives
// re-renders without re-fetching — that's the whole point of prefetching.
// Each test below needs to control whether that cache starts warm (a prior
// idle/intent prefetch already resolved it) or cold, so every test clears it
// via the test-only reset hook. (vi.resetModules() + a dynamic re-import
// would also isolate this module's own state, but it reinstantiates
// prism-react-renderer itself — an ESM dependency Vite doesn't externalize —
// as a second, partially initialized copy that crashes when rendered.)
beforeEach(() => {
  __resetHighlighterCacheForTests();
});

// Every test that installs a stub import must restore the real one — a leak
// would poison every later test in this file with a permanently-rejecting
// (or stale-resolving) dynamic import.
afterEach(() => {
  __setHighlighterImportForTests(null);
});

it("highlights immediately when the chunk was already prefetched", async () => {
  // Stand-in for CodeHighlighterPreloader's idle prefetch, or the
  // pointer-enter/focus "intent" signal on a card's collapsed header —
  // either way, the chunk is already resolved before this block mounts.
  await preloadPythonCodeHighlighter();

  const first = 'print("<pending>")';
  const latest = `${first}\nprint(2)`;
  const { container, rerender } = render(<PythonCode code={first} streaming />);
  // No waitFor here: the highlighter was prefetched, so the very first
  // render already reads the resolved module from cache — no async chunk
  // load gates this paint.
  expect(container.querySelector("pre")?.textContent).toBe(first);
  expect(container.querySelector("pre .token.string")?.textContent).toBe('"<pending>"');
  expect(container.querySelector("pre .animate-pulse")).not.toBeNull();
  expect(screen.getByRole("button", { name: "Copy code" })).toBeDefined();

  rerender(<PythonCode code={latest} streaming />);
  expect(container.querySelectorAll(".token-line")).toHaveLength(2);
  expect(container.querySelector("pre")?.textContent).toContain("print(2)");
  fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
  await waitFor(() => expect(copy).toHaveBeenCalledWith(latest));

  rerender(<PythonCode code={latest} />);
  expect(container.querySelector("pre .animate-pulse")).toBeNull();
  expect(container.querySelectorAll("pre .token.number")).toHaveLength(1);
  expect(screen.getAllByRole("button", { name: "Copied" })).toHaveLength(1);
});

it("falls back to plain text, then highlights once an un-prefetched chunk resolves", async () => {
  const code = 'print("<pending>")';
  const { container } = render(<PythonCode code={code} streaming />);

  // Cold render, nothing prefetched it ahead of time: the highlighter chunk
  // is still in flight, so the first paint must show plain, unescaped-HTML
  // source text — and still the copy control + streaming cursor — rather
  // than a blank body (the bug this component used to have).
  expect(container.querySelector("pre")?.textContent).toBe(code);
  expect(container.querySelector("pre .token")).toBeNull();
  expect(container.querySelector("pre .animate-pulse")).not.toBeNull();
  expect(screen.getByRole("button", { name: "Copy code" })).toBeDefined();

  // The chunk resolves shortly after mount (this component's own effect
  // requests it); the tree then swaps to the highlighted version.
  await waitFor(() => {
    expect(container.querySelector("pre .token.string")?.textContent).toBe('"<pending>"');
  });
  expect(container.querySelector("pre .animate-pulse")).not.toBeNull();

  // The copy control survives the swap and still copies the current source.
  fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
  await waitFor(() => expect(copy).toHaveBeenCalledWith(code));
});

// Regression coverage for the P1 adversarial-review finding on Ava #3463: a
// rejected dynamic import (chunk-load failure) used to be cached forever by
// `highlightPromise ??= ...` with no `.catch`, permanently bricking syntax
// highlighting after one transient failure and raising an unhandled
// rejection at every later call site. loadHighlight() now clears its cache on
// rejection, and every call site attaches a rejection handler.
it("a rejected import falls back to plain text without an unhandled rejection", async () => {
  const unhandled: unknown[] = [];
  const onUnhandledRejection = (reason: unknown) => unhandled.push(reason);
  process.on("unhandledRejection", onUnhandledRejection);
  // Follow-up P2 from the #3463 QA pass: loadHighlight()'s .catch used to
  // swallow the rejection with no log line at all, so a chunk-load failure
  // was indistinguishable from "nothing went wrong" in the console. Silence
  // the real console.warn (this failure is expected) and assert it fired.
  const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);

  try {
    __setHighlighterImportForTests(() => Promise.reject(new Error("chunk load failed")));

    const code = 'print("<pending>")';
    const { container } = render(<PythonCode code={code} streaming />);

    // Cold render, chunk load fails: stays on the plain-text fallback rather
    // than throwing or rendering nothing.
    expect(container.querySelector("pre")?.textContent).toBe(code);
    expect(container.querySelector("pre .token")).toBeNull();

    // Give the rejected promise's microtask chain (loadHighlight's .catch,
    // the effect's second .then handler) a full turn to run.
    await waitFor(() => {
      expect(container.querySelector("pre")?.textContent).toBe(code);
    });
    // A macrotask turn too — Node schedules the unhandledRejection check
    // after the microtask queue drains, so this is where an unfixed version
    // of loadHighlight would actually report it.
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(container.querySelector("pre .token")).toBeNull();
    expect(warnSpy).toHaveBeenCalled();
  } finally {
    process.off("unhandledRejection", onUnhandledRejection);
    warnSpy.mockRestore();
  }

  expect(unhandled).toEqual([]);
});

// Locks the single-choke-point dedup: loadHighlight()'s own .catch is part
// of the `highlightPromise` chain itself, so for one real import attempt it
// runs exactly once no matter how many call sites share that in-flight
// promise — several code blocks each firing preloadPythonCodeHighlighter()
// on hover/focus while one attempt is still in flight must not each log the
// same underlying failure separately.
it("warns only once per failed import attempt even with several concurrent preload calls", async () => {
  const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);

  try {
    __setHighlighterImportForTests(() => Promise.reject(new Error("chunk load failed")));

    await Promise.all([
      preloadPythonCodeHighlighter(),
      preloadPythonCodeHighlighter(),
      preloadPythonCodeHighlighter(),
    ]);

    expect(warnSpy).toHaveBeenCalledTimes(1);
  } finally {
    warnSpy.mockRestore();
  }
});

it("retries and highlights on the next call after a prior rejection", async () => {
  __setHighlighterImportForTests(() => Promise.reject(new Error("chunk load failed")));

  const code = 'print("<pending>")';
  const first = render(<PythonCode code={code} streaming />);
  await waitFor(() => {
    expect(first.container.querySelector("pre")?.textContent).toBe(code);
  });
  expect(first.container.querySelector("pre .token")).toBeNull();
  first.unmount();

  // The failure above must have cleared the module-level cache (not just
  // left `highlightPromise` pointing at a dead, permanently-rejected
  // promise) — restoring the real import and retrying now succeeds.
  __setHighlighterImportForTests(null);
  await preloadPythonCodeHighlighter();

  const retry = render(<PythonCode code={code} streaming />);
  expect(retry.container.querySelector("pre .token.string")?.textContent).toBe('"<pending>"');
});
