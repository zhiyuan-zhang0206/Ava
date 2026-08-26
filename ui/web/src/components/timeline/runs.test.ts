// runs.ts — pure grouping / classification unit tests. No DOM.

import { describe, expect, it } from "vitest";

import type { BackendTimelineItem } from "@/lib/types";

import {
  classifyItem,
  formatTurnSummary,
  formatTurnTiming,
  groupIntoTurns,
  inboundKind,
  summarizeTurn,
  type TimelineGroup,
} from "./runs";

// Minimal item factory — only the fields classification reads (kind + source),
// plus whatever the caller overrides (duration / sdk_calls fields for the
// timing/SDK-aggregation tests below).
function item(
  kind: BackendTimelineItem["kind"],
  source: string | null = null,
  id = `${idSeq++}.0`,
  extra: Partial<BackendTimelineItem> = {},
): BackendTimelineItem {
  return {
    item_id: id,
    kind,
    source,
    payload: "",
    created_at: "2026-07-21T00:00:00Z",
    inbound_id: null,
    show_timestamp: true,
    ...extra,
  };
}
let idSeq = 1;

describe("inboundKind", () => {
  it("maps agent / human / system sources", () => {
    expect(inboundKind("agent:42")).toBe("agent");
    expect(inboundKind("user")).toBe("human");
    expect(inboundKind("ui:page:board")).toBe("human");
    expect(inboundKind("system")).toBe("system");
    expect(inboundKind("system:update")).toBe("system");
    expect(inboundKind("watcher:7")).toBe("system");
    expect(inboundKind("shell:3")).toBe("system");
    expect(inboundKind("schedule:9")).toBe("system");
    expect(inboundKind("self:update")).toBe("system");
    expect(inboundKind(null)).toBe("system");
  });
});

describe("classifyItem", () => {
  it("agent_chat and human inbound are primary", () => {
    expect(classifyItem(item("agent_chat"))).toBe("primary");
    expect(classifyItem(item("inbound_chat", "user"))).toBe("primary");
    expect(classifyItem(item("inbound_chat", "ui:page:board"))).toBe("primary");
    // Attached media stays visible — never folded into a turn's details.
    expect(classifyItem(item("attach"))).toBe("primary");
  });

  it("agent + system inbound are secondary", () => {
    expect(classifyItem(item("inbound_chat", "agent:5"))).toBe("secondary");
    expect(classifyItem(item("inbound_chat", "system"))).toBe("secondary");
    expect(classifyItem(item("inbound_chat", "watcher:1"))).toBe("secondary");
  });

  it("thinking / code / output / compact / system_prompt are secondary", () => {
    for (const k of [
      "agent_reasoning",
      "agent_code",
      "code_output",
      "inbound_compact_summary",
      "inbound_compact_request",
      "system_prompt",
    ] as const) {
      expect(classifyItem(item(k))).toBe("secondary");
    }
  });

  it("card-family system_marker is secondary; ephemeral is bare", () => {
    // note / memory / lifecycle sources render as cards → fold like notes.
    expect(classifyItem(item("system_marker", "heartbeat"))).toBe("secondary");
    expect(classifyItem(item("system_marker", "memory"))).toBe("secondary");
    expect(classifyItem(item("system_marker", "lifecycle_terminate"))).toBe("secondary");
    // unknown / payload-identified marker has no card → bare.
    expect(classifyItem(item("system_marker", null))).toBe("bare");
    expect(classifyItem(item("system_marker", "cancelled"))).toBe("bare");
  });
});

