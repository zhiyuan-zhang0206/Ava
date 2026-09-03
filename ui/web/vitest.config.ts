// Vitest config — React hook test environment.
//
// `environment: "happy-dom"` provides a DOM for React 19 (needed by
// renderHook / act / component render). 2-3x faster startup than jsdom
// with consistent behavior (recommended by the testing-library team).
//
// `setupFiles`: vitest.setup.ts installs global stubs happy-dom lacks (a
// firing IntersectionObserver — see that file) and registers RTL cleanup in a
// global afterEach — RTL's own auto-cleanup needs `afterEach` on globalThis,
// which `globals: false` never provides.
//
// `globals: false`: keeps `import { describe, it, expect } from "vitest"`
// explicit, avoids polluting globals, and improves IDE go-to-definition.

import path from "node:path";

import { configDefaults, coverageConfigDefaults, defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    // Mirror tsconfig.json `paths` so vitest can resolve `@/...` imports
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  test: {
    // happy-dom provides the window / document React render needs. Pure
    // function tests (timeline.test.ts / api.test.ts / agent-tree.test.ts)
    // don't depend on the DOM, but once happy-dom is loaded the overhead
    // is negligible — keeping a single env simplifies things.
    environment: "happy-dom",
    globals: false,
    setupFiles: ["./vitest.setup.ts"],
    reporters: [
      ["default", {}],
      ["junit", { outputFile: "./junit.xml", addFileAttribute: true }],
    ],
    // Pin the timezone: timestamp-rendering tests (timeline.test.tsx) assert
    // on formatted output, and `Intl` renders "GMT+8"-style zone names on
    // non-UTC dev machines that the assertions (written against CI's UTC)
    // reject. One deterministic zone everywhere ends the local-vs-CI split.
    env: { TZ: "UTC" },
    // `.builds/` holds the e2e frontend build dirs (frontend_proc copies the
    // tree there per e2e session); the default include would otherwise sweep
    // their copied tests and run the suite twice (and fail on the copy's
    // missing fixtures). Build artifacts are never test sources.
    // `src/**/flaky/**` is the quarantine convention: a timing-sensitive test
    // file lives under a `flaky/` directory, which this run skips and the
    // serial CI step (vitest.flaky.config.ts) picks up by the same pattern.
    exclude: [...configDefaults.exclude, ".builds/**", "src/**/flaky/**"],
    // fork pool: each test file runs in its own V8 isolate (no DOM leakage).
    // fileParallelism true: files run concurrently using available CPUs.
    pool: "forks",
    fileParallelism: true,
    // coverage: v8 provider, json-summary for CI soft-threshold gate parsing.
    // text reporter for humans, json-summary for scripts.
    coverage: {
      provider: "v8",
      reporter: ["text", "json-summary"],
      include: ["src/**"],
      // `src/**` sweeps in the *.ava.okf.md docs that live next to the code
      // they describe (their path IS the OKF hierarchy, so they can't be
      // relocated). v8 hands each to the
      // transform pipeline, which throws RolldownError PARSE_ERROR and then
      // drops the file from coverage anyway — five lines of red-looking log per
      // frontend CI run, growing with the doc tree. Exclude them up front so
      // the outcome is the same and the log stays quiet.
      // Spread the defaults: naming `exclude` replaces them wholesale, which
      // would otherwise pull test files and type-only modules into the
      // denominator and move the coverage gate.
      exclude: [...coverageConfigDefaults.exclude, "src/**/*.md"],
    },
  },
});
