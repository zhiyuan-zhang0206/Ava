import { describe, expect, it } from "vitest";

import type { RunTimelineResponse, RunTimelineUnit } from "@/lib/contracts/types";

import { readoutText } from "./run-timeline-readout";

const t = ((key: string, values?: Record<string, string | number>) =>
  key === "estimatedSuffix" ? "(estimated)" : `${key}${values ? ` ${Object.values(values).join("|")}` : ""}`) as never;

const unit = (tokens: Pick<RunTimelineUnit, "context_tokens" | "generation_tokens" | "estimated">): RunTimelineUnit => ({
  kind: "output",
  i0: 3,
  i1: 4,
  start: "2026-10-04T12:00:00Z",
  end: "2026-10-04T12:01:00Z",
  source: null,
  preview: "out",
  parent: null,
  ...tokens,
});

function readout(u: RunTimelineUnit): string | null {
  return readoutText(
    { kind: "unit", i0: u.i0, i1: u.i1, unitKind: u.kind } as never,
    {
      data: { units: [u], nodes: [], requests: [], events: [] } as unknown as RunTimelineResponse,
      t,
      unitLabel: () => "Output",
      sourceLabel: (s) => s,
    },
  );
}

describe("run-timeline unit readout tokens", () => {
  it("appends the tokens of a block a request has read", () => {
    expect(readout(unit({ context_tokens: 1234, generation_tokens: null, estimated: false }))).toMatch(
      /readoutUnitTokens 1\.2k$/,
    );
  });

  it("marks an estimated share", () => {
    expect(readout(unit({ context_tokens: 1234, generation_tokens: 10, estimated: true }))).toMatch(
      /readoutUnitTokens 1\.2k \(estimated\)$/,
    );
  });

  it("shows no tokens for a block no request has read", () => {
    expect(readout(unit({ context_tokens: null, generation_tokens: null, estimated: null }))).not.toContain(
      "readoutUnitTokens",
    );
  });
});
