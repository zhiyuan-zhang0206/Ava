// Timeline reducer unit tests — pure function, no jsdom / RTL.

import { describe, expect, it } from "vitest";

import {
  applySystemEvent,
  isEventForThread,
  mergeSnapshotWithStreaming,
  parseItemId,
  sortByItemId,
} from "./timeline";
import type { BackendTimelineItem, SystemEvent } from "./types";

let _id_counter_fix = 0;
function item(overrides: Partial<BackendTimelineItem>): BackendTimelineItem {
  return {
    item_id: `fix.${++_id_counter_fix}`,
    kind: "agent_chat",
    source: null,
    payload: "",
    created_at: "2026-01-01T00:00:00Z",
    inbound_id: null,
    show_timestamp: true,
    ...overrides,
  };
}

function kinds(items: BackendTimelineItem[]): string[] {
  return items.map((i) => i.kind);
}

describe("parseItemId", () => {
  it("standard format '5.1' → [5, 1]", () => {
    expect(parseItemId("5.1")).toEqual([5, 1]);
  });

  it("multi-digit msg_idx '123.456' → [123, 456]", () => {
    expect(parseItemId("123.456")).toEqual([123, 456]);
  });

  it("msg_idx=0 '0.0' → [0, 0]", () => {
    expect(parseItemId("0.0")).toEqual([0, 0]);
  });

  it("ephemeral marker '_marker.xxx' → null", () => {
    expect(parseItemId("_marker.exec_start")).toBeNull();
    expect(parseItemId("_marker.compact_done")).toBeNull();
  });

  it("non-standard format → null", () => {
    expect(parseItemId("not-an-id")).toBeNull();
    expect(parseItemId("")).toBeNull();
    expect(parseItemId("a.b")).toBeNull();
  });

  it("more than three parts '1.2.3' → null", () => {
    expect(parseItemId("1.2.3")).toBeNull();
  });
});

describe("sortByItemId", () => {
  it("standard sort: ascending by msg_idx, then by block_idx within the same msg_idx", () => {
    const items: BackendTimelineItem[] = [
      item({ item_id: "2.0", kind: "agent_chat", payload: "third" }),
      item({ item_id: "1.0", kind: "inbound_chat", payload: "first" }),
      item({ item_id: "1.1", kind: "agent_reasoning", payload: "second" }),
    ];
    const sorted = sortByItemId(items);
    expect(sorted.map((i) => i.item_id)).toEqual(["1.0", "1.1", "2.0"]);
  });

  it("ephemeral markers at the end; stable items keep msg_idx order", () => {
    const items: BackendTimelineItem[] = [
      item({ item_id: "_marker.err", kind: "system_marker", payload: "error:x" }),
      item({ item_id: "2.0", kind: "agent_code", payload: "code" }),
      item({ item_id: "_marker.compact", kind: "system_marker", payload: "compact_done" }),
      item({ item_id: "1.0", kind: "inbound_chat", payload: "msg" }),
    ];
    const sorted = sortByItemId(items);
    expect(sorted.map((i) => i.item_id)).toEqual([
      "1.0",
      "2.0",
      "_marker.err",
      "_marker.compact",
    ]);
  });

  it("does not mutate input", () => {
    const items: BackendTimelineItem[] = [
      item({ item_id: "2.0" }),
      item({ item_id: "1.0" }),
    ];
    const copy = [...items];
    sortByItemId(items);
    expect(items).toEqual(copy);
  });
});

