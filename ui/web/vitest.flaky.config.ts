// Vitest config — flaky frontend tests (serial execution).
//
// These tests are timing-sensitive (DOM rendering races, animation-dependent
// assertions) and must run serially (fileParallelism false). They are excluded
// from the main parallel vitest run (vitest.config.ts excludes the same
// `src/**/flaky/**` pattern this config includes), and CI runs this config as
// its own serial step after the parallel one.
//
// To quarantine a test file, move it into a `flaky/` directory next to where
// it lived and leave a comment in the file saying why — both configs key off
// the directory pattern, no list to update.

import path from "node:path";

import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  test: {
    environment: "happy-dom",
    globals: false,
    // Same deterministic timezone as the main config — this run is its own
    // vitest process, so the pin isn't inherited.
    env: { TZ: "UTC" },
    // Same global stubs + network guard as the main config (vitest.config.ts) —
    // this run gets its own process, so setupFiles isn't inherited automatically.
    setupFiles: ["./vitest.setup.ts"],
    // Serial execution for flaky tests
    pool: "forks",
    fileParallelism: false,
    // The quarantine directory pattern — the main config excludes the same.
    include: ["src/**/flaky/**/*.test.{ts,tsx}"],
    // No coverage from flaky tests — coverage gate is satisfied by the
    // parallel stable run.
    coverage: { enabled: false },
  },
});