describe("summarizeTurn / formatTurnSummary", () => {
  it("counts action items (thinking/code/output) — the specific metrics shown in the header", () => {
    const run = [
      item("agent_reasoning"),
      item("agent_code"),
      item("code_output"),
      item("agent_reasoning"),
      item("inbound_chat", "agent:2"),
      item("system_marker", "heartbeat"),
    ];
    const s = summarizeTurn(run);
    expect(s.total).toBe(6);
    expect(s.thinking).toBe(2);
    expect(s.code).toBe(1);
    expect(s.output).toBe(1);
    // The heartbeat note lands in the systemNotes bucket — every member kind
    // is counted somewhere so the header is never blank.
    expect(formatTurnSummary(s)).toBe("1 system note · 1 agent message · 2 thinking · 1 code · 1 output");
  });

  it("omits zero-count action kinds from the summary line", () => {
    const s = summarizeTurn([
      item("agent_code"),
      item("agent_code"),
      item("code_output"),
    ]);
    expect(s.thinking).toBe(0);
    expect(s.code).toBe(2);
    expect(s.output).toBe(1);
    expect(formatTurnSummary(s)).toBe("2 code · 1 output");
  });

  it("a turn with no actions still summarizes every member (never an empty header)", () => {
    const s = summarizeTurn([
      item("inbound_chat", "agent:1"),
      item("system_marker", "heartbeat"),
    ]);
    expect(s.thinking).toBe(0);
    expect(s.code).toBe(0);
    expect(s.output).toBe(0);
    expect(formatTurnSummary(s)).toBe("1 system note · 1 agent message");
  });

  it("a turn with only thinking shows just the thinking count", () => {
    const s = summarizeTurn([
      item("agent_reasoning"),
      item("agent_reasoning"),
      item("agent_reasoning"),
    ]);
    expect(formatTurnSummary(s)).toBe("3 thinking");
  });
});

describe("summarizeTurn — timing aggregation", () => {
  it("sums committed reasoning_ms across thinking blocks, codeElapsedMs across code blocks, execMs across output blocks", () => {
    const run = [
      item("agent_reasoning", null, undefined, { reasoning_ms: 8_000 }),
      item("agent_code", null, undefined, { codeElapsedMs: 3_000 }),
      item("code_output", null, undefined, { exec_ms: 1_500 }),
      item("agent_reasoning", null, undefined, { reasoning_ms: 4_000 }),
      item("agent_code", null, undefined, { codeElapsedMs: 2_500 }),
      item("code_output", null, undefined, { exec_ms: 2_500 }),
    ];
    const s = summarizeTurn(run);
    expect(s.thinkingMs).toBe(12_000);
    expect(s.codeMs).toBe(5_500);
    expect(s.execMs).toBe(4_000);
    // formatDuration rounds to whole seconds above 1s: 5.5s → 6s.
    expect(formatTurnTiming(s)).toBe("Thought for 12s · Wrote code for 6s · Ran for 4s");
  });

  it("codeMs is zero when no agent_code items carry codeElapsedMs", () => {
    const run = [item("agent_code", null, undefined, {})];
    expect(summarizeTurn(run).codeMs).toBe(0);
  });

  // Regression (symptom: turn header never showed "Wrote code for Xs" after a
  // snapshot commit): summarizeTurn only read the frontend-frozen
  // codeElapsedMs, which every snapshot merge drops — the committed backend
  // code_elapsed_ms (#755) was ignored here even though the per-card summary
  // already used it.
  it("codeMs prefers the committed backend code_elapsed_ms over the frontend-frozen codeElapsedMs", () => {
    const committed = [item("agent_code", null, undefined, { code_elapsed_ms: 7_000 })];
    expect(summarizeTurn(committed).codeMs).toBe(7_000);
    const both = [
      item("agent_code", null, undefined, { code_elapsed_ms: 7_000, codeElapsedMs: 1_000 }),
    ];
    expect(summarizeTurn(both).codeMs).toBe(7_000);
  });

  it("falls back to the frozen reasoningElapsedMs when reasoning_ms was never committed (interrupted stream)", () => {
    const run = [item("agent_reasoning", null, undefined, { reasoningElapsedMs: 3_000 })];
    expect(summarizeTurn(run).thinkingMs).toBe(3_000);
  });

  it("a turn with no reasoning/execution items has zero timing and no timing line", () => {
    const run = [item("inbound_chat", "agent:1"), item("system_marker", "heartbeat")];
    const s = summarizeTurn(run);
    expect(s.thinkingMs).toBe(0);
    expect(s.execMs).toBe(0);
    expect(formatTurnTiming(s)).toBeNull();
  });

  it("drops the zero side — thinking only", () => {
    const s = summarizeTurn([item("agent_reasoning", null, undefined, { reasoning_ms: 500 })]);
    expect(formatTurnTiming(s)).toBe("Thought for 0.5s");
  });
});