describe("applySystemEvent", () => {
  it("code_start appends an empty agent_code entry", () => {
    const ev: SystemEvent = { role: "code_start", item_id: "5.1", agent_id: 1 };
    const result = applySystemEvent([], ev);
    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({
      item_id: "5.1",
      kind: "agent_code",
      payload: "",
    });
  });

  it("code_delta appends payload under the same item_id", () => {
    let items: BackendTimelineItem[] = [];
    items = applySystemEvent(items, { role: "code_start", item_id: "5.1", agent_id: 1 });
    items = applySystemEvent(items, {
      role: "code_delta",
      item_id: "5.1",
      agent_id: 1,
      content: "pri",
    });
    items = applySystemEvent(items, {
      role: "code_delta",
      item_id: "5.1",
      agent_id: 1,
      content: "nt(1)",
    });
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: "agent_code", payload: "print(1)" });
  });

  it("out-of-order new item_ids are insert-sorted, not tail-appended (Task #1051: delta-only path never goes through the snapshot re-sort)", () => {
    let items: BackendTimelineItem[] = [];
    // "3.1" arrives first (e.g. reconnect-window resend), then the earlier
    // "2.5" — a tail append would leave 2.5 AFTER 3.1 until the next
    // snapshot merge.
    items = applySystemEvent(items, {
      role: "chat_delta",
      item_id: "3.1",
      agent_id: 1,
      content: "later",
    });
    items = applySystemEvent(items, {
      role: "chat_delta",
      item_id: "2.5",
      agent_id: 1,
      content: "earlier",
    });
    expect(items.map((i) => i.item_id)).toEqual(["2.5", "3.1"]);
  });

  it("code_delta with a new item_id creates an entry (fallback for missed start, partial)", () => {
    const ev: SystemEvent = {
      role: "code_delta",
      item_id: "5.1",
      agent_id: 1,
      content: "x",
    };
    const result = applySystemEvent([], ev);
    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({
      item_id: "5.1",
      kind: "agent_code",
      payload: "x",
      partial: true,
    });
  });

  it("reasoning_start + reasoning_delta concatenate into agent_reasoning", () => {
    let items: BackendTimelineItem[] = [];
    items = applySystemEvent(items, {
      role: "reasoning_start",
      item_id: "5.0",
      agent_id: 1,
    });
    items = applySystemEvent(items, {
      role: "reasoning_delta",
      item_id: "5.0",
      agent_id: 1,
      content: "Let ",
    });
    items = applySystemEvent(items, {
      role: "reasoning_delta",
      item_id: "5.0",
      agent_id: 1,
      content: "me think",
    });
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: "agent_reasoning", payload: "Let me think" });
  });

  it("exec_start creates a code_output placeholder with execStartedAt", () => {
    const ev: SystemEvent = { role: "exec_start", item_id: "6.0", agent_id: 1 };
    const result = applySystemEvent([], ev);
    expect(result[0]).toMatchObject({ kind: "code_output", item_id: "6.0", payload: "" });
    expect(result[0].execStartedAt).toBeGreaterThan(0);
  });

  it("exec_output carries a stable item_id, upsert (new id append, same id replace)", () => {
    let items: BackendTimelineItem[] = [
      item({ kind: "agent_code", item_id: "5.1", payload: "print('x')" }),
    ];
    items = applySystemEvent(items, {
      role: "exec_output",
      item_id: "6.0",
      agent_id: 1,
      content: "x\n",
    });
    expect(items).toHaveLength(2);
    expect(items[1]).toMatchObject({
      item_id: "6.0",
      kind: "code_output",
      payload: "x\n",
    });
  });

  it("compact_done is a no-op — no visible marker created", () => {
    const ev: SystemEvent = { role: "compact_done", agent_id: 1 };
    const result = applySystemEvent([], ev);
    expect(result).toEqual([]);
  });

  it("cancelled is a reducer no-op (the store drops the in-flight bubble using msg_count)", () => {
    // The reducer can't tell a committed bubble from a still-streaming one
    // (normal streamed items have `partial` unset). The store does the drop
    // in processSseEvent with msg_count; here the reducer just passes through.
    const items = [item({ item_id: "1.0", kind: "agent_chat", payload: "x" })];
    expect(applySystemEvent(items, { role: "cancelled", agent_id: 1 })).toBe(items);
    expect(applySystemEvent([], { role: "cancelled", agent_id: 1 })).toHaveLength(0);
  });

  it("inbound_arrived no longer produces an optimistic placeholder — no-op; turnActive managed by processSseEvent", () => {
    const before: BackendTimelineItem[] = [item({ kind: "agent_chat", payload: "hi" })];
    const after = applySystemEvent(before, {
      role: "inbound_arrived",
      agent_id: 1,
      inbound_id: 42,
      kind: "chat",
      source: "user",
      content: "hello from web",
    });
    // optimistic update removed — inbound_arrived inserts no item
    expect(after).toHaveLength(1);
    expect(after[0]).toBe(before[0]);
  });



  it("inbound_committed no-op (hook handles reload; reducer no-op)", () => {
    const before: BackendTimelineItem[] = [item({ kind: "agent_chat", payload: "hi" })];
    const after = applySystemEvent(before, {
      role: "inbound_committed",
      agent_id: 1,
      inbound_id: 42,
    });
    expect(after).toBe(before);
  });

  it("llm_done no-op (hook handles reload; reducer no-op)", () => {
    const before: BackendTimelineItem[] = [item({ kind: "agent_chat", payload: "hi" })];
    const after = applySystemEvent(before, { role: "llm_done", agent_id: 1 });
    expect(after).toBe(before);
  });

  it("cluster update start is a no-op in the per-agent timeline", () => {
    const before: BackendTimelineItem[] = [item({ kind: "agent_chat", payload: "hi" })];
    const after = applySystemEvent(before, {
      role: "cluster_update_started",
      agent_id: 0,
      kind: "rollout",
      origin: "user",
    });
    expect(after).toBe(before);
  });

  it("full flow: code_start → delta × 2 → exec_start → exec_output (exec_start creates placeholder)", () => {
    let items: BackendTimelineItem[] = [];
    items = applySystemEvent(items, { role: "code_start", item_id: "5.1", agent_id: 1 });
    items = applySystemEvent(items, {
      role: "code_delta",
      item_id: "5.1",
      agent_id: 1,
      content: "a",
    });
    items = applySystemEvent(items, {
      role: "code_delta",
      item_id: "5.1",
      agent_id: 1,
      content: "b",
    });
    items = applySystemEvent(items, { role: "exec_start", item_id: "6.0", agent_id: 1 });
    // exec_start creates a code_output placeholder now
    expect(kinds(items)).toEqual(["agent_code", "code_output"]);
    expect(items[1]).toMatchObject({ kind: "code_output", item_id: "6.0", payload: "" });
    items = applySystemEvent(items, {
      role: "exec_output",
      item_id: "6.0",
      agent_id: 1,
      content: "2",
    });
    // exec_output replaces the placeholder with full content
    expect(kinds(items)).toEqual(["agent_code", "code_output"]);
    expect(items[0]).toMatchObject({ payload: "ab" });
    expect(items[1]).toMatchObject({ kind: "code_output", payload: "2" });
    // code_start cleared reasoning clocks (none present, no-op)
    // exec_start cleared code clocks → code item's codeStartedAt gone
    expect(items[0].codeStartedAt).toBeUndefined();
  });

  it("a keepalive bootstraps an empty live output item after a mid-stream join", () => {
    const items = applySystemEvent([], {
      role: "exec_output_chunk",
      item_id: "6.0",
      agent_id: 1,
      content: "keepalive payload must not render",
      keepalive: true,
    });

    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      item_id: "6.0",
      kind: "code_output",
      payload: "",
      partial: true,
    });
    expect(items[0].execStartedAt).toBeTypeOf("number");
  });

  it("a keepalive for an existing output item preserves the array reference", () => {
    const before = [
      item({ item_id: "6.0", kind: "code_output", payload: "existing", partial: true }),
    ];

    const after = applySystemEvent(before, {
      role: "exec_output_chunk",
      item_id: "6.0",
      agent_id: 1,
      content: "",
      keepalive: true,
    });

    expect(after).toBe(before);
  });

  // Regression (symptom: "Writing code for Xs" never appeared during
  // streaming): code_start used to run clearCodeClocks AFTER createStartItem,
  // stripping the fresh item's codeStartedAt in the same event and stamping
  // codeElapsedMs=0 — the live code clock was dead on arrival.
  it("code_start starts a live code clock that survives its own event and deltas", () => {
    let items: BackendTimelineItem[] = [];
    items = applySystemEvent(items, { role: "code_start", item_id: "5.1", agent_id: 1 });
    expect(items[0].codeStartedAt).toBeTypeOf("number");
    expect(items[0].codeElapsedMs).toBeUndefined();
    items = applySystemEvent(items, {
      role: "code_delta",
      item_id: "5.1",
      agent_id: 1,
      content: "x = 1",
    });
    // The delta appends payload without touching the clock anchor.
    expect(items[0].codeStartedAt).toBeTypeOf("number");
    expect(items[0].codeElapsedMs).toBeUndefined();
    // exec_start ends the code-writing phase: anchor cleared, elapsed frozen.
    items = applySystemEvent(items, { role: "exec_start", item_id: "6.0", agent_id: 1 });
    expect(items[0].codeStartedAt).toBeUndefined();
    expect(items[0].codeElapsedMs).toBeTypeOf("number");
  });

  it("a second code_start freezes the previous block's clock but keeps its own live", () => {
    let items: BackendTimelineItem[] = [];
    items = applySystemEvent(items, { role: "code_start", item_id: "5.1", agent_id: 1 });
    items = applySystemEvent(items, { role: "code_start", item_id: "5.2", agent_id: 1 });
    const first = items.find((it) => it.item_id === "5.1");
    const second = items.find((it) => it.item_id === "5.2");
    expect(first?.codeStartedAt).toBeUndefined();
    expect(first?.codeElapsedMs).toBeTypeOf("number");
    expect(second?.codeStartedAt).toBeTypeOf("number");
    expect(second?.codeElapsedMs).toBeUndefined();
  });

  it("consecutive exec_starts with same item_id upsert — at most 1 per id", () => {
    let items: BackendTimelineItem[] = [];
    items = applySystemEvent(items, { role: "exec_start", item_id: "6.0", agent_id: 1 });
    items = applySystemEvent(items, { role: "exec_start", item_id: "6.0", agent_id: 1 });
    items = applySystemEvent(items, { role: "exec_start", item_id: "6.0", agent_id: 1 });
    // same item_id → upsert, not appended; items length stays 1
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: "code_output", item_id: "6.0" });
    // different item_ids → each gets its own item (LLM↔exec multi-round)
    items = applySystemEvent(items, { role: "exec_start", item_id: "7.0", agent_id: 1 });
    expect(items).toHaveLength(2);
  });

  it("exec_start after exec_output creates a fresh placeholder", () => {
    let items: BackendTimelineItem[] = [];
    items = applySystemEvent(items, {
      role: "exec_output",
      item_id: "6.0",
      agent_id: 1,
      content: "old exec output",
    });
    items = applySystemEvent(items, { role: "exec_start", item_id: "7.0", agent_id: 1 });
    // The new exec_start at 7.0 is a new item (different from 6.0)
    expect(items.filter((it) => it.kind === "code_output" && it.item_id === "7.0")).toHaveLength(1);
    expect(items.filter((it) => it.kind === "code_output")).toHaveLength(2);
  });

  it("exec_output does not erase other system_markers (error / cancelled etc.)", () => {
    const errorMarker: BackendTimelineItem = {
      item_id: "_marker.err",
      kind: "system_marker",
      source: null,
      payload: "error:test error",
      created_at: "2026-01-01T00:00:00Z",
      inbound_id: null,
      show_timestamp: true,
    };
    const cancelledMarker: BackendTimelineItem = {
      item_id: "_marker.cancelled",
      kind: "system_marker",
      source: null,
      payload: "cancelled",
      created_at: "2026-01-01T00:00:00Z",
      inbound_id: null,
      show_timestamp: true,
    };
    let items: BackendTimelineItem[] = [errorMarker, cancelledMarker];
    items = applySystemEvent(items, {
      role: "exec_output",
      item_id: "6.0",
      agent_id: 1,
      content: "out",
    });
    expect(items).toHaveLength(3);
    expect(items[0]).toMatchObject({ kind: "system_marker", payload: "error:test error" });
    expect(items[1]).toMatchObject({ kind: "system_marker", payload: "cancelled" });
  });

  it("does not mutate the input list", () => {
    const original: BackendTimelineItem[] = [];
    const ev: SystemEvent = {
      role: "exec_output",
      item_id: "5.0",
      agent_id: 1,
      content: "x",
    };
    applySystemEvent(original, ev);
    expect(original).toHaveLength(0);
  });

  it("chat_start + chat_delta concatenate into agent_chat (by item_id)", () => {
    let items: BackendTimelineItem[] = [];
    items = applySystemEvent(items, { role: "chat_start", item_id: "5.0", agent_id: 1 });
    items = applySystemEvent(items, {
      role: "chat_delta",
      item_id: "5.0",
      agent_id: 1,
      content: "hi ",
    });
    items = applySystemEvent(items, {
      role: "chat_delta",
      item_id: "5.0",
      agent_id: 1,
      content: "user",
    });
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: "agent_chat", payload: "hi user" });
  });

  describe("partial flag", () => {
    it("chat_delta arrives without chat_start → partial=true (user joined mid-stream and missed start)", () => {
      const ev: SystemEvent = {
        role: "chat_delta",
        item_id: "5.0",
        agent_id: 1,
        content: "...",
      };
      const [first] = applySystemEvent([], ev);
      expect(first).toMatchObject({ kind: "agent_chat", payload: "...", partial: true });
    });

    it("item created by chat_start carries no partial flag (full stream)", () => {
      const items = applySystemEvent([], {
        role: "chat_start",
        item_id: "5.0",
        agent_id: 1,
      });
      expect(items[0].partial).toBeUndefined();
    });

    it("partial=true accumulates more delta and stays partial (sticky)", () => {
      let items = applySystemEvent([], {
        role: "code_delta",
        item_id: "5.1",
        agent_id: 1,
        content: "x",
      });
      items = applySystemEvent(items, {
        role: "code_delta",
        item_id: "5.1",
        agent_id: 1,
        content: "y",
      });
      expect(items[0]).toMatchObject({ payload: "xy", partial: true });
    });

    it("partial behavior is symmetric across reasoning / chat / code kinds", () => {
      const r = applySystemEvent([], {
        role: "reasoning_delta",
        item_id: "5.0",
        agent_id: 1,
        content: "a",
      });
      expect(r[0].partial).toBe(true);
      const c = applySystemEvent([], {
        role: "code_delta",
        item_id: "5.1",
        agent_id: 1,
        content: "a",
      });
      expect(c[0].partial).toBe(true);
      const t = applySystemEvent([], {
        role: "chat_delta",
        item_id: "5.2",
        agent_id: 1,
        content: "a",
      });
      expect(t[0].partial).toBe(true);
    });
  });
});


  describe("SSE arrival order — tail append", () => {
    // The single SSE connection (EventSource over TCP) delivers frames in
    // publish order, and item_ids are monotonically increasing (msg_idx =
    // len(state.messages) at stream time), so new items append at the tail.
    // The old insert-sort defended a pre-2026-05-10 multi-connection
    // architecture; the merge re-sorts by item_id anyway, so the visible
    // order is always business order.
    it("in-order arrivals append in arrival order", () => {
      let items: BackendTimelineItem[] = [];
      items = applySystemEvent(items, {
        role: "reasoning_delta",
        item_id: "5.0",
        agent_id: 1,
        content: "thinking",
      });
      items = applySystemEvent(items, {
        role: "chat_delta",
        item_id: "5.1",
        agent_id: 1,
        content: "reply",
      });
      items = applySystemEvent(items, {
        role: "code_delta",
        item_id: "5.2",
        agent_id: 1,
        content: "print(1)",
      });
      expect(items.map((i) => i.item_id)).toEqual(["5.0", "5.1", "5.2"]);
      expect(items.map((i) => i.kind)).toEqual([
        "agent_reasoning",
        "agent_chat",
        "agent_code",
      ]);
    });

    it("existing item (same id) updates in place, position unchanged", () => {
      let items: BackendTimelineItem[] = [];
      items = applySystemEvent(items, {
        role: "reasoning_start",
        item_id: "5.0",
        agent_id: 1,
      });
      items = applySystemEvent(items, {
        role: "chat_start",
        item_id: "5.1",
        agent_id: 1,
      });
      expect(items.map((i) => i.item_id)).toEqual(["5.0", "5.1"]);

      items = applySystemEvent(items, {
        role: "chat_delta",
        item_id: "5.1",
        agent_id: 1,
        content: "more text",
      });
      expect(items.map((i) => i.item_id)).toEqual(["5.0", "5.1"]);
      expect(items[1].payload).toBe("more text");
    });

    it("exec_start appends a code_output placeholder after the streamed message", () => {
      let items: BackendTimelineItem[] = [];
      items = applySystemEvent(items, {
        role: "chat_delta",
        item_id: "5.1",
        agent_id: 1,
        content: "reply",
      });
      items = applySystemEvent(items, { role: "exec_start", item_id: "6.0", agent_id: 1 });
      // "6.0" (exec_msg_idx = len(state.messages) at exec entry) is the
      // largest id, so the placeholder lands at the tail.
      expect(items.map((i) => i.item_id)).toEqual(["5.1", "6.0"]);
      expect(items[1]).toMatchObject({ kind: "code_output", payload: "" });
      expect(items[1].execStartedAt).toBeGreaterThan(0);
    });
  });

