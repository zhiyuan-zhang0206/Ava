// Vitest config — flaky frontend tests (serial execution).
//
// These tests are timing-sensitive (DOM rendering races, animation-dependent
// assertions) and must run serially (fileParallelism false). They are excluded
// from the main parallel vitest run.
//
// To add a test to this list, add its path pattern to the `include` array.

import path from "node:path";

import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  test: {
    environment: "happy-dom",
    globals: false,
    // Same global stubs + network guard as the main config (vitest.config.ts) —
    // this run gets its own process, so setupFiles isn't inherited automatically.
    setupFiles: ["./vitest.setup.ts"],
    // Serial execution for flaky tests
    pool: "forks",
    fileParallelism: false,
    // Add known-flaky test patterns here as they are discovered.
    include: [
      // Teardown-time ReferenceError: window is not defined — react-dom's
      // scheduler flushes leftover work after happy-dom's window has already
      // been torn down, under CI's parallel forks. Vitest logs it as an
      // Unhandled Error and fails the whole run even though every test
      // passed (seen repeatedly in CI, e.g. PRs #813/#814).
      "src/components/timeline/flaky/card.test.tsx",
    ],
    // No coverage from flaky tests — coverage gate is satisfied by the
    // parallel stable run.
    coverage: { enabled: false },
  },
});