describe("summarizeTurn — SDK call aggregation", () => {
  it("aggregates call counts by method across code items, sourced from the backend AST field (not a text scan)", () => {
    const run = [
      item("agent_code", null, undefined, {
        payload: "ava.files.read('a')",
        sdk_calls: [{ method: "files.read", count: 1 }],
      }),
      item("agent_code", null, undefined, {
        payload: "ava.files.read('b')\nava.shell.run('ls')",
        sdk_calls: [
          { method: "files.read", count: 1 },
          { method: "shell.run", count: 1 },
        ],
      }),
    ];
    const s = summarizeTurn(run);
    expect(s.sdkCalls).toEqual([
      { method: "files.read", count: 2 },
      { method: "shell.run", count: 1 },
    ]);
  });

  it("trusts a genuinely empty backend sdk_calls ([]) as zero — does not regex-scan the payload text", () => {
    const run = [
      item("agent_code", null, undefined, {
        payload: "# example: ava.files.read('x')\nprint('hi')",
        sdk_calls: [],
      }),
    ];
    expect(summarizeTurn(run).sdkCalls).toEqual([]);
  });

  it("skips (does not regex-scan) an item whose sdk_calls field is absent, e.g. never committed", () => {
    const run = [item("agent_code", null, undefined, { payload: "ava.agents.spawn()", sdk_calls: null })];
    expect(summarizeTurn(run).sdkCalls).toEqual([]);
  });

  it("a turn with no code items has an empty sdkCalls list", () => {
    expect(summarizeTurn([item("agent_reasoning")]).sdkCalls).toEqual([]);
  });
});

describe("summarizeTurn — workedMs", () => {
  // workedMs is now thinkingMs + codeMs + execMs — the sum of each work
  // block's backend-measured duration, not wall-clock between items.
  // This naturally excludes system notes / restart markers (itemOwnMs
  // returns 0 for them) and works even when the first item lacks created_at
  // (e.g. a streaming block whose duration is measured by the frontend clock).

  it("workedMs = thinkingMs + codeMs + execMs", () => {
    const run = [
      item("agent_reasoning", null, "a", { reasoning_ms: 8_000 }),
      item("agent_code", null, "b", { code_elapsed_ms: 3_000 }),
      item("code_output", null, "c", { exec_ms: 1_500 }),
      item("agent_reasoning", null, "d", { reasoning_ms: 4_000 }),
      item("agent_code", null, "e", { code_elapsed_ms: 2_500 }),
      item("code_output", null, "f", { exec_ms: 2_500 }),
    ];
    const s = summarizeTurn(run);
    // 8+3+1.5+4+2.5+2.5 = 21.5s
    expect(s.workedMs).toBe(21_500);
  });

  it("returns zero workedMs when no item carries duration data", () => {
    const run = [
      item("agent_reasoning", null, "a"),
      item("agent_code", null, "b"),
      item("code_output", null, "c"),
    ];
    expect(summarizeTurn(run).workedMs).toBe(0);
  });

  // Regression (symptom: a single code block never showed "Worked for Xs"
  // under the old wall-clock logic because created_at marks a block's
  // START — first→last created_at is 0 for a single-block turn). With the
  // duration-sum approach, the block's own code_elapsed_ms becomes workedMs.
  it("single-block turn works for as long as the block itself ran", () => {
    const s = summarizeTurn([
      item("agent_code", null, "a", { code_elapsed_ms: 5_000 }),
    ]);
    expect(s.workedMs).toBe(5_000);
  });

  it("falls back to frozen frontend clocks when backend durations are absent", () => {
    const s = summarizeTurn([
      item("agent_reasoning", null, "a", { reasoningElapsedMs: 3_000 }),
      item("agent_code", null, "b", { codeElapsedMs: 2_000 }),
    ]);
    expect(s.workedMs).toBe(5_000);
  });

  // Regression (symptom: agent restart inserted a system marker at T0, then
  // the agent started working at T1 — the old wall-clock logic included the
  // idle gap, showing "Worked for T1-T0" even though the agent did nothing
  // during that gap). With the duration-sum approach, system notes contribute
  // 0 ms (itemOwnMs returns 0), so workedMs reflects only actual work.
  it("system notes / restart markers contribute zero to workedMs", () => {
    const run = [
      item("system_marker", "lifecycle_restart", "a", {
        created_at: "2026-07-21T00:00:00.000Z", // T0: restart (not work)
      }),
      item("agent_reasoning", null, "b", {
        created_at: "2026-07-21T00:05:00.000Z",
        reasoning_ms: 10_000,
      }),
      item("agent_code", null, "c", {
        created_at: "2026-07-21T00:05:15.000Z",
        code_elapsed_ms: 5_000,
      }),
      item("code_output", null, "d", {
        created_at: "2026-07-21T00:05:22.000Z",
        exec_ms: 2_000,
      }),
    ];
    const s = summarizeTurn(run);
    // 10 + 5 + 2 = 17s of actual work. Not 324s (wall-clock from T0).
    expect(s.workedMs).toBe(17_000);
  });

  // Timestamps are not an input to workedMs at all. #1007 had to teach the
  // summary to find the first STAMPED member because the live clock counted
  // wall-clock from it; the live clock now counts the same block durations as
  // the completed clock, so an unstamped turn is not a special case — the
  // behavioral guard for it lives in timeline.test.tsx (a turn whose members
  // all lack created_at still shows a live timer).
  it("ignores created_at entirely — an unstamped turn still measures its work", () => {
    const s = summarizeTurn([
      item("system_prompt", null, "0.0", { created_at: null }),
      item("agent_reasoning", null, "1.0", { created_at: null, reasoning_ms: 4_000 }),
    ]);
    expect(s.workedMs).toBe(4_000);
  });

  // Regression (symptom: a turn of only system notes rendered a completely
  // blank header): the systemNotes bucket guarantees a non-empty summary line
  // for any non-empty turn.
  it("a system-note-only turn summarizes as '1 system note' (never blank)", () => {
    const one = summarizeTurn([item("system_marker", "sdk_hint")]);
    expect(one.systemNotes).toBe(1);
    expect(formatTurnSummary(one)).toBe("1 system note");
    const several = summarizeTurn([
      item("system_marker", "lifecycle_restart"),
      item("inbound_chat", "system"),
    ]);
    expect(several.systemNotes).toBe(2);
    expect(formatTurnSummary(several)).toBe("2 system notes");
  });
});