describe("mergeSnapshotWithStreaming", () => {
  // New algorithm: snapshotIds set; prev item_ids not in snapshot are
  // kept as clientExtras appended after the snapshot. Stable ids
  // automatically distinguish "already committed" vs "not yet
  // committed", no longer relying on the old trail walk + timestamp
  // heuristic.
  //
  // 2026-05-09 enhancement: clientExtras are insert-sorted by item_id
  // before being appended — fixes the bug where network out-of-order
  // arrival made streaming items reach the timeline in the wrong order.

  it("empty prev + snapshot → result equals snapshot", () => {
    const snapshot = [
      item({ item_id: "1.0", kind: "inbound_chat", payload: "hi", inbound_id: 1 }),
    ];
    expect(mergeSnapshotWithStreaming([], snapshot, 0)).toEqual(snapshot);
  });

  it("prev items not in the snapshot are kept (scroll-loaded history survives an incremental snapshot)", () => {
    // Incremental design: a snapshot only covers new commits; every prev
    // item outside it (scroll-loaded history, committed items) survives.
    const prev = [
      item({ item_id: "1.0", kind: "inbound_chat", payload: "old msg", inbound_id: 1 }),
      item({ item_id: "2.0", kind: "agent_chat", payload: "old reply" }),
      item({ item_id: "5.0", kind: "inbound_chat", payload: "recent", inbound_id: 5 }),
    ];
    const snapshot = [
      item({ item_id: "5.0", kind: "inbound_chat", payload: "recent", inbound_id: 5 }),
      item({ item_id: "6.0", kind: "agent_chat", payload: "newest" }),
    ];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 7);
    expect(result.map((i) => i.item_id)).toEqual(["1.0", "2.0", "5.0", "6.0"]);
  });

  it("a committed item missing from the snapshot is kept (no stale-drop — snapshots are incremental)", () => {
    // The old window semantics dropped committed items inside the window
    // that were absent from the snapshot (stale-drop). Incremental
    // snapshots carry only new commits, so absence proves nothing — keep.
    const prev = [
      item({ item_id: "1.0", kind: "inbound_chat", payload: "old", inbound_id: 1 }),
      item({ item_id: "5.1", kind: "agent_chat", payload: "committed earlier" }),
    ];
    const snapshot = [
      item({ item_id: "5.0", kind: "inbound_chat", payload: "recent", inbound_id: 5 }),
    ];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 7);
    // Sorted by item_id: (5,0) precedes (5,1).
    expect(result.map((i) => i.item_id)).toEqual(["1.0", "5.0", "5.1"]);
  });

  it("prev and snapshot share an item_id → snapshot version wins, no duplicate from prev", () => {
    // Main bug regression: after turn 1 completes, prev =
    // [inbound_1, reasoning_1, chat_1] (from the previous reload's
    // snapshot). Sending inbound_2 triggers reload; the old logic's
    // trail walk treated the streaming-kind tail (reasoning_1 /
    // chat_1) as a trail and appended a copy after the snapshot,
    // resulting in duplicates. The new logic dedupes by item_id and
    // keeps only the snapshot version for the same id.
    const prev = [
      item({ item_id: "1.0", kind: "inbound_chat", payload: "hi", inbound_id: 1 }),
      item({ item_id: "2.0", kind: "agent_reasoning", payload: "thought 1" }),
      item({ item_id: "2.1", kind: "agent_chat", payload: "reply 1" }),
    ];
    const snapshot = [
      item({ item_id: "1.0", kind: "inbound_chat", payload: "hi", inbound_id: 1 }),
      item({ item_id: "2.0", kind: "agent_reasoning", payload: "thought 1" }),
      item({ item_id: "2.1", kind: "agent_chat", payload: "reply 1" }),
      item({ item_id: "3.0", kind: "inbound_chat", payload: "second", inbound_id: 2 }),
    ];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    expect(result).toEqual(snapshot);
    expect(result.filter((i) => i.payload === "thought 1")).toHaveLength(1);
    expect(result.filter((i) => i.payload === "reply 1")).toHaveLength(1);
  });

  it("narrow stale rule: a partial not at the future position and not tracked is dropped", () => {
    // A `partial` item is an uncommitted generation. Unless it is the
    // single future position (msg_idx == msg_count) or still tracked in
    // streamingIds (a cancel may drop it by id), it is stale (error-
    // interrupted turn) and must not linger as a ghost bubble.
    const prev = [
      item({ item_id: "1.0", kind: "inbound_chat", payload: "old", inbound_id: 1 }),
      item({
        item_id: "5.0",
        kind: "agent_reasoning",
        payload: "thinking 1/2 ...",
        partial: true,
      }),
    ];
    const snapshot = [
      item({ item_id: "1.0", kind: "inbound_chat", payload: "old envelope", inbound_id: 1 }),
    ];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    expect(result).toHaveLength(1);
    expect(result[0].payload).toBe("old envelope"); // snapshot version wins
    expect(result.find((i) => i.item_id === "5.0")).toBeUndefined();
  });

  it("llm-done reload: after snapshot replaces the streaming id, prev partial vanishes automatically", () => {
    // After commit, snapshot and streaming share the same item_id → snapshot version wins
    const prev = [
      item({
        item_id: "5.0",
        kind: "agent_reasoning",
        payload: "thinking partial...",
        partial: true,
      }),
    ];
    const snapshot = [
      item({ item_id: "5.0", kind: "agent_reasoning", payload: "thinking complete" }),
    ];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    expect(result).toEqual(snapshot);
    expect(result.find((i) => i.partial)).toBeUndefined();
  });

  it("same content → returns the same prev reference (issue 2 flicker optimization)", () => {
    // When snapshot refresh receives entirely identical items,
    // mergeSnapshotWithStreaming should return the prev reference so
    // Zustand skips setState and React skips the full-tree diff.
    const a = item({ item_id: "5.0", kind: "agent_chat", payload: "hi" });
    const b = item({ item_id: "5.1", kind: "agent_code", payload: "x = 1" });
    const prev = [a, b];
    const snapshot = [a, b];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    expect(result).toBe(prev); // same reference, not toEqual
  });

  it("content changed → returns a new array (must flip the reference when commit replaces partial)", () => {
    const prev = [
      item({ item_id: "5.0", kind: "agent_chat", payload: "partial" }),
    ];
    const snapshot = [
      item({ item_id: "5.0", kind: "agent_chat", payload: "final" }),
    ];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    expect(result).not.toBe(prev);
    expect(result[0].payload).toBe("final");
  });

  it("all ephemeral markers not in the snapshot are preserved (error / cancelled carry 'this happened' info)", () => {
    // The old merge dropped old-format exec_start markers; the producer
    // is gone (no code creates system_marker+exec_start anymore), so the
    // generic keep rule applies to every marker.
    const prev = [
      item({
        item_id: "_marker.execstart",
        kind: "system_marker",
        payload: "exec_start",
      }),
      item({
        item_id: "_marker.err",
        kind: "system_marker",
        payload: "error:something blew up",
      }),
      item({
        item_id: "_marker.cancel",
        kind: "system_marker",
        payload: "cancelled",
      }),
    ];
    const snapshot: BackendTimelineItem[] = [];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    expect(result).toHaveLength(3);
  });
  it("ephemeral marker kept; stale partial dropped (narrow rule, not hard reset)", () => {
    const prev = [
      item({ item_id: "_marker.execstart", kind: "system_marker", payload: "exec_start" }),
      item({ item_id: "9.0", kind: "agent_chat", payload: "streaming…", partial: true }),
    ];
    const snapshot: BackendTimelineItem[] = [];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    // The marker survives; the partial (not at msg_count, not tracked) drops.
    expect(result.map((i) => i.item_id)).toEqual(["_marker.execstart"]);
  });

  it("does not mutate input", () => {
    const prev = [item({ item_id: "5.0", kind: "agent_chat" })];
    const snapshot = [item({ item_id: "1.0", kind: "inbound_chat", inbound_id: 1 })];
    const prevCopy = [...prev];
    const snapshotCopy = [...snapshot];
    mergeSnapshotWithStreaming(prev, snapshot, 0);
    expect(prev).toEqual(prevCopy);
    expect(snapshot).toEqual(snapshotCopy);
  });

  it("narrow stale rule: multiple stale partials not in snapshot are discarded, snapshot left clean", () => {
    // Only in-flight partials survive a merge; stale ones (neither at the
    // future position nor in streamingIds) are dropped. They reappear when
    // the next node enter re-emits chat_start / code_start.
    const prev = [
      item({ item_id: "1.0", kind: "inbound_chat", payload: "envelope:user", inbound_id: 1 }),
      item({ item_id: "5.0", kind: "agent_code", payload: "print('z')", partial: true }),
      item({ item_id: "3.0", kind: "agent_chat", payload: "reply text", partial: true }),
      item({ item_id: "4.0", kind: "agent_reasoning", payload: "thinking", partial: true }),
    ];
    const snapshot = [
      item({ item_id: "1.0", kind: "inbound_chat", payload: "envelope:user", inbound_id: 1 }),
    ];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    expect(result.map((i) => i.item_id)).toEqual(["1.0"]);
  });

  it("merge: _marker.* not in snapshot → kept as ephemeral (error etc.)", () => {
    // After optimistic-update removal, _marker.* only comes from
    // error / cancelled. The generic keep rule preserves them all.
    const prev: BackendTimelineItem[] = [
      item({ item_id: "1.0", kind: "agent_chat", payload: "old" }),
      {
        item_id: "_marker.something",
        kind: "system_marker",
        source: null,
        payload: "error:test failure",
        created_at: "2026-01-01T00:00:00Z",
        inbound_id: null,
        show_timestamp: true,
      },
    ];
    const snapshot: BackendTimelineItem[] = [
      item({ item_id: "1.0", kind: "agent_chat", payload: "old" }),
    ];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    // _marker.something not in snapshot → preserved
    expect(result).toHaveLength(2);
    expect(result[1].payload).toBe("error:test failure");
  });

  it("merge KEEPS pre-compact items — compaction is reconciled by the store's compact_done hard reset, not the merge", () => {
    // The old merge dropped pre-compact items as a side effect of its
    // stale-drop. The incremental merge has no window concept, so it keeps
    // everything — which is exactly why the store hard-resets the thread on
    // compact_done (a GET during the reset window may read a lagging
    // pre-compact checkpoint). This test pins that the merge alone must NOT
    // pretend to reconcile compaction.
    const prev: BackendTimelineItem[] = [
      item({ item_id: "90.0", kind: "inbound_chat", payload: "pre-compact", inbound_id: 1 }),
      item({ item_id: "91.0", kind: "agent_reasoning", payload: "pre-compact thinking" }),
      item({ item_id: "_marker.err", kind: "system_marker", payload: "error:pre-compact error" }),
    ];
    const snapshot: BackendTimelineItem[] = [
      item({
        item_id: "1.0",
        kind: "system_marker",
        payload: "[summary]",
      }),
      item({ item_id: "2.0", kind: "agent_chat", payload: "tail" }),
    ];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    // Everything survives: snapshot items, pre-compact items, marker.
    expect(result.map((i) => i.item_id)).toEqual([
      "1.0", "2.0",
      "90.0", "91.0",
      "_marker.err",
    ]);
  });

});

