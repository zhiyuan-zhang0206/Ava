// Font-stack contract — the global font stacks must cover the CJK fallbacks
// for every platform the app runs on (user ruling 2026-08-09 #1094).
//
// Both stacks cover CJK because code and data surfaces can contain localized
// text even though application chrome uses sans by default.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const css = readFileSync(join(__dirname, "globals.css"), "utf8");

function stackOf(name: string): string {
  const line = css
    .split("\n")
    .find((l) => l.includes(`--${name}:`));
  expect(line, `--${name} must be declared`).toBeDefined();
  return line!;
}

describe("global font stacks (task #1094)", () => {
  it("sans stack covers macOS, Windows and Linux CJK", () => {
    const sans = stackOf("font-sans");
    expect(sans).toContain('"PingFang SC"');
    expect(sans).toContain('"Microsoft YaHei"');
    expect(sans).toContain('"Noto Sans SC"');
  });

  it("mono stack covers CJK too — PingFang before YaHei before Noto Sans SC, generic last", () => {
    const mono = stackOf("font-mono");
    const ping = mono.indexOf('"PingFang SC"');
    const yahei = mono.indexOf('"Microsoft YaHei"');
    const noto = mono.indexOf('"Noto Sans SC"');
    const generic = mono.lastIndexOf("monospace");
    expect(ping).toBeGreaterThan(-1);
    expect(yahei).toBeGreaterThan(ping);
    expect(noto).toBeGreaterThan(yahei);
    expect(generic).toBeGreaterThan(noto);
  });
});

describe("typography scale (task #2560)", () => {
  it.each([
    ["text-2xs", "0.625rem"],
    ["text-xs", "0.75rem"],
    ["text-sm", "0.875rem"],
    ["text-base", "1rem"],
  ])("declares --%s as %s", (name, size) => {
    expect(stackOf(name)).toContain(size);
  });
});
