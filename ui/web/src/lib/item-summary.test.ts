import { describe, expect, it } from "vitest";

import { formatDuration, orderSdkCalls, sdkNamespace, summarizeCode, summarizeOutput } from "./item-summary";

describe("summarizeCode", () => {
  it("renders recorded calls by namespace, then count and method name", () => {
    const s = summarizeCode("", [
      { method: "shell.run", count: 5 },
      { method: "files.read", count: 2 },
      { method: "files.write", count: 1 },
    ]);
    expect(s.calls).toEqual([
      { method: "files.read", count: 2 },
      { method: "files.write", count: 1 },
      { method: "shell.run", count: 5 },
    ]);
    expect(s.totalCalls).toBe(8);
  });

  it("recorded calls win — the payload text never adds or removes entries", () => {
    const code = "ava.shell.run('ls')\nava.files.read('a')";
    const s = summarizeCode(code, [{ method: "files.read", count: 1 }]);
    expect(s.calls).toEqual([{ method: "files.read", count: 1 }]);
    expect(s.totalCalls).toBe(1);
  });

  it("plain python: no calls, non-blank line count", () => {
    const code = "x = 1\n\ny = 2\nprint(x + y)\n";
    const s = summarizeCode(code);
    expect(s.calls).toEqual([]);
    expect(s.lines).toBe(3);
  });

  it("trusts an authoritative empty array — comment/string mentions never count", () => {
    const code = "# ava.files.read('comment-only')\nvalue = \"ava.shell.run('string-only')\"";

    expect(summarizeCode(code, [])).toEqual({
      calls: [],
      totalCalls: 0,
      lines: 2,
    });
  });

  it.each([undefined, null])(
    "absent sdk_calls (%s) shows no calls and never scans the payload",
    (sdkCalls) => {
      const code = "ava.files.read('a')\nava.shell.run('ls')";
      expect(summarizeCode(code, sdkCalls)).toEqual({
        calls: [],
        totalCalls: 0,
        lines: 2,
      });
    },
  );
});

describe("orderSdkCalls", () => {
  it("groups interleaved namespaces alphabetically even when counts tie", () => {
    expect(orderSdkCalls([
      { method: "shell.run", count: 3 },
      { method: "files.write", count: 2 },
      { method: "agents.spawn", count: 3 },
      { method: "files.read", count: 3 },
      { method: "shell.sessions.list", count: 2 },
    ])).toEqual([
      { method: "agents.spawn", count: 3 },
      { method: "files.read", count: 3 },
      { method: "files.write", count: 2 },
      { method: "shell.run", count: 3 },
      { method: "shell.sessions.list", count: 2 },
    ]);
    expect(sdkNamespace("shell.sessions.list")).toBe("shell");
  });

  it("returns an empty list for no calls", () => {
    expect(orderSdkCalls([])).toEqual([]);
  });

  it("keeps a single call", () => {
    expect(orderSdkCalls([{ method: "files.read", count: 1 }])).toEqual([
      { method: "files.read", count: 1 },
    ]);
  });

  it("uses the whole method as the namespace when there is no dot", () => {
    expect(sdkNamespace("status")).toBe("status");
    expect(orderSdkCalls([
      { method: "status", count: 2 },
      { method: "shell.run", count: 1 },
    ])).toEqual([
      { method: "shell.run", count: 1 },
      { method: "status", count: 2 },
    ]);
  });

  it("keeps namespace order independent of counts", () => {
    expect(orderSdkCalls([
      { method: "shell.run", count: 100 },
      { method: "agents.spawn", count: 1 },
    ])).toEqual([
      { method: "agents.spawn", count: 1 },
      { method: "shell.run", count: 100 },
    ]);
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