// ═══════════════════════════════════════════════════════
// isEventForThread
// ═══════════════════════════════════════════════════════

describe("isEventForThread", () => {

  it("same agent_id → true", () => {
    expect(isEventForThread({ agent_id: 42 }, 42)).toBe(true);
  });

  it("different agent_id → false", () => {
    expect(isEventForThread({ agent_id: 42 }, 99)).toBe(false);
  });

  it("activeThreadId=null → false (uninitialized)", () => {
    expect(isEventForThread({ agent_id: 42 }, null)).toBe(false);
  });

  it("agent_id=0 system signal → passes through (independent of activeThreadId)", () => {
    // System-level signals like token reset use agent_id=0, meaning "not bound to any thread"
    expect(isEventForThread({ agent_id: 0 }, 42)).toBe(true);
    expect(isEventForThread({ agent_id: 0 }, null)).toBe(true);
  });
});

// ═══════════════════════════════════════════════════════
// Stryker reinforcement — precisely kills the actionable survived
// mutants the baseline (PR #295) left behind on timeline.ts. Each it
// block has a comment above identifying the mutator + line number +
// expected mutant behavior so future readers can map them at a glance.
// ═══════════════════════════════════════════════════════

describe("parseItemId — boundary reinforcement", () => {
  // L46 LogicalOperator: `||` → `&&`
  // When only one part is finite, current → null; mutant `&&` →
  // returns [num, NaN].
  it("one part non-finite (e.g. '5.x') → null (pins || cannot become &&)", () => {
    expect(parseItemId("5.x")).toBeNull();
    expect(parseItemId("x.5")).toBeNull();
  });
});