function kinds(groups: TimelineGroup[]): string[] {
  return groups.map((g) =>
    g.kind === "turn" ? `turn(${g.items.length}@${g.startIndex})` : `single(${g.item.kind}@${g.index})`,
  );
}

describe("groupIntoTurns", () => {
  it("collapseTurns off → every item is its own single group", () => {
    const items = [item("agent_reasoning"), item("agent_code"), item("agent_chat")];
    const groups = groupIntoTurns(items, { collapseTurns: false, liveIndex: null });
    expect(groups.every((g) => g.kind === "single")).toBe(true);
    expect(groups).toHaveLength(3);
  });

  it("folds a stretch of secondary items between two primary replies", () => {
    const items = [
      item("agent_chat"), // 0 primary
      item("agent_reasoning"), // 1
      item("agent_code"), // 2
      item("code_output"), // 3
      item("agent_chat"), // 4 primary
    ];
    const groups = groupIntoTurns(items, { collapseTurns: true, liveIndex: null });
    expect(kinds(groups)).toEqual(["single(agent_chat@0)", "turn(3@1)", "single(agent_chat@4)"]);
    const turn = groups[1];
    if (turn.kind !== "turn") throw new Error("expected turn");
    expect(turn.summary.total).toBe(3);
    expect(turn.startIndex).toBe(1);
  });

  it("a lone secondary item becomes a turn block (always collapsible)", () => {
    const items = [item("agent_chat"), item("inbound_chat", "agent:9"), item("agent_chat")];
    const groups = groupIntoTurns(items, { collapseTurns: true, liveIndex: null });
    // The lone secondary item is now wrapped in a turn block, not a single.
    expect(kinds(groups)).toEqual([
      "single(agent_chat@0)",
      "turn(1@1)",
      "single(agent_chat@2)",
    ]);
  });

  it("bare markers break turns and pass through as singles", () => {
    const items = [
      item("agent_reasoning"), // 0
      item("agent_code"), // 1
      item("system_marker", null), // 2 bare (ephemeral)
      item("agent_reasoning"), // 3
      item("code_output"), // 4
    ];
    const groups = groupIntoTurns(items, { collapseTurns: true, liveIndex: null });
    expect(kinds(groups)).toEqual(["turn(2@0)", "single(system_marker@2)", "turn(2@3)"]);
  });

  it("human inbound breaks a turn", () => {
    const items = [
      item("agent_reasoning"), // 0
      item("agent_code"), // 1
      item("inbound_chat", "user"), // 2 primary
      item("agent_reasoning"), // 3
      item("agent_code"), // 4
    ];
    const groups = groupIntoTurns(items, { collapseTurns: true, liveIndex: null });
    expect(kinds(groups)).toEqual(["turn(2@0)", "single(inbound_chat@2)", "turn(2@3)"]);
  });

  it("streaming secondary items are always groupable — no peeling", () => {
    // Even with liveIndex set, secondary items should fold into turns.
    const items = [
      item("agent_reasoning"), // 0
      item("agent_code"), // 1
      item("code_output"), // 2
      item("agent_reasoning"), // 3 <- "live"
    ];
    const groups = groupIntoTurns(items, { collapseTurns: true, liveIndex: 3 });
    // All four fold into one turn — the live item is not peeled.
    expect(kinds(groups)).toEqual(["turn(4@0)"]);
  });

  it("a single streaming secondary item becomes a turn block immediately", () => {
    const items = [
      item("agent_chat"), // 0 primary
      item("agent_reasoning"), // 1 live secondary (first streaming chunk)
    ];
    const groups = groupIntoTurns(items, { collapseTurns: true, liveIndex: 1 });
    // The streaming item is wrapped in a turn block from the start.
    expect(kinds(groups)).toEqual([
      "single(agent_chat@0)",
      "turn(1@1)",
    ]);
  });

  it("initial context items fold into a turn — system prompt and memories go into first detail block", () => {
    // Initial context: system prompt, compact summary, memory notes, guidance
    // markers — these should fold into a turn block like any other secondary
    // items so they appear inside the first detail block, not standalone.
    const items = [
      item("system_prompt"), // 0
      item("inbound_compact_summary"), // 1
      item("inbound_compact_request"), // 2
      item("system_marker", "memory"), // 3
      item("system_marker", "heartbeat"), // 4 note
      item("system_marker", "agent_id"), // 5 note
    ];
    const groups = groupIntoTurns(items, { collapseTurns: true, liveIndex: null });
    // All six fold into a single turn.
    expect(kinds(groups)).toEqual(["turn(6@0)"]);
    const turn = groups[0];
    if (turn.kind !== "turn") throw new Error("expected turn");
    expect(turn.summary.total).toBe(6);
    // The two guidance notes land in the systemNotes bucket.
    expect(formatTurnSummary(turn.summary)).toBe("system prompt · 2 compact summaries · 1 memory · 2 system notes");
  });

  it("context-only items after real content fold normally (e.g. restart lifecycle marker + new system prompt)", () => {
    // After a primary message, context items are part of a real turn transition
    // (e.g. restart → new system prompt → memory notes). They should fold like
    // any other secondary items.
    const items = [
      item("agent_chat"), // 0 primary — seenNonContext = true
      item("system_marker", "lifecycle_restart"), // 1 context
      item("system_prompt"), // 2 context
      item("system_marker", "memory"), // 3 context
      item("system_marker", "agent_memory"), // 4 context
      item("agent_chat"), // 5 primary
    ];
    const groups = groupIntoTurns(items, { collapseTurns: true, liveIndex: null });
    // Items 1-4 fold into a turn between the two agent_chat primaries.
    expect(kinds(groups)).toEqual([
      "single(agent_chat@0)",
      "turn(4@1)",
      "single(agent_chat@5)",
    ]);
    // Verify the turn summary counts action items (context items like
    // system_prompt/system_marker have no thinking/code/output count).
    const turn = groups[1];
    if (turn.kind !== "turn") throw new Error("expected turn");
    expect(turn.summary.thinking).toBe(0);
    expect(turn.summary.code).toBe(0);
    expect(turn.summary.output).toBe(0);
    // The lifecycle_restart marker lands in the systemNotes bucket.
    expect(formatTurnSummary(turn.summary)).toBe("system prompt · 2 memories · 1 system note");
  });

  it("context items mixed with agent actions before first primary — all fold together", () => {
    // All secondary items — context and actions alike — now fold into turns
    // regardless of position. The initial context is part of the first turn.
    const items = [
      item("system_prompt"), // 0
      item("system_marker", "memory"), // 1
      item("agent_reasoning"), // 2
      item("agent_code"), // 3
      item("code_output"), // 4
      item("agent_chat"), // 5 primary
    ];
    const groups = groupIntoTurns(items, { collapseTurns: true, liveIndex: null });
    // 0-4 fold into one turn (all secondary); 5 is primary single.
    expect(kinds(groups)).toEqual([
      "turn(5@0)",
      "single(agent_chat@5)",
    ]);
    const turn = groups[0];
    if (turn.kind !== "turn") throw new Error("expected turn");
    expect(turn.summary.total).toBe(5);
  });
});
