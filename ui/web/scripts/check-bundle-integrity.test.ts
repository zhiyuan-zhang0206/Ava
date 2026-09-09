import { execFileSync } from "node:child_process";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { describe, expect, it } from "vitest";

import { checkBundleIntegrity, degradedChunks } from "./check-bundle-integrity.mjs";

const scriptsDir = path.dirname(fileURLToPath(import.meta.url));

describe("degradedChunks", () => {
  it("flags chunks carrying the cancelAnimationFrame(0) corpse", () => {
    const chunks = [
      { path: "a.js", src: "window.cancelAnimationFrame(0)" },
      { path: "b.js", src: "window.cancelAnimationFrame(1)" },
      { path: "c.js", src: "window.requestAnimationFrame(() => {})" },
    ];
    expect(degradedChunks(chunks)).toEqual(["a.js"]);
  });

  it("accepts chunks without the corpse", () => {
    const chunks = [
      { path: "a.js", src: "cancelAnimationFrame(a), cancelAnimationFrame(window.rAF)" },
    ];
    expect(degradedChunks(chunks)).toEqual([]);
  });
});

describe("checkBundleIntegrity", () => {
  it("passes on a healthy chunk directory", async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), "ava-bundle-integrity-"));
    try {
      const chunksDir = path.join(dir, ".next", "static", "chunks");
      await mkdir(chunksDir, { recursive: true });
      await writeFile(path.join(chunksDir, "a.js"), "var x = 1;");
      expect(() => checkBundleIntegrity(chunksDir)).not.toThrow();
    } finally {
      await rm(dir, { recursive: true });
    }
  });

  it("throws on a chunk directory carrying the corpse", async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), "ava-bundle-integrity-"));
    try {
      const chunksDir = path.join(dir, ".next", "static", "chunks");
      await mkdir(chunksDir, { recursive: true });
      await writeFile(path.join(chunksDir, "a.js"), "window.cancelAnimationFrame(0)");
      expect(() => checkBundleIntegrity(chunksDir)).toThrow(
        "contains cancelAnimationFrame(0)",
      );
    } finally {
      await rm(dir, { recursive: true });
    }
  });

  it("scans nested chunk directories", async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), "ava-bundle-integrity-"));
    try {
      const chunksDir = path.join(dir, ".next", "static", "chunks");
      const nested = path.join(chunksDir, "app", "control");
      await mkdir(nested, { recursive: true });
      await writeFile(path.join(chunksDir, "a.js"), "var x = 1;");
      await writeFile(path.join(nested, "b.js"), "self.cancelAnimationFrame(0)");
      expect(() => checkBundleIntegrity(chunksDir)).toThrow(
        "contains cancelAnimationFrame(0)",
      );
    } finally {
      await rm(dir, { recursive: true });
    }
  });

  it("throws when the chunk directory has no chunks", async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), "ava-bundle-integrity-"));
    try {
      const chunksDir = path.join(dir, ".next", "static", "chunks");
      await mkdir(chunksDir, { recursive: true });
      expect(() => checkBundleIntegrity(chunksDir)).toThrow("no chunks found");
    } finally {
      await rm(dir, { recursive: true });
    }
  });
});

describe("CLI entrypoint", () => {
  it("does not run the check when imported", async () => {
    const tempDir = await mkdtemp(path.join(os.tmpdir(), "ava-bundle-integrity-"));
    try {
      expect(() =>
        execFileSync(
          process.execPath,
          [
            "--input-type=module",
            "--eval",
            `import ${JSON.stringify(pathToFileURL(path.join(scriptsDir, "check-bundle-integrity.mjs")).href)}`,
          ],
          { cwd: tempDir, stdio: "pipe" },
        ),
      ).not.toThrow();
    } finally {
      await rm(tempDir, { recursive: true });
    }
  });

  it("exits 0 on a healthy directory and 1 on a degraded one", async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), "ava-bundle-integrity-"));
    try {
      const chunksDir = path.join(dir, ".next", "static", "chunks");
      await mkdir(chunksDir, { recursive: true });
      const script = path.join(scriptsDir, "check-bundle-integrity.mjs");
      await writeFile(path.join(chunksDir, "a.js"), "var x = 1;");
      expect(() => execFileSync(process.execPath, [script], { cwd: dir, stdio: "pipe" })).not.toThrow();
      await writeFile(path.join(chunksDir, "a.js"), "window.cancelAnimationFrame(0)");
      expect(() => execFileSync(process.execPath, [script], { cwd: dir, stdio: "pipe" })).toThrow();
    } finally {
      await rm(dir, { recursive: true });
    }
  });
});
