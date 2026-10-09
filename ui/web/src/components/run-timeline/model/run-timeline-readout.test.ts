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
      data: { units: [u], nodes: [], messages: [], events: [] } as unknown as RunTimelineResponse,
      t,
      unitLabel: () => "Output",
      sourceLabel: (s) => s,
    },
  );
}

describe("run-timeline message readout", () => {
  const message = (estimated: boolean) => ({
    idx: 5,
    start: "2026-10-04T12:00:00Z",
    end: "2026-10-04T12:00:00Z",
    session: 1,
    context_tokens: 1234,
    estimated,
    context_total: 5000,
    request: null,
  });
  const read = (m: ReturnType<typeof message>) =>
    readoutText({ kind: "message", idx: 5 }, {
      data: { units: [], nodes: [], messages: [m], events: [] } as unknown as RunTimelineResponse,
      t,
      unitLabel: () => "",
      sourceLabel: (s) => s,
    });

  it("shows the message's own weight and the context through it", () => {
    expect(read(message(false))).toMatch(/^readoutMessage 5\|2\|.*\|readoutUnitTokens 1\.2k\|5\.0k$/);
  });

  it("marks an estimated weight", () => {
    expect(read(message(true))).toMatch(/\|readoutUnitTokens 1\.2k \(estimated\)\|5\.0k$/);
  });

  it("says nothing of a message that is not in the data", () => {
    expect(readoutText({ kind: "message", idx: 9 }, {
      data: { units: [], nodes: [], messages: [], events: [] } as unknown as RunTimelineResponse,
      t,
      unitLabel: () => "",
      sourceLabel: (s) => s,
    })).toBeNull();
  });
});

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
