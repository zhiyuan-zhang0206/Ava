import { describe, expect, it } from "vitest";

import {
  formatDuration,
  formatTokensCompact,
  summarizeCode,
  summarizeOutput,
} from "./item-summary";

describe("summarizeCode", () => {
  it("groups ava.* calls by full method path, descending", () => {
    const code = [
      "ava.files.read('a')",
      "ava.files.write('b', x)",
      "ava.shell.run('ls')",
      "ava.files.read('c')",
    ].join("\n");
    const s = summarizeCode(code);
    expect(s.calls).toEqual([
      { method: "files.read", count: 2 },
      { method: "files.write", count: 1 },
      { method: "shell.run", count: 1 },
    ]);
    expect(s.totalCalls).toBe(4);
  });

  it("ties break by method name", () => {
    const s = summarizeCode("ava.shell.run()\nava.agents.spawn()");
    expect(s.calls).toEqual([
      { method: "agents.spawn", count: 1 },
      { method: "shell.run", count: 1 },
    ]);
  });

  it("does not count attribute reads (no trailing paren)", () => {
    const s = summarizeCode("x = ava.self.AGENT_ID\nava.self.log('hi')");
    expect(s.calls).toEqual([{ method: "self.log", count: 1 }]);
    expect(s.totalCalls).toBe(1);
  });

  it("counts a direct callable like ava.help(ava)", () => {
    const s = summarizeCode("ava.help(ava)");
    expect(s.calls).toEqual([{ method: "help", count: 1 }]);
  });

  it("does not match a non-ava identifier ending in ava", () => {
    const s = summarizeCode("guava.fs.read()\nlava.x()");
    expect(s.totalCalls).toBe(0);
  });

  it("tolerates whitespace before the paren", () => {
    expect(summarizeCode("ava.fs.read ('a')").totalCalls).toBe(1);
  });

  it("falls back to non-blank line count for plain python", () => {
    const code = "x = 1\n\ny = 2\nprint(x + y)\n";
    const s = summarizeCode(code);
    expect(s.calls).toEqual([]);
    expect(s.lines).toBe(3);
  });
});

describe("summarizeOutput", () => {
  it("counts lines and chars, no trailing-newline phantom line", () => {
    const s = summarizeOutput("a\nb\nc\n");
    expect(s.lines).toBe(3);
    expect(s.chars).toBe(6);
    expect(s.hasError).toBe(false);
  });

  it("empty output is zero lines", () => {
    expect(summarizeOutput("").lines).toBe(0);
  });

  it("flags a traceback", () => {
    const out = [
      "Traceback (most recent call last):",
      '  File "<stdin>", line 1, in <module>',
      "ValueError: bad",
    ].join("\n");
    expect(summarizeOutput(out).hasError).toBe(true);
  });

  it("flags a bare trailing error line", () => {
    expect(summarizeOutput("some output\nKeyError: 'x'").hasError).toBe(true);
  });

  it("does not flag the word error mid-sentence", () => {
    expect(summarizeOutput("no error here, all good").hasError).toBe(false);
  });
});

describe("formatTokensCompact", () => {
  it("formats across magnitudes", () => {
    expect(formatTokensCompact(820)).toBe("820");
    expect(formatTokensCompact(1234)).toBe("1.2k");
    expect(formatTokensCompact(3_400_000)).toBe("3.4M");
  });
});

describe("formatDuration", () => {
  it("sub-0.1s floors to 0.1s", () => {
    expect(formatDuration(0)).toBe("0.1s");
    expect(formatDuration(50)).toBe("0.1s");
    expect(formatDuration(99)).toBe("0.1s");
  });

  it("0.1s-1s rounds to 0.1s", () => {
    expect(formatDuration(150)).toBe("0.2s");
    expect(formatDuration(340)).toBe("0.3s");
    expect(formatDuration(949)).toBe("0.9s");
    expect(formatDuration(950)).toBe("1s");
  });

  it("1s-60s rounds to whole seconds", () => {
    expect(formatDuration(1_200)).toBe("1s");
    expect(formatDuration(8_400)).toBe("8s");
    expect(formatDuration(45_600)).toBe("46s");
  });

  it("over a minute switches to m/s", () => {
    expect(formatDuration(72_000)).toBe("1m 12s");
    expect(formatDuration(120_000)).toBe("2m");
  });
});