describe("sortByItemId — comparator reinforcement", () => {
  // L59:9 ConditionalExpression: `pa===null && pb===null` → `false`
  // When both are null, the mutant returns 1 (not 0), changing the
  // relative order of two ephemeral markers.
  it("two ephemeral markers both parseItemId=null → keep original order (comparator returns 0)", () => {
    const m1 = item({ item_id: "_marker.first", kind: "system_marker", payload: "a" });
    const m2 = item({ item_id: "_marker.second", kind: "system_marker", payload: "b" });
    const sorted = sortByItemId([m1, m2]);
    expect(sorted[0]).toBe(m1);
    expect(sorted[1]).toBe(m2);
    // also stable in the reverse direction
    const sorted2 = sortByItemId([m2, m1]);
    expect(sorted2[0]).toBe(m2);
    expect(sorted2[1]).toBe(m1);
  });

  // L59:24 Conditional `pb===null` → `true` / EqualityOperator `===` → `!==`
  // pa=null, pb≠null case: mutant incorrectly returns 0 (current
  // returns 1, pushing ephemeral to the end).
  it("one stable + one ephemeral mixed → ephemeral must be after stable (pins the pb===null branch)", () => {
    const m = item({ item_id: "_marker.x", kind: "system_marker", payload: "marker" });
    const s = item({ item_id: "5.0", kind: "agent_chat", payload: "stable" });
    // even when ephemeral is placed first, sort always pushes it to the end
    const sorted = sortByItemId([m, s]);
    expect(sorted.map((i) => i.item_id)).toEqual(["5.0", "_marker.x"]);
    // also symmetric in reverse
    const sorted2 = sortByItemId([s, m]);
    expect(sorted2.map((i) => i.item_id)).toEqual(["5.0", "_marker.x"]);
  });

  // L63 Conditional `pa[0] !== pb[0]` → `true`
  // Same msg_idx, different block_idx: current compares block_idx;
  // mutant always returns pa[0]-pb[0]=0 → no sort, original order kept.
  it("same msg_idx, different block_idx → sorted by block_idx (pins that msg_idx-equal branch falls to block compare)", () => {
    const a = item({ item_id: "5.2", kind: "agent_code", payload: "block 2" });
    const b = item({ item_id: "5.0", kind: "agent_chat", payload: "block 0" });
    const c = item({ item_id: "5.1", kind: "agent_reasoning", payload: "block 1" });
    const sorted = sortByItemId([a, b, c]);
    expect(sorted.map((i) => i.item_id)).toEqual(["5.0", "5.1", "5.2"]);
  });

  // L64 ArithmeticOperator: `pa[1] - pb[1]` → `pa[1] + pb[1]`
  // Already sorted [5.0, 5.2]: current 0-2=-2 no swap; mutant 0+2=2
  // swaps → [5.2, 5.0].
  it("input already ascending by block_idx → no swap (pins - cannot become +)", () => {
    const x = item({ item_id: "5.0", payload: "first" });
    const y = item({ item_id: "5.2", payload: "second" });
    const sorted = sortByItemId([x, y]);
    expect(sorted.map((i) => i.item_id)).toEqual(["5.0", "5.2"]);
  });
});

