import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

const copy = vi.hoisted(() => vi.fn().mockResolvedValue(true));
vi.mock("@/lib/clipboard", () => ({ copyToClipboard: copy }));

import { PythonCode, __resetHighlighterCacheForTests, preloadPythonCodeHighlighter } from "./python-code";

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
