import { readFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { describe, expect, it } from "vitest";

import { checkFirstLoadJs, parseRouteBundleStats } from "./check-first-load-js.mjs";

const scriptsDir = path.dirname(fileURLToPath(import.meta.url));

describe("checkFirstLoadJs", () => {
  it("rejects malformed build diagnostics", () => {
    expect(() =>
      parseRouteBundleStats([{ route: "/", firstLoadUncompressedJsBytes: "invalid" }]),
    ).toThrow("Next build diagnostics do not contain valid route bundle statistics");
  });

  it("does not read build diagnostics when imported", async () => {
    const tempDir = await mkdtemp(path.join(os.tmpdir(), "ava-first-load-js-"));
    try {
      expect(() =>
        execFileSync(
          process.execPath,
          [
            "--input-type=module",
            "--eval",
            `import ${JSON.stringify(pathToFileURL(path.join(scriptsDir, "check-first-load-js.mjs")).href)}`,
          ],
          { cwd: tempDir, stdio: "pipe" },
        ),
      ).not.toThrow();
    } finally {
      await rm(tempDir, { recursive: true });
    }
  });

  it("rejects a first-load route bundle above its budget", async () => {
    const diagnostics: unknown = JSON.parse(
      await readFile(path.join(scriptsDir, "fixtures/route-bundle-stats.json"), "utf8"),
    );
    const stats = parseRouteBundleStats(diagnostics);

    expect(() => checkFirstLoadJs(stats, "/", 1_200_000)).toThrow(
      "First-load JavaScript for / is 1207268 bytes, exceeding its 1200000 byte budget",
    );
  });
});