describe("createStartItem dedupe-guard reinforcement", () => {
  // L113 (×3): if (items.some(...)) return items
  // mutants remove the guard (`if (false)` / callback always false /
  // callback returns undefined) → same-id second creation duplicates.
  it("two chat_starts under same item_id → items length still 1, payload unchanged (pins dup guard)", () => {
    let items: BackendTimelineItem[] = [];
    items = applySystemEvent(items, { role: "chat_start", item_id: "5.0", agent_id: 1 });
    // delta a chunk first
    items = applySystemEvent(items, {
      role: "chat_delta",
      item_id: "5.0",
      agent_id: 1,
      content: "halfway written",
    });
    expect(items).toHaveLength(1);
    expect(items[0].payload).toBe("halfway written");
    // Second chat_start (re-emitted after reload) — should no-op; no duplicate insert, no payload wipe
    items = applySystemEvent(items, { role: "chat_start", item_id: "5.0", agent_id: 1 });
    expect(items).toHaveLength(1);
    expect(items[0].payload).toBe("halfway written");
  });

  it("same item_id × twice for code_start + reasoning_start equally no-op (covers all three kinds)", () => {
    let items: BackendTimelineItem[] = [];
    items = applySystemEvent(items, { role: "code_start", item_id: "5.1", agent_id: 1 });
    items = applySystemEvent(items, { role: "code_start", item_id: "5.1", agent_id: 1 });
    expect(items).toHaveLength(1);

    items = applySystemEvent(items, { role: "reasoning_start", item_id: "5.0", agent_id: 1 });
    items = applySystemEvent(items, { role: "reasoning_start", item_id: "5.0", agent_id: 1 });
    expect(items).toHaveLength(2);
  });
});

