import { describe, expect, it } from "vitest";

import type { RunTimelineMessage, RunTimelineResponse } from "@/lib/types";

import { buildStripLayout, coveredMessageIndexes, messageChainIndexes } from "./strip-layout";

type LayerNode = NonNullable<RunTimelineResponse["layers"]>[number];

const WINDOW = { from: "2026-09-19T00:00:00Z", to: "2026-09-19T01:00:00Z" };
const PLOT = { left: 32, width: 968 };

function message(overrides: Partial<RunTimelineMessage> & Pick<RunTimelineMessage, "key">): RunTimelineMessage {
  return {
    idx: Number(overrides.key.split(".").at(-1) ?? 1),
    ts: "2026-09-19T00:00:00Z",
    kind: "ai",
    source: null,
    chars: 100,
    parts: [{ kind: "text", chars: 100 }],
    ...overrides,
  };
}

/** The demo's two-pass `lay()` (window-relative normalization), ported
 *  verbatim into the test — the layout module must stay line-for-line
 *  equivalent to this reference. */
function demoReference(
  stampsMs: (number | null)[],
  chars: number[],
  width: number,
): { X: number[]; E: number[] } {
  const spanMs = Date.parse(WINDOW.to) - Date.parse(WINDOW.from);
  const total = chars.reduce((sum, value) => sum + value, 0);
  const times = stampsMs.map((stamp) =>
    stamp === null ? 0 : Math.max(0, Math.min(1, (stamp - Date.parse(WINDOW.from)) / spanMs)),
  );
  const shares = chars.map((value) => value / total);
  let pack = 0;
  times.forEach((time, index) => {
    pack = Math.max(time, pack) + shares[index];
  });
  const lambda = 1 / pack;
  const X: number[] = [];
  const E: number[] = [];
  let previous = 0;
  times.forEach((time, index) => {
    const x = Math.max(time, previous);
    previous = x + shares[index] * lambda;
    X.push(x * width);
    E.push(previous * width);
  });
  return { X, E };
}

describe("buildStripLayout", () => {
  it("matches the demo two-pass packing for spread messages", () => {
    const stamps = [
      Date.parse("2026-09-19T00:05:00Z"),
      Date.parse("2026-09-19T00:20:00Z"),
      Date.parse("2026-09-19T00:45:00Z"),
    ];
    const messages = [
      message({ key: "c.1", ts: "2026-09-19T00:05:00Z", chars: 120 }),
      message({ key: "c.2", ts: "2026-09-19T00:20:00Z", chars: 360 }),
      message({ key: "c.3", ts: "2026-09-19T00:45:00Z", chars: 60 }),
    ];
    const layout = buildStripLayout(messages, WINDOW, PLOT);
    const reference = demoReference(stamps, [120, 360, 60], PLOT.width);

    layout.messages.forEach((bar, index) => {
      expect(bar.left - PLOT.left).toBeCloseTo(reference.X[index], 6);
      expect(bar.left + bar.width - PLOT.left).toBeCloseTo(reference.E[index], 6);
    });
  });

  it("packs colliding messages after the previous bar, keeping x non-decreasing", () => {
    const messages = [
      message({ key: "c.1", ts: "2026-09-19T00:30:00Z", chars: 300 }),
      message({ key: "c.2", ts: "2026-09-19T00:30:00Z", chars: 100 }),
      message({ key: "c.3", ts: "2026-09-19T00:30:01Z", chars: 100 }),
    ];
    const layout = buildStripLayout(messages, WINDOW, PLOT);
    const [first, second, third] = layout.messages;
    expect(second.left).toBeCloseTo(first.left + first.width, 6);
    expect(third.left).toBeCloseTo(second.left + second.width, 6);
    expect(second.left).toBeGreaterThanOrEqual(first.left);
    expect(third.left).toBeGreaterThanOrEqual(second.left);
  });

  it("keeps bar width exactly proportional to chars", () => {
    const messages = [
      message({ key: "c.1", ts: "2026-09-19T00:10:00Z", chars: 100 }),
      message({ key: "c.2", ts: "2026-09-19T00:45:00Z", chars: 700 }),
    ];
    const layout = buildStripLayout(messages, WINDOW, PLOT);
    expect(layout.messages[1].width / layout.messages[0].width).toBeCloseTo(7, 6);
  });

  it("splits a bar into part rects proportional to part chars, contiguous", () => {
    const messages = [
      message({
        key: "c.1",
        chars: 300,
        parts: [
          { kind: "think", chars: 60 },
          { kind: "text", chars: 180 },
          { kind: "call", chars: 60 },
        ],
      }),
    ];
    const layout = buildStripLayout(messages, WINDOW, PLOT);
    const [bar] = layout.messages;
    expect(bar.parts).toHaveLength(3);
    expect(bar.parts[1].left).toBeCloseTo(bar.parts[0].left + bar.parts[0].width, 6);
    expect(bar.parts[2].left).toBeCloseTo(bar.parts[1].left + bar.parts[1].width, 6);
    expect(bar.parts[0].width / bar.parts[1].width).toBeCloseTo(60 / 180, 6);
    const total = bar.parts.reduce((sum, part) => sum + part.width, 0);
    expect(total).toBeCloseTo(bar.width, 6);
  });

  it("anchors the ts-less head message at the plot start", () => {
    const messages = [
      message({ key: "c.0", ts: null, kind: "prompt", chars: 500 }),
      message({ key: "c.1", ts: "2026-09-19T00:30:00Z", chars: 500 }),
    ];
    const layout = buildStripLayout(messages, WINDOW, PLOT);
    expect(layout.messages[0].left).toBe(PLOT.left);
    expect(layout.messages[1].left).toBeCloseTo(PLOT.left + PLOT.width / 2, 6);
  });

  it("places messages without collisions at their time positions", () => {
    const messages = [
      message({ key: "c.1", ts: "2026-09-19T00:15:00Z", chars: 1 }),
      message({ key: "c.2", ts: "2026-09-19T00:45:00Z", chars: 1 }),
    ];
    const layout = buildStripLayout(messages, WINDOW, PLOT);
    expect(layout.messages[0].left).toBeCloseTo(PLOT.left + PLOT.width * 0.25, 6);
    expect(layout.messages[1].left).toBeCloseTo(PLOT.left + PLOT.width * 0.75, 6);
  });

  it("fails visibly on an unparsable timestamp", () => {
    const messages = [message({ key: "c.1", ts: "not-a-time" })];
    expect(() => buildStripLayout(messages, WINDOW, PLOT)).toThrow(/unparsable message timestamp/);
  });

  it("returns an empty layout for no messages or a degenerate window", () => {
    expect(buildStripLayout([], WINDOW, PLOT)).toEqual({ messages: [], scale: 0 });
    const messages = [message({ key: "c.1" })];
    expect(
      buildStripLayout(messages, { from: WINDOW.from, to: WINDOW.from }, PLOT).messages,
    ).toEqual([]);
  });
});

