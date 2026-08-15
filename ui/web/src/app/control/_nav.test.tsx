// Scroll-spy anchor selection — the pure core of the two-level nav highlight.
// The regression this pins: a sub-anchor must be able to win over its wrapping
// parent section (config-gateway over config), which the old intersection-set
// approach couldn't do.

import { describe, expect, it } from "vitest";

import { pickActiveAnchor } from "./_nav";

const IDS = ["status", "config", "config-gateway", "config-agent-runner", "config-common", "presets"];

describe("pickActiveAnchor", () => {
  it("picks the last anchor whose top is at/above the line", () => {
    // Scrolled so status + config + config-gateway tops have passed the line,
    // the rest are still below it.
    const tops: Record<string, number> = {
      status: -400,
      config: -120,
      "config-gateway": 40,
      "config-agent-runner": 300,
      "config-common": 560,
      presets: 900,
    };
    expect(pickActiveAnchor(IDS, 96, (id) => tops[id])).toBe("config-gateway");
  });

  it("advances from the parent section to a sub-anchor as it scrolls past", () => {
    // At the top of Config: only config's own top has crossed → parent active.
    const atSectionTop: Record<string, number> = {
      status: -200,
      config: 40,
      "config-gateway": 200,
      "config-agent-runner": 480,
      "config-common": 740,
      presets: 1000,
    };
    expect(pickActiveAnchor(IDS, 96, (id) => atSectionTop[id])).toBe("config");
    // Scroll down into the agent-runner zone → the sub-anchor wins over config.
    const inAgentRunner: Record<string, number> = {
      status: -900,
      config: -620,
      "config-gateway": -300,
      "config-agent-runner": 40,
      "config-common": 300,
      presets: 640,
    };
    expect(pickActiveAnchor(IDS, 96, (id) => inAgentRunner[id])).toBe("config-agent-runner");
  });

  it("defaults to the first id when nothing has scrolled past yet", () => {
    const allBelow: Record<string, number> = {
      status: 200,
      config: 500,
      "config-gateway": 700,
      "config-agent-runner": 900,
      "config-common": 1100,
      presets: 1300,
    };
    expect(pickActiveAnchor(IDS, 96, (id) => allBelow[id])).toBe("status");
  });

  it("skips missing anchors (topOf returns null)", () => {
    const tops: Record<string, number | null> = {
      status: -100,
      config: 40,
      "config-gateway": null, // not in the DOM (e.g. hidden on a remote config view)
      "config-agent-runner": 500,
      "config-common": 700,
      presets: 900,
    };
    expect(pickActiveAnchor(IDS, 96, (id) => tops[id] ?? null)).toBe("config");
  });
});