describe("upsertItemById reinforcement", () => {
  // L158 / L159: findIndex callback + idx >= 0
  // mutant: callback returns false/undefined → idx=-1 → falls to
  //         insertSorted (duplicate).
  // mutant: idx > 0 → when the existing item is at idx=0, no
  //         replacement, falls to insertSorted.
  it("exec_output same id replaces items[0] (idx=0 boundary) → length still 1, payload is new value", () => {
    // First place an old code_output with id=6.0 at position 0 (idx=0)
    let items: BackendTimelineItem[] = [
      item({ item_id: "6.0", kind: "code_output", payload: "old output" }),
    ];
    items = applySystemEvent(items, {
      role: "exec_output",
      item_id: "6.0",
      agent_id: 1,
      content: "new envelope",
    });
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ item_id: "6.0", payload: "new envelope" });
  });

  it("exec_output same id replaces an item in the middle (idx>0) → position unchanged, only payload updated", () => {
    let items: BackendTimelineItem[] = [
      item({ item_id: "5.0", kind: "agent_chat", payload: "chat" }),
      item({ item_id: "6.0", kind: "code_output", payload: "old output" }),
      item({ item_id: "7.0", kind: "agent_chat", payload: "more chat" }),
    ];
    items = applySystemEvent(items, {
      role: "exec_output",
      item_id: "6.0",
      agent_id: 1,
      content: "fresh envelope",
    });
    expect(items).toHaveLength(3);
    expect(items[1]).toMatchObject({ item_id: "6.0", payload: "fresh envelope" });
  });
});

describe("insertSorted unparseable-id reinforcement", () => {
  // L140 ConditionalExpression: `if (!newParsed) return [...items, newItem]` → `if (false) ...`
  // mutant skips early return; findIndex indexing null newParsed →
  // TypeError. Trigger: new item id unparseable (not _marker.* but
  // split fails) + items non-empty. The SDK never produces such ids;
  // use chat_delta with an illegal id to simulate.
  it("chat_delta with unparseable id (not _marker, split fails) → silent append, no throw (pins !newParsed early return)", () => {
    // items non-empty so the mutant actually enters findIndex and accesses newParsed[0]
    const existing = item({ item_id: "5.0", kind: "agent_chat", payload: "first" });
    const result = applySystemEvent([existing], {
      role: "chat_delta",
      item_id: "not-an-id",
      agent_id: 1,
      content: "hi",
    });
    expect(result).toHaveLength(2);
    // unparseable ids fall through to fallback path and append at the end
    expect(result[1].item_id).toBe("not-an-id");
    expect(result[1].payload).toBe("hi");
  });
});

describe("isExecStartMarker / removeExecStartMarkers reinforcement", () => {
  // L176/L177: isEphemeralMarker(it) && it.kind === "system_marker" && it.payload === "exec_start"
  // mutant 1: kind check → true → any ephemeral with
  //           payload="exec_start" is treated as exec_start.
  // mutant 2: && → || → any ephemeral is dropped.
  // mutant 3: payload check → true → any ephemeral system_marker is dropped.
  // Use mergeSnapshotWithStreaming to exercise the isExecStartMarker filter path.
  it("every ephemeral marker survives the merge (error/cancelled/old exec_start all visible)", () => {
    // The merge's exec_start special-case was removed with the incremental
    // redesign (no producer creates exec_start markers anymore); the
    // generic keep rule preserves every marker.
    const err = item({
      item_id: "_marker.err",
      kind: "system_marker",
      payload: "error:boom",
    });
    const cancelled = item({
      item_id: "_marker.cancel",
      kind: "system_marker",
      payload: "cancelled",
    });
    const exec_start = item({
      item_id: "_marker.exec",
      kind: "system_marker",
      payload: "exec_start",
    });
    const result = mergeSnapshotWithStreaming(
      [err, cancelled, exec_start],
      [],
      0,
    );
    expect(result).toHaveLength(3);
    const payloads = result.map((it) => it.payload);
    expect(payloads).toContain("error:boom");
    expect(payloads).toContain("cancelled");
    expect(payloads).toContain("exec_start");
  });

  // The merge no longer branches on exec_start markers; this pins that a
  // non-system_marker ephemeral with payload="exec_start" survives (it is
  // not partial → generic keep rule).
  it("ephemeral marker kind≠system_marker but payload='exec_start' → survives", () => {
    const fake = item({
      item_id: "_marker.weird",
      kind: "agent_chat", // intentionally non-system_marker
      payload: "exec_start",
    });
    const result = mergeSnapshotWithStreaming([fake], [], 0);
    expect(result).toHaveLength(1);
    expect(result[0]).toBe(fake);
  });

  // L176 LogicalOperator: && → ||
  // Pin: ephemeral with non-exec_start payload must not be dropped.
  // (Existing tests already cover this, but add one going through
  // exec_output → removeExecStartMarkers.)
  it("exec_output triggers removeExecStartMarkers; non-exec_start ephemeral markers must remain", () => {
    const errorMarker: BackendTimelineItem = {
      item_id: "_marker.err",
      kind: "system_marker",
      source: null,
      payload: "error:test error",
      created_at: "2026-01-01T00:00:00Z",
      inbound_id: null,
      show_timestamp: true,
    };
    const result = applySystemEvent([errorMarker], {
      role: "exec_output",
      item_id: "6.0",
      agent_id: 1,
      content: "out",
    });
    // exec_output must not drop error marker
    expect(result).toHaveLength(2);
    expect(result[0]).toMatchObject({ kind: "system_marker", payload: "error:test error" });
  });
});

