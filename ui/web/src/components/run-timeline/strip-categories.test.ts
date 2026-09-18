import { describe, expect, it } from "vitest";

import type { RunTimelineMessage } from "@/lib/types";

import {
  legendMatches,
  STRIP_LEGEND_CATEGORIES,
  stripMessageClass,
  stripPartClass,
  type StripColorClass,
} from "./strip-categories";

function message(kind: RunTimelineMessage["kind"], source: string | null = null): RunTimelineMessage {
  return { key: "c.1", idx: 1, ts: "2026-09-19T00:00:00Z", kind, source, chars: 10, parts: [] };
}

describe("stripMessageClass", () => {
  it("maps every server message kind (exhaustive — a new kind must fail this)", () => {
    const expected: [RunTimelineMessage["kind"], StripColorClass][] = [
      ["prompt", "prompt"],
      ["note", "note"],
      ["compact", "compact"],
      ["inbound", "ib-sys"],
      ["attach", "other"],
      ["ai", "think"],
      ["exec", "out"],
    ];
    for (const [kind, colorClass] of expected) {
      expect(stripMessageClass(message(kind)), kind).toBe(colorClass);
    }
  });

  it("splits inbound messages by source", () => {
    expect(stripMessageClass(message("inbound", "agent:405"))).toBe("ib-agent");
    expect(stripMessageClass(message("inbound", "user"))).toBe("ib-user");
    expect(stripMessageClass(message("inbound", "system"))).toBe("ib-sys");
    expect(stripMessageClass(message("inbound", "watcher:12"))).toBe("ib-sys");
    expect(stripMessageClass(message("inbound", null))).toBe("ib-sys");
  });

  it("fails visibly on a message kind the console does not know", () => {
    expect(() => stripMessageClass(message("future" as unknown as RunTimelineMessage["kind"]))).toThrow(
      /unmapped message kind/,
    );
  });
});

describe("stripPartClass", () => {
  const expected: [RunTimelineMessage["parts"][number]["kind"], string | null, StripColorClass][] = [
    ["think", null, "think"],
    ["text", null, "text"],
    ["call", null, "call"],
    ["out", null, "out"],
    ["note", null, "note"],
    ["compact", null, "compact"],
    ["inbound", "agent:405", "ib-agent"],
    ["inbound", "user", "ib-user"],
    ["inbound", "schedule:1", "ib-sys"],
    ["attach", null, "other"],
    ["prompt", null, "prompt"],
  ];

  it("maps every server part kind (exhaustive — a new kind must fail this)", () => {
    for (const [kind, source, colorClass] of expected) {
      expect(stripPartClass(kind, source), `${kind}/${source}`).toBe(colorClass);
    }
  });

  it("fails visibly on a part kind the console does not know", () => {
    expect(() =>
      stripPartClass(
        "future" as unknown as RunTimelineMessage["parts"][number]["kind"],
        null,
      ),
    ).toThrow(/unmapped part kind/);
  });
});

describe("legendMatches", () => {
  it("matches a category to its own color class", () => {
    for (const category of STRIP_LEGEND_CATEGORIES) {
      if (category === "sys") continue;
      expect(legendMatches(category, category), category).toBe(true);
    }
  });

  it("lights both compact and system-inbound under the sys row", () => {
    expect(legendMatches("compact", "sys")).toBe(true);
    expect(legendMatches("ib-sys", "sys")).toBe(true);
    expect(legendMatches("ib-agent", "sys")).toBe(false);
    expect(legendMatches("prompt", "sys")).toBe(false);
  });

  it("never lights the other bucket", () => {
    for (const category of STRIP_LEGEND_CATEGORIES) {
      expect(legendMatches("other", category), category).toBe(false);
    }
  });

  it("does not cross-match unrelated categories", () => {
    expect(legendMatches("think", "text")).toBe(false);
    expect(legendMatches("compact", "prompt")).toBe(false);
    expect(legendMatches("ib-user", "ib-agent")).toBe(false);
  });
});