describe("coveredMessageIndexes", () => {
  it("selects messages inside an inclusive node span", () => {
    const messages = [
      message({ key: "c.1", ts: "2026-09-19T00:10:00Z" }),
      message({ key: "c.2", ts: "2026-09-19T00:20:00Z" }),
      message({ key: "c.3", ts: "2026-09-19T00:30:00Z" }),
      message({ key: "c.4", ts: null }),
    ];
    expect(
      coveredMessageIndexes(messages, "2026-09-19T00:10:00Z", "2026-09-19T00:20:00Z"),
    ).toEqual([0, 1]);
    expect(coveredMessageIndexes(messages, "2026-09-19T00:31:00Z", "2026-09-19T00:40:00Z")).toEqual([]);
  });
});

describe("messageChainIndexes", () => {
  const layer = (
    id: string,
    depth: number,
    parent: string | null,
    start: string,
    end: string,
  ): LayerNode => ({ id, depth, parent, start, end, summary: id });

  const layers: LayerNode[] = [
    layer("L1", 0, null, "2026-09-19T00:00:00Z", "2026-09-19T01:00:00Z"),
    layer("L2a", 1, "L1", "2026-09-19T00:00:00Z", "2026-09-19T00:30:00Z"),
    layer("L2b", 1, "L1", "2026-09-19T00:30:00Z", "2026-09-19T01:00:00Z"),
    layer("L3", 2, "L2a", "2026-09-19T00:05:00Z", "2026-09-19T00:25:00Z"),
  ];

  it("returns ancestors then the deepest-narrowest covering leaf", () => {
    expect(messageChainIndexes(layers, "2026-09-19T00:10:00Z")).toEqual([0, 1, 3]);
    expect(messageChainIndexes(layers, "2026-09-19T00:40:00Z")).toEqual([0, 2]);
  });

  it("is empty for the ts-less head or nodes that do not cover", () => {
    expect(messageChainIndexes(layers, null)).toEqual([]);
    expect(messageChainIndexes(null, "2026-09-19T00:10:00Z")).toEqual([]);
  });

  it("stops cleanly when a parent id is missing", () => {
    const orphaned: LayerNode[] = [layer("L2", 1, "missing", "2026-09-19T00:00:00Z", "2026-09-19T01:00:00Z")];
    expect(messageChainIndexes(orphaned, "2026-09-19T00:10:00Z")).toEqual([0]);
  });
});