describe("applySystemEvent — case label + template literal reinforcement", () => {
  // L220 StringLiteral: case "exec_output_chunk" → case ""
  // mutant makes exec_output_chunk fall through to the next case
  // (exec_output) → replaces instead of appends.
  it("exec_output_chunk same id multiple times → payload accumulates by append, not replace (pins case label)", () => {
    let items: BackendTimelineItem[] = [];
    items = applySystemEvent(items, {
      role: "exec_output_chunk",
      item_id: "6.0",
      agent_id: 1,
      content: "first",
    });
    items = applySystemEvent(items, {
      role: "exec_output_chunk",
      item_id: "6.0",
      agent_id: 1,
      content: "+second",
    });
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      item_id: "6.0",
      kind: "code_output",
      payload: "first+second",
      partial: true, // appendDeltaById new-item path carries partial=true
    });
  });

  // L237 StringLiteral: case "compact_request" → case ""
  // compact_request is now a no-op (no visible marker created).
  // The case label is pinned by the switch exhaustiveness check.
  it("compact_request is a no-op — no visible marker created", () => {
    const result = applySystemEvent([], {
      role: "compact_request",
      agent_id: 1,
      content: "user-typed compact reason",
    });
    expect(result).toEqual([]);
  });

  // L261 StringLiteral: case "error" → case ""
  // mutant makes error fall through to cancelled → payload "cancelled"
  // without the error content.
  it("error payload must be 'error:<content>' (pins case label + L277 marker template)", () => {
    const result = applySystemEvent([], {
      role: "error",
      agent_id: 1,
      content: "boom details",
    });
    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({
      kind: "system_marker",
      payload: "error:boom details",
    });
    // Pin L277 marker template literal
    expect(result[0].item_id.startsWith("_marker.")).toBe(true);
    expect(result[0].item_id).not.toBe("");
  });
});

// ═══════════════════════════════════════════════════════
// Reference equality — pins mergeSnapshotWithStreaming's "return prev
// on identical content" path. (Existing tests already cover toBe(prev);
// add boundary cases to keep the reference optimization from regressing.)
// ═══════════════════════════════════════════════════════
describe("mergeSnapshotWithStreaming reference identity reinforcement", () => {
  it("snapshot equal to prev → returns same prev reference (Zustand setState short-circuit)", () => {
    const a = item({ item_id: "5.0", kind: "agent_chat", payload: "hi" });
    const b = item({ item_id: "5.1", kind: "agent_chat", payload: "more" });
    const prev = [a, b];
    const snapshot = [a, b];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    expect(result).toBe(prev);
  });

  it("snapshot has one extra item → must return a new array (reference must flip)", () => {
    const a = item({ item_id: "5.0", kind: "agent_chat", payload: "hi" });
    const prev = [a];
    const snapshot = [a, item({ item_id: "5.1", kind: "agent_chat", payload: "added" })];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    expect(result).not.toBe(prev);
    expect(result).toHaveLength(2);
  });

  it("same length but one item replaced → returns a new array (pins same-flag actually compares elements)", () => {
    const a = item({ item_id: "5.0", kind: "agent_chat", payload: "old" });
    const a2 = item({ item_id: "5.0", kind: "agent_chat", payload: "new" });
    const prev = [a];
    const snapshot = [a2];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    expect(result).not.toBe(prev);
    expect(result[0].payload).toBe("new");
  });
});

// ═══════════════════════════════════════════════════════
// applySystemEvent no-op case reinforcement — pins the case-label
// string literal and the case condition itself, so a StringLiteral /
// ConditionalExpression mutant that changes a case to `""` or removes
// it entirely (causing the switch to fall through to undefined) is
// caught (the original would return items).
//
// These event roles (token_usage / label_updated / timeline_snapshot)
// are no-ops in the timeline reducer (each consumed by its own hook),
// but the case label must exist — dropping it makes the switch return
// undefined and downstream setState(undefined) blows up immediately.
// ═══════════════════════════════════════════════════════
describe("applySystemEvent no-op case label reinforcement", () => {
  // L293:10 StringLiteral: `case "token_usage"` → `case ""`
  // Mutant fails to match token_usage → switch falls through → undefined.
  it("token_usage no-op (hook handles it; reducer no-op) → returns same prev reference", () => {
    const before: BackendTimelineItem[] = [item({ kind: "agent_chat", payload: "hi" })];
    const after = applySystemEvent(before, {
      role: "token_usage",
      agent_id: 1,
      input_tokens: 100,
      output_tokens: 50,
    });
    expect(after).toBe(before);
  });

  // L305:5 Conditional: removes the `case "label_updated":` label
  // L305:10 StringLiteral: `case "label_updated"` → `case ""`
  // Mutants make label_updated fall through the switch → undefined.
  it("label_updated no-op (consumed by the useAgentLabelUpdates hook) → returns same prev reference", () => {
    const before: BackendTimelineItem[] = [item({ kind: "agent_chat", payload: "hi" })];
    const after = applySystemEvent(before, {
      role: "label_updated",
      agent_id: 1,
      label: "new-label",
    });
    expect(after).toBe(before);
  });

  // L309:5 Conditional: removes the `case "timeline_snapshot":` label
  // L309:10 StringLiteral: `case "timeline_snapshot"` → `case ""`
  // Mutants make timeline_snapshot fall through the switch → undefined.
  // (timeline_snapshot is actually intercepted by store.processSseEvent
  // and never reaches the reducer, but the reducer case must exist
  // as a type-exhaustive guard.)
  it("timeline_snapshot no-op (store.processSseEvent intercepts; reducer fallback returns items) → returns same prev reference", () => {
    const before: BackendTimelineItem[] = [item({ kind: "agent_chat", payload: "hi" })];
    const after = applySystemEvent(before, {
      role: "timeline_snapshot",
      agent_id: 1,
      msg_count: 0,
      items: [],
    });
    expect(after).toBe(before);
  });
});

describe("mergeSnapshotWithStreaming — system-prompt keep-verbatim (generic keep rule)", () => {
  // Incremental snapshots never carry the ~128KB system-prompt item (0.0) —
  // message 0 is below the publish cursor. The generic keep rule (prev items
  // not in the snapshot survive) must preserve the existing object, not
  // re-allocate the string per snapshot.

  it("keeps prev's system-prompt item verbatim when the snapshot omits it", () => {
    const sys = item({ item_id: "0.0", kind: "system_prompt", payload: "BIG PROMPT" });
    const prev = [sys, item({ item_id: "1.0", kind: "agent_chat", payload: "old reply" })];
    const snapshot = [item({ item_id: "1.0", kind: "agent_chat", payload: "old reply" })];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 2);
    expect(result.map((i) => i.item_id)).toEqual(["0.0", "1.0"]);
    expect(result[0]).toBe(sys); // same object reference — no re-allocation
  });

  it("keeps prev's system-prompt item even for an empty snapshot", () => {
    // Defensive: an empty snapshot (nothing new in the window) must not drop
    // prev items — the merge keeps everything not replaced.
    const sys = item({ item_id: "0.0", kind: "system_prompt", payload: "BIG PROMPT" });
    const result = mergeSnapshotWithStreaming([sys], [], 1);
    expect(result.map((i) => i.item_id)).toEqual(["0.0"]);
    expect(result[0]).toBe(sys);
  });

});
