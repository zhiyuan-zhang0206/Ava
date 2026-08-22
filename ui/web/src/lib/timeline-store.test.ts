// timeline-store.ts direct unit tests — the SSE-driven streaming timeline
// store, split out of store.ts (which now holds only UI + cluster state).
//
// Complements the flaky/use-timeline.test.ts integration tests:
//   - use-timeline: renderHook actually runs React state, tests hook wiring
//   - here:          direct act on `useTimelineStore.getState()` actions,
//                    locking down pure-state-machine behavior — SSE role × flag
//                    transitions, connection-event writes, switchThread
//                    defaults + LRU, per-thread background folding, etc.

import { act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useTimelineStore } from "./timeline-store";
import type { AgentRow, BackendTimelineItem, SystemEvent } from "./types";

// -- helpers ───────────────────────────────────────────────────────────────

let _id_counter = 0;
function item(overrides: Partial<BackendTimelineItem>): BackendTimelineItem {
  return {
    kind: "agent_chat",
    source: null,
    payload: "",
    created_at: "2026-01-01T00:00:00Z",
    item_id: `s.${++_id_counter}`,
    inbound_id: null,
    show_timestamp: true,
    ...overrides,
  };
}

/** Reset the timeline store to a fresh state — avoids cross-test contamination.
 *  Direct setState (not switchThread): switchThread now parks/unparks per-thread
 *  state, so re-selecting the same id would unpark the prior test's polluted
 *  state instead of clearing it, and its parked buckets must be dropped too. */
function resetStore(): void {
  useTimelineStore.setState({
    activeThreadId: 42, // 42 so isEventForThread does not block agent_id=42 SSE events
    items: [],
    streamingIds: new Set(),
    streamingCode: false,
    turnActive: false,
    connectionState: "open",
    tokenUsage: 0,
    reasoningTokens: 0,
    maxContextTokens: 0,
    hasMoreOlder: false,
    loadingOlder: false,
    olderFetchCount: 0,
    scrollToBottomRequest: 0,
    threads: new Map(),
  });
}

beforeEach(() => {
  resetStore();
});

afterEach(() => {
  vi.useRealTimers();
});

// -- 1. SSE role dispatch (table-driven) ───────────────────────────────────
//
// Exhaustive table for the processSseEvent 5-way OR chain — each role × each flag.
// This kills the ~30-mutant cluster (turnStart / turnEnd / codeStart / codeEnd /
// token_usage ConditionalExpression + StringLiteral + EqualityOperator).

// Each role expresses its effect on each flag as "set true" / "set false" / "keep" (None).
// This semantics lets us precisely test "flag is actually flipped" regardless of prior state.
type FlagOp = "set-true" | "set-false" | "keep";

interface RoleCase {
  readonly name: string;
  readonly event: SystemEvent;
  readonly turnActive: FlagOp;
  readonly streamingCode: FlagOp;
  /** 'set' = write event.input_tokens; 'keep' = leave unchanged */
  readonly tokenUsage: "set" | "keep";
}

const ROLE_CASES: readonly RoleCase[] = [
  // turnStart chain: inbound_arrived / chat_start / code_start / reasoning_start / exec_start
  {
    name: "inbound_arrived → turnActive set true; streaming keep",
    event: {
      role: "inbound_arrived",
      agent_id: 42,
      inbound_id: 1,
      kind: "chat",
      source: "user",
      content: "hi",
    },
    turnActive: "set-true",
    streamingCode: "keep",
    tokenUsage: "keep",
  },
  {
    name: "chat_start → turnActive set true; streaming keep",
    event: { role: "chat_start", agent_id: 42, item_id: "1.0" },
    turnActive: "set-true",
    streamingCode: "keep",
    tokenUsage: "keep",
  },
  {
    name: "code_start → turnActive set true + streamingCode set true",
    event: { role: "code_start", agent_id: 42, item_id: "1.0" },
    turnActive: "set-true",
    streamingCode: "set-true",
    tokenUsage: "keep",
  },
  {
    name: "reasoning_start → turnActive set true; streaming keep",
    event: { role: "reasoning_start", agent_id: 42, item_id: "1.0" },
    turnActive: "set-true",
    streamingCode: "keep",
    tokenUsage: "keep",
  },
  {
    name: "exec_start → turnActive set true + streaming set false (codeEnd)",
    event: { role: "exec_start", agent_id: 42, item_id: "5.0" },
    turnActive: "set-true",
    streamingCode: "set-false",
    tokenUsage: "keep",
  },
  // turnEnd chain: llm_done / cancelled / error
  {
    name: "llm_done → turnActive set false; streaming keep",
    event: { role: "llm_done", agent_id: 42 },
    turnActive: "set-false",
    streamingCode: "keep",
    tokenUsage: "keep",
  },
  {
    name: "cancelled → turnActive set false + streaming set false (in codeEnd chain)",
    event: { role: "cancelled", agent_id: 42 },
    turnActive: "set-false",
    streamingCode: "set-false",
    tokenUsage: "keep",
  },
  {
    name: "error → turnActive set false + streaming set false (in codeEnd chain)",
    event: { role: "error", agent_id: 42, content: "boom" },
    turnActive: "set-false",
    streamingCode: "set-false",
    tokenUsage: "keep",
  },
  // codeEnd only: exec_output / compact_done
  {
    name: "exec_output → streaming set false; turnActive keep",
    event: { role: "exec_output", agent_id: 42, item_id: "2.0", content: "out" },
    turnActive: "keep",
    streamingCode: "set-false",
    tokenUsage: "keep",
  },
  {
    name: "compact_done → streaming set false; turnActive keep",
    event: { role: "compact_done", agent_id: 42 },
    turnActive: "keep",
    streamingCode: "set-false",
    tokenUsage: "keep",
  },
  // token_usage: writes tokenUsage; others keep
  {
    name: "token_usage → writes tokenUsage; turnActive/streaming keep",
    event: { role: "token_usage", agent_id: 42, input_tokens: 1234, output_tokens: 56 },
    turnActive: "keep",
    streamingCode: "keep",
    tokenUsage: "set",
  },
];

/**
 * Run one dispatch + assertion for a single flag.
 * Initial state set to the inverse of the "expected result" so mutations breaking the OR chain
 * (cond → false / role string changed to "" etc.) → the ternary takes the keep branch and preserves the original value
 * → contradicts the expectation → the test fails. This is how table-driven kills every role × flag mutation.
 *
 */
function runFlagCase(
  event: SystemEvent,
  flag: "turnActive" | "streamingCode",
  op: FlagOp,
): void {
  // Initial: inverse of the set-true expected value
  const initial = op === "set-true" ? false : true;
  const expected = op === "set-true" ? true : op === "set-false" ? false : initial;

  act(() => {
    useTimelineStore.setState({ [flag]: initial });
    useTimelineStore.getState().processSseEvent(event);
  });
  expect(useTimelineStore.getState()[flag]).toBe(expected);
}

describe("processSseEvent role dispatch (table-driven)", () => {
  // Each case runs two independent sub-tests (one for turnActive, one for streamingCode);
  // each starts in the inverse state of expected so keep mutations and cond mutations are both distinguishable.
  it.each(ROLE_CASES)("$name [turnActive]", ({ event, turnActive }) => {
    runFlagCase(event, "turnActive", turnActive);
  });

  it.each(ROLE_CASES)("$name [streamingCode]", ({ event, streamingCode }) => {
    runFlagCase(event, "streamingCode", streamingCode);
  });

  it.each(ROLE_CASES)("$name [tokenUsage]", ({ event, tokenUsage }) => {
    // tokenUsage: starts at 999, set op → event.input_tokens (1234), keep → 999
    act(() => {
      useTimelineStore.setState({ tokenUsage: 999 });
      useTimelineStore.getState().processSseEvent(event);
    });
    if (tokenUsage === "set") {
      // Only token_usage events take this path; expect input_tokens to be written
      expect(useTimelineStore.getState().tokenUsage).toBe(1234);
    } else {
      expect(useTimelineStore.getState().tokenUsage).toBe(999);
    }
  });

  // Separate test of the turnStart-then-turnEnd chain — ensures that the turnActive set
  // earlier is flipped back by the subsequent turnEnd (verifies "ternary order: turnStart? : turnEnd? : keep")
  it("turnStart sets true, subsequent turnEnd flips to false", () => {
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "chat_start",
        agent_id: 42,
        item_id: "1.0",
      });
    });
    expect(useTimelineStore.getState().turnActive).toBe(true);

    act(() => {
      useTimelineStore.getState().processSseEvent({ role: "llm_done", agent_id: 42 });
    });
    expect(useTimelineStore.getState().turnActive).toBe(false);
  });

  // Roles not in the turnStart/turnEnd map must not modify turnActive (keep s.turnActive)
  it("unrelated role (chat_delta) does not modify turnActive", () => {
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "chat_start",
        agent_id: 42,
        item_id: "1.0",
      });
    });
    expect(useTimelineStore.getState().turnActive).toBe(true);

    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "chat_delta",
        agent_id: 42,
        item_id: "1.0",
        content: "hi",
      });
    });
    // chat_delta is not in the turnEnd map → turnActive stays true
    expect(useTimelineStore.getState().turnActive).toBe(true);
  });

  // After codeStart sets true, roles not in the codeEnd map must not clear streamingCode
  it("unrelated role (code_delta) does not modify streamingCode", () => {
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "code_start",
        agent_id: 42,
        item_id: "1.0",
      });
    });
    expect(useTimelineStore.getState().streamingCode).toBe(true);

    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "code_delta",
        agent_id: 42,
        item_id: "1.0",
        content: "x = 1",
      });
    });
    expect(useTimelineStore.getState().streamingCode).toBe(true);
  });

  // token_usage with reasoning_tokens > 0 sets reasoningTokens
  it("token_usage with reasoning_tokens writes reasoningTokens", () => {
    act(() => {
      useTimelineStore.setState({ reasoningTokens: 0 });
      useTimelineStore.getState().processSseEvent({
        role: "token_usage",
        agent_id: 42,
        input_tokens: 1234,
        output_tokens: 56,
        reasoning_tokens: 300,
      });
    });
    expect(useTimelineStore.getState().reasoningTokens).toBe(300);
  });

  // token_usage without reasoning_tokens defaults to 0
  it("token_usage without reasoning_tokens keeps reasoningTokens at 0", () => {
    act(() => {
      useTimelineStore.setState({ reasoningTokens: 99 });
      useTimelineStore.getState().processSseEvent({
        role: "token_usage",
        agent_id: 42,
        input_tokens: 1234,
        output_tokens: 56,
      });
    });
    expect(useTimelineStore.getState().reasoningTokens).toBe(0);
  });

  // non-token_usage role must not modify tokenUsage (keep s.tokenUsage)
  it("non-token_usage role does not touch tokenUsage", () => {
    act(() => {
      useTimelineStore.setState({ tokenUsage: 999 });
      useTimelineStore.getState().processSseEvent({
        role: "chat_start",
        agent_id: 42,
        item_id: "1.0",
      });
    });
    expect(useTimelineStore.getState().tokenUsage).toBe(999);
  });

  // agent_id guard: cross-thread SSE must not contaminate state
  it("cross-thread SSE blocked by the isEventForThread guard", () => {
    // activeThreadId=42, an agent_id=99 event arrives
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "chat_start",
        agent_id: 99,
        item_id: "1.0",
      });
    });
    expect(useTimelineStore.getState().turnActive).toBe(false); // not set
  });

  // timeline_snapshot uses a dedicated branch (mergeSnapshotWithStreaming); does not touch turnActive/streamingCode/tokenUsage
  it("timeline_snapshot replaces items, does not touch lifecycle flags", () => {
    act(() => {
      useTimelineStore.setState({ tokenUsage: 500, turnActive: true, streamingCode: true });
    });
    const snapshot = [item({ item_id: "1.0", kind: "agent_chat", payload: "hello" })];
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "timeline_snapshot",
        agent_id: 42,
      msg_count: 0,
        items: snapshot,
      });
    });
    const s = useTimelineStore.getState();
    expect(s.items).toHaveLength(1);
    expect(s.items[0].payload).toBe("hello");
    // lifecycle flags untouched (snapshot branch returns early)
    expect(s.turnActive).toBe(true);
    expect(s.streamingCode).toBe(true);
    expect(s.tokenUsage).toBe(500);
  });
});

// -- 2. processConnectionEvent ──────────────────────────────────────────────

describe("processConnectionEvent", () => {
  it("closed → connectionState='closed' + turnActive=false + streamingCode=false", () => {
    act(() => {
      useTimelineStore.setState({ turnActive: true, streamingCode: true });
      useTimelineStore.getState().processConnectionEvent({ type: "closed" });
    });
    const s = useTimelineStore.getState();
    expect(s.connectionState).toBe("closed");
    expect(s.turnActive).toBe(false);
    expect(s.streamingCode).toBe(false);
  });

  it("closed marks partial items as interrupted, non-partial items unchanged", () => {
    const items: BackendTimelineItem[] = [
      item({ item_id: "1.0", kind: "agent_chat", payload: "done", partial: false }),
      item({ item_id: "2.0", kind: "agent_reasoning", payload: "...", partial: true }),
      item({ item_id: "3.0", kind: "code_output", payload: "out" }), // partial undefined
    ];
    act(() => {
      useTimelineStore.setState({ items });
      useTimelineStore.getState().processConnectionEvent({ type: "closed" });
    });
    const after = useTimelineStore.getState().items;
    expect(after[0].interrupted).toBeFalsy(); // non-partial
    expect(after[1].interrupted).toBe(true); // partial → interrupted
    expect(after[2].interrupted).toBeFalsy(); // not marked when partial is not set
    // partial preserved
    expect(after[1].partial).toBe(true);
  });

  it("closed does not re-mark partials that are already interrupted (but still ok to produce a new object)", () => {
    const items: BackendTimelineItem[] = [
      item({ item_id: "1.0", payload: "x", partial: true, interrupted: true }),
    ];
    act(() => {
      useTimelineStore.setState({ items });
      useTimelineStore.getState().processConnectionEvent({ type: "closed" });
    });
    const after = useTimelineStore.getState().items;
    // partial && !interrupted=false → item unchanged (original ref returned)
    expect(after[0]).toBe(items[0]);
  });

  it("closed with no partials → items reference unchanged (some=false short-circuit)", () => {
    const items: BackendTimelineItem[] = [
      item({ item_id: "1.0", payload: "a" }),
      item({ item_id: "2.0", payload: "b" }),
    ];
    act(() => {
      useTimelineStore.setState({ items });
      useTimelineStore.getState().processConnectionEvent({ type: "closed" });
    });
    expect(useTimelineStore.getState().items).toBe(items); // ref equality
  });

  it("open → connectionState='open' + clears interrupted flag", () => {
    const items: BackendTimelineItem[] = [
      item({ item_id: "1.0", payload: "a", partial: true, interrupted: true }),
      item({ item_id: "2.0", payload: "b", partial: false }),
    ];
    act(() => {
      useTimelineStore.setState({ items, connectionState: "closed" });
      useTimelineStore.getState().processConnectionEvent({ type: "open" });
    });
    const after = useTimelineStore.getState().items;
    expect(useTimelineStore.getState().connectionState).toBe("open");
    expect(after[0].interrupted).toBe(false); // flipped
    expect(after[0].partial).toBe(true); // partial still preserved
    expect(after[1]).toBe(items[1]); // non-interrupted item ref unchanged
  });

  it("open with no interrupted items → items ref unchanged (some=false short-circuit)", () => {
    const items: BackendTimelineItem[] = [
      item({ item_id: "1.0", payload: "a", partial: true }),
      item({ item_id: "2.0", payload: "b" }),
    ];
    act(() => {
      useTimelineStore.setState({ items });
      useTimelineStore.getState().processConnectionEvent({ type: "open" });
    });
    expect(useTimelineStore.getState().items).toBe(items);
  });

  it("reconnecting → connectionState='reconnecting', items unchanged, lifecycle flags untouched", () => {
    const items: BackendTimelineItem[] = [
      item({ item_id: "1.0", payload: "x", partial: true }),
    ];
    act(() => {
      useTimelineStore.setState({ items, turnActive: true, streamingCode: true });
      useTimelineStore.getState().processConnectionEvent({ type: "reconnecting" });
    });
    const s = useTimelineStore.getState();
    expect(s.connectionState).toBe("reconnecting");
    expect(s.items).toBe(items); // ref unchanged (does not take closed/open branch)
    expect(s.turnActive).toBe(true); // not reset
    expect(s.streamingCode).toBe(true);
  });

  // Key: reconnecting must not accidentally trigger the open-branch interrupted clear.
  // Mutant kill: `else if (ev.type === "open")` → `else if (true)` (would clear interrupted on reconnecting)
  it("reconnecting keeps interrupted items (does not trigger open-branch clear)", () => {
    const items: BackendTimelineItem[] = [
      item({ item_id: "1.0", payload: "x", partial: true, interrupted: true }),
    ];
    act(() => {
      useTimelineStore.setState({ items });
      useTimelineStore.getState().processConnectionEvent({ type: "reconnecting" });
    });
    // open branch clears interrupted; reconnecting must not enter the open branch
    expect(useTimelineStore.getState().items[0].interrupted).toBe(true);
    expect(useTimelineStore.getState().items).toBe(items); // ref unchanged
  });
});

// -- 3. clearPartialFlags ──────────────────────────────────────────────────

describe("clearPartialFlags", () => {
  it("mixed partial/non-partial set: only partial gets cleared, non-partial item ref unchanged (some !== every)", () => {
    const a = item({ item_id: "1.0", payload: "committed", partial: false });
    const b = item({ item_id: "2.0", payload: "still loading", partial: true });
    const c = item({ item_id: "3.0", payload: "another commit" }); // partial undefined
    act(() => {
      useTimelineStore.setState({ items: [a, b, c] });
      useTimelineStore.getState().clearPartialFlags();
    });
    const after = useTimelineStore.getState().items;
    expect(after).toHaveLength(3);
    expect(after[0]).toBe(a); // ref unchanged (no partial)
    expect(after[1].partial).toBe(false); // flipped
    expect(after[1].payload).toBe("still loading");
    expect(after[2]).toBe(c); // ref unchanged
  });

  it("no partials → items ref unchanged (some short-circuit)", () => {
    const items = [item({ item_id: "1.0", payload: "a" })];
    act(() => {
      useTimelineStore.setState({ items });
      useTimelineStore.getState().clearPartialFlags();
    });
    expect(useTimelineStore.getState().items).toBe(items);
  });

  it("all partial → all flipped to false (verifies some=true enters the path)", () => {
    const items = [
      item({ item_id: "1.0", payload: "a", partial: true }),
      item({ item_id: "2.0", payload: "b", partial: true }),
    ];
    act(() => {
      useTimelineStore.setState({ items });
      useTimelineStore.getState().clearPartialFlags();
    });
    const after = useTimelineStore.getState().items;
    expect(after.every((i) => i.partial === false)).toBe(true);
  });
});

// -- 4. create() initial defaults ──────────────────────────────────────────

describe("create() initial defaults (fresh module)", () => {
  // Verify the initial state set in create() — these mutations
  // are masked by setState/switchThread after beforeEach; the module must be **freshly imported**
  // to observe (vi.resetModules + dynamic import = fresh singleton).
  it("freshly imported timeline store: all initial defaults match contract", async () => {
    vi.resetModules();
    const mod = await import("./timeline-store");
    const s = mod.useTimelineStore.getState();
    expect(s.items).toEqual([]);
    expect(s.turnActive).toBe(false);
    expect(s.streamingCode).toBe(false);
    expect(s.connectionState).toBe("open");
    expect(s.tokenUsage).toBe(0);
    expect(s.reasoningTokens).toBe(0);
    expect(s.maxContextTokens).toBe(0);
    expect(s.activeThreadId).toBeNull();
    expect(s.scrollToBottomRequest).toBe(0);
    expect(s.hasMoreOlder).toBe(false);
    expect(s.loadingOlder).toBe(false);
    expect(s.olderFetchCount).toBe(0);
    expect(s.threads.size).toBe(0);
  });
});

describe("switchThread defaults", () => {

  it("cold switch (cached=null) clears items / flips all lifecycle flags / zeros tokens / sets activeThreadId", () => {
    // pollute state first
    act(() => {
      useTimelineStore.setState({
        items: [item({ payload: "stale" })],
        turnActive: true,
        streamingCode: true,
        connectionState: "closed",
        tokenUsage: 999,
        reasoningTokens: 77,
        maxContextTokens: 200000,
        activeThreadId: 7,
      });
      useTimelineStore.getState().switchThread(99, null, false);
    });
    const s = useTimelineStore.getState();
    expect(s.items).toEqual([]);
    expect(s.turnActive).toBe(false);
    expect(s.streamingCode).toBe(false);
    // The switch preserves the live connection state — stamping "open" here
    // would clear the disconnect banner while the stream is actually
    // reconnecting (Task #1051).
    expect(s.connectionState).toBe("closed");
    expect(s.tokenUsage).toBe(0);
    expect(s.reasoningTokens).toBe(0);
    expect(s.maxContextTokens).toBe(0);
    expect(s.activeThreadId).toBe(99);
  });

  it("hot switch (cached items) installs the cached items + sets activeThreadId + zeros tokens (useTokenUsage restores)", () => {
    act(() => {
      useTimelineStore.setState({
        items: [item({ payload: "stale" })],
        turnActive: true,
        streamingCode: true,
        connectionState: "closed",
        tokenUsage: 555, // zeroed by the switch; useTokenUsage restores the hot value in the same commit
      });
      const cachedItems = [item({ payload: "restored" })];
      useTimelineStore.getState().switchThread(8, cachedItems, false);
    });
    const s = useTimelineStore.getState();
    expect(s.items).toHaveLength(1);
    expect(s.items[0].payload).toBe("restored");
    expect(s.turnActive).toBe(false);
    expect(s.streamingCode).toBe(false);
    // Live connection state preserved (Task #1051) — see the cold-switch test.
    expect(s.connectionState).toBe("closed");
    expect(s.activeThreadId).toBe(8);
    // Token fields go cold on switch — the single applyTokenUsage gate (driven
    // by useTokenUsage) writes the real value, never the switch action.
    expect(s.tokenUsage).toBe(0);
  });

  it("switchThread preserves the connection state (Task #1051 regression: it used to stamp 'open', hiding a real disconnect)", () => {
    act(() => {
      useTimelineStore.setState({ connectionState: "closed" });
      useTimelineStore.getState().switchThread(99, null, false);
    });
    expect(useTimelineStore.getState().connectionState).toBe("closed");
    // A healthy connection stays "open" through the switch.
    act(() => {
      useTimelineStore.setState({ connectionState: "open" });
      useTimelineStore.getState().switchThread(97, null, false);
    });
    expect(useTimelineStore.getState().connectionState).toBe("open");
  });

  it("hot switch carries hasMoreOlder from the cache; cold switch forces it false", () => {
    act(() => {
      useTimelineStore.getState().switchThread(3, [item({ payload: "hot" })], true);
    });
    expect(useTimelineStore.getState().hasMoreOlder).toBe(true);
    act(() => {
      // cold switch ignores the passed hasMoreOlder (no cache → nothing older loaded yet)
      useTimelineStore.getState().switchThread(4, null, true);
    });
    expect(useTimelineStore.getState().hasMoreOlder).toBe(false);
  });
});

describe("applyTokenUsage", () => {
  it("writes all token fields atomically", () => {
    act(() => {
      useTimelineStore.setState({ tokenUsage: 1, reasoningTokens: 2, maxContextTokens: 3 });
      useTimelineStore.getState().applyTokenUsage(1200, 300, 200000, 120000, 160000);
    });
    const s = useTimelineStore.getState();
    expect(s.tokenUsage).toBe(1200);
    expect(s.reasoningTokens).toBe(300);
    expect(s.maxContextTokens).toBe(200000);
    expect(s.softCompactTokens).toBe(120000);
    expect(s.hardCompactTokens).toBe(160000);
  });

  it("resets all token fields to 0 (cold path)", () => {
    act(() => {
      useTimelineStore.setState({
        tokenUsage: 900,
        reasoningTokens: 80,
        maxContextTokens: 128000,
        softCompactTokens: 76800,
        hardCompactTokens: 102400,
      });
      useTimelineStore.getState().applyTokenUsage(0, 0, 0, 0, 0);
    });
    const s = useTimelineStore.getState();
    expect(s.tokenUsage).toBe(0);
    expect(s.reasoningTokens).toBe(0);
    expect(s.maxContextTokens).toBe(0);
    expect(s.softCompactTokens).toBe(0);
    expect(s.hardCompactTokens).toBe(0);
  });
});

// -- scrollToBottomRequest: the single force-scroll signal ──────────────────
//
// The timeline honors exactly one force-scroll trigger; the store owns it.
// It MUST bump on agent switch (switchThread) and on send
// (requestScrollToBottom), and MUST NOT bump on a mid-stream snapshot refresh
// (reloadSnapshot) or a scroll-up load-older (prependOlder) — either of those
// bumping would yank the viewport to the bottom while the user is reading.
describe("scrollToBottomRequest force-scroll signal", () => {
  it("requestScrollToBottom increments the counter (send path)", () => {
    act(() => {
      useTimelineStore.setState({ scrollToBottomRequest: 5 });
      useTimelineStore.getState().requestScrollToBottom();
    });
    expect(useTimelineStore.getState().scrollToBottomRequest).toBe(6);
  });

  it("switchThread bumps it (hot-cache agent switch)", () => {
    act(() => {
      useTimelineStore.setState({ scrollToBottomRequest: 2 });
      useTimelineStore.getState().switchThread(8, [item({ payload: "x" })], false);
    });
    expect(useTimelineStore.getState().scrollToBottomRequest).toBe(3);
  });

  it("switchThread bumps it (cold-cache agent switch)", () => {
    act(() => {
      useTimelineStore.setState({ scrollToBottomRequest: 9 });
      useTimelineStore.getState().switchThread(11, null, false);
    });
    expect(useTimelineStore.getState().scrollToBottomRequest).toBe(10);
  });

  it("reloadSnapshot does NOT bump it (mid-stream refresh must not force-scroll)", () => {
    act(() => {
      useTimelineStore.setState({ scrollToBottomRequest: 4 });
      useTimelineStore.getState().reloadSnapshot([item({ item_id: "0.0", payload: "s" })], 1, false);
    });
    expect(useTimelineStore.getState().scrollToBottomRequest).toBe(4);
  });

  it("prependOlder does NOT bump it (scroll-up load-older must not force-scroll)", () => {
    act(() => {
      useTimelineStore.setState({
        items: [item({ item_id: "5.0", payload: "curr" })],
        scrollToBottomRequest: 7,
        hasMoreOlder: true,
        loadingOlder: true,
      });
      useTimelineStore.getState().prependOlder([item({ item_id: "1.0", payload: "old" })], false);
    });
    expect(useTimelineStore.getState().scrollToBottomRequest).toBe(7);
  });
});

// -- reloadSnapshot ─────────────────────────────────────────────────────────

describe("reloadSnapshot", () => {
  it("hard reset: stable-id partial discarded when not in snapshot, replaced by snapshot", () => {
    // New hard-reset semantics: snapshot is the only truth. Old partial item_id="9.9" not in
    // snapshot → discard (instead of keeping as streaming). Each node enter triggers a new
    // snapshot reload; dirty partials are auto-cleaned.
    const partial = item({ item_id: "9.9", payload: "streaming...", partial: true });
    act(() => {
      useTimelineStore.setState({ items: [partial] });
      const snapshot = [item({ item_id: "1.0", kind: "inbound_chat", payload: "msg" })];
      useTimelineStore.getState().reloadSnapshot(snapshot, 0, false);
    });
    const after = useTimelineStore.getState().items;
    expect(after.length).toBe(1);
    expect(after[0].item_id).toBe("1.0");
    // partial 9.9 cleared by hard reset
    expect(after.some((i) => i.item_id === "9.9")).toBe(false);
  });

  // Regression for #1142: a still-streaming item's own msg_idx equals
  // msg_count BY DEFINITION (agent/graph/_callbacks.py + _exec.py both
  // stamp len(state.messages) — the count BEFORE this uncommitted message).
  // The old guard treated that equality as proof the snapshot was stale and
  // bailed out before ever calling mergeSnapshotWithStreaming — on a cold
  // thread (no history folded yet, only the one streaming item from SSE)
  // that discarded the ONLY response carrying history.
  it("cold thread with one streaming item at msg_idx == msg_count: snapshot still merges in the missing history (#1142)", () => {
    act(() => {
      // cold thread: SSE folds in exactly one streaming item at msg_idx 5.
      useTimelineStore.getState().processSseEvent({ role: "code_start", agent_id: 42, item_id: "5.0" });
    });
    expect(useTimelineStore.getState().items.map((i) => i.item_id)).toEqual(["5.0"]);

    act(() => {
      // HTTP snapshot lands: committed history 0..4, msg_count=5 — the same
      // horizon the streaming item already claims (currentMax === msg_count).
      const history = [0, 1, 2, 3, 4].map((i) =>
        item({ item_id: `${i}.0`, kind: "agent_chat", payload: `msg ${i}` }),
      );
      useTimelineStore.getState().reloadSnapshot(history, 5, false);
    });
    const ids = useTimelineStore.getState().items.map((i) => i.item_id);
    expect(ids).toEqual(["0.0", "1.0", "2.0", "3.0", "4.0", "5.0"]);
  });
});

// -- cancelled → drop the streamed bubbles by id (streamingIds) ─────────────

describe("compact_done hard reset (incremental design)", () => {
  it("active-thread compact_done KEEPS the pre-compact items visible and arms the reset window", () => {
    // Regression for "compact 完成后 context UI 不立即刷新": clearing the
    // items on compact_done blanked the context panel for the whole reset
    // window (sub-second locally, seconds on remote machines). The old
    // context stays until the first post-compact snapshot replaces it.
    act(() => {
      useTimelineStore.getState().switchThread(
        1,
        [
          item({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
          item({ item_id: "90.0", kind: "inbound_chat", payload: "pre-compact" }),
          item({ item_id: "91.0", kind: "agent_chat", payload: "pre-compact reply" }),
        ],
        true,
      );
    });
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "compact_done",
        agent_id: 1,
      });
    });
    const s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["0.0", "90.0", "91.0"]);
    expect(s.streamingIds.size).toBe(0);
    expect(s.resetPending).toBe(true);
  });

  it("an EMPTY timeline_snapshot inside the reset window is ignored (does not blank the panel)", () => {
    // The post-REMOVE_ALL init_context enter used to emit an empty full-window
    // snapshot; replacing with [] inside the reset window blanked the context
    // panel until the rebuilt-history snapshot arrived. It must be skipped —
    // only a snapshot that actually carries the new history ends the window.
    act(() => {
      useTimelineStore.getState().switchThread(
        1,
        [item({ item_id: "90.0", kind: "inbound_chat", payload: "pre-compact" })],
        false,
      );
    });
    act(() => {
      useTimelineStore.getState().processSseEvent({ role: "compact_done", agent_id: 1 });
    });
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "timeline_snapshot",
        agent_id: 1,
        msg_count: 0,
        items: [],
      });
    });
    const s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["90.0"]); // still visible
    expect(s.resetPending).toBe(true); // window still open
  });

  it("parked-thread compact_done marks the thread: switch-back seeds cold with the reset window armed", () => {
    // A compact that completes while the thread is parked must not let a
    // later switch-back seed the lagging pre-compact snapshot and keep-merge
    // it back in. The marker makes switchThread seed cold + arm resetPending.
    act(() => {
      useTimelineStore.getState().switchThread(
        1,
        [item({ item_id: "90.0", kind: "inbound_chat", payload: "pre-compact" })],
        false,
      );
    });
    // Park thread 1 by switching to thread 2.
    act(() => {
      useTimelineStore.getState().switchThread(2, [item({ item_id: "1.0", payload: "other" })], false);
    });
    // compact_done arrives for the parked thread 1.
    act(() => {
      useTimelineStore.getState().processSseEvent({ role: "compact_done", agent_id: 1 });
    });
    expect(useTimelineStore.getState().compactedThreadIds.has(1)).toBe(true);
    // Switch back: the (possibly lagging) items stay visible — consistent
    // with the active-thread keep-items behavior — but the reset window is
    // armed so the first post-compact snapshot replaces wholesale instead of
    // keep-merging the old history back in.
    act(() => {
      useTimelineStore.getState().switchThread(1, [item({ item_id: "90.0", payload: "LAGGING cache" })], false);
    });
    const s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["90.0"]);
    expect(s.resetPending).toBe(true);
    expect(s.compactedThreadIds.has(1)).toBe(false); // marker consumed
    // The first post-compact snapshot replaces wholesale.
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "timeline_snapshot",
        agent_id: 1,
        msg_count: 2,
        items: [item({ item_id: "1.0", payload: "[summary]" })],
      });
    });
    expect(useTimelineStore.getState().items.map((i) => i.item_id)).toEqual(["1.0"]);
    expect(useTimelineStore.getState().resetPending).toBe(false);
  });

  it("bucketless compact marker is consumed by the post-compact snapshot (Task #994: stale reset window on switch-back)", () => {
    // The user scenario: an agent compacts while it has NO parked bucket (the
    // user is on another thread and never visited it). compact_done sets the
    // marker; the post-compact FULL snapshot arrives while the thread is still
    // bucketless and is dropped; a later switch-back then sees the marker and
    // arms a stale reset window — the GET is dropped inside the window and the
    // FIRST incremental snapshot (agent still streaming) replaces the fresh
    // seed wholesale, leaving only the tail ("只显示最后一个 detail block，
    // 之前所有消息不触发加载").
    // Active on thread 2; thread 1 has no bucket.
    act(() => {
      useTimelineStore.getState().switchThread(2, [item({ item_id: "2.0", payload: "other" })], false);
    });
    // compact_done for the bucketless thread 1 → marker set.
    act(() => {
      useTimelineStore.getState().processSseEvent({ role: "compact_done", agent_id: 1 });
    });
    expect(useTimelineStore.getState().compactedThreadIds.has(1)).toBe(true);
    // The post-compact FULL snapshot arrives while thread 1 is still
    // bucketless. Its job (the reset window's whole purpose) is done: the
    // marker must be consumed so a later switch-back does NOT arm a stale
    // window that has no full snapshot to wait for.
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "timeline_snapshot",
        agent_id: 1,
        msg_count: 2,
        items: [item({ item_id: "1.0", kind: "system_marker", payload: "[summary]" })],
      });
    });
    expect(useTimelineStore.getState().compactedThreadIds.has(1)).toBe(false);
    // Switch back with a fresh post-compact cache: no reset window, the GET
    // applies, and an incremental streaming snapshot keep-merges.
    act(() => {
      useTimelineStore.getState().switchThread(
        1,
        [item({ item_id: "1.0", kind: "system_marker", payload: "[summary]" })],
        true,
      );
    });
    let s = useTimelineStore.getState();
    expect(s.resetPending).toBe(false);
    // The HTTP snapshot (tail window with history) must apply, not be dropped.
    act(() => {
      useTimelineStore.getState().reloadSnapshot(
        [
          item({ item_id: "1.0", kind: "system_marker", payload: "[summary]" }),
          item({ item_id: "2.0", kind: "agent_chat", payload: "earlier message" }),
        ],
        3,
        true,
      );
    });
    s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["1.0", "2.0"]);
    // The next streaming snapshot (incremental tail — "the last detail block")
    // must keep-merge onto the history, not replace it wholesale.
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "timeline_snapshot",
        agent_id: 1,
        msg_count: 3,
        items: [item({ item_id: "3.0", kind: "code_output", payload: "last block" })],
      });
    });
    s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["1.0", "2.0", "3.0"]);
    expect(s.hasMoreOlder).toBe(true);
  });

  it("the first post-compact snapshot replaces items wholesale and clears the reset window", () => {
    act(() => {
      useTimelineStore.getState().switchThread(1, [item({ item_id: "90.0", payload: "old" })], false);
    });
    act(() => {
      useTimelineStore.getState().processSseEvent({ role: "compact_done", agent_id: 1 });
    });
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "timeline_snapshot",
        agent_id: 1,
        msg_count: 3,
        items: [
          item({ item_id: "1.0", kind: "system_marker", payload: "[summary]" }),
          item({ item_id: "2.0", kind: "agent_chat", payload: "post-compact" }),
        ],
      });
    });
    const s = useTimelineStore.getState();
    // Wholesale replace: pre-compact 90.0 must NOT survive (keep-all merge would resurrect it).
    expect(s.items.map((i) => i.item_id)).toEqual(["1.0", "2.0"]);
    expect(s.resetPending).toBe(false);
  });

  it("a GET reload inside the reset window is dropped (checkpoint may lag pre-compact); hasMoreOlder still refreshes", () => {
    act(() => {
      useTimelineStore.getState().switchThread(1, [item({ item_id: "90.0", payload: "old" })], true);
    });
    act(() => {
      useTimelineStore.getState().processSseEvent({ role: "compact_done", agent_id: 1 });
    });
    act(() => {
      useTimelineStore.getState().reloadSnapshot(
        [item({ item_id: "90.0", kind: "inbound_chat", payload: "LAGGING pre-compact" })],
        95,
        true,
      );
    });
    const s = useTimelineStore.getState();
    // The lagging GET must not change the visible items — they stay exactly
    // as compact_done left them (kept pre-compact, awaiting the snapshot).
    expect(s.items.map((i) => i.item_id)).toEqual(["90.0"]);
    expect(s.hasMoreOlder).toBe(true); // but has_more refresh is safe
    expect(s.resetPending).toBe(true);
  });

  it("SSE reconnect clears the reset window so a GET can apply again", () => {
    act(() => {
      useTimelineStore.getState().switchThread(1, [item({ item_id: "90.0", payload: "old" })], false);
    });
    act(() => {
      useTimelineStore.getState().processSseEvent({ role: "compact_done", agent_id: 1 });
    });
    expect(useTimelineStore.getState().resetPending).toBe(true);
    act(() => {
      useTimelineStore.getState().processConnectionEvent({ type: "open" });
    });
    expect(useTimelineStore.getState().resetPending).toBe(false);
  });
});

describe("processSseEvent: cancelled", () => {
  // A cancelled generation is discarded by the kernel (commits nothing). The
  // cancel event drops exactly the ids streamed this turn (streamingIds) —
  // not a msg_count boundary that could be stale after SSE-missed snapshots —
  // keeps committed items, clears the executing… marker, and flips the flags.
  it("drops the streamed generation bubble by id, keeps committed items, flips flags", () => {
    const committed = item({ item_id: "2.0", kind: "agent_chat", payload: "earlier reply" });
    act(() => {
      useTimelineStore.setState({ items: [committed], streamingIds: new Set(), turnActive: true });
      const ps = useTimelineStore.getState().processSseEvent;
      ps({ role: "chat_start", agent_id: 42, item_id: "3.0" }); // tracked in streamingIds
      ps({ role: "chat_delta", agent_id: 42, item_id: "3.0", content: "half-wri" });
      ps({ role: "reasoning_delta", agent_id: 42, item_id: "3.1", content: "thinking" });
      ps({ role: "cancelled", agent_id: 42 });
    });
    const s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["2.0"]); // committed kept, streamed 3.0/3.1 dropped
    expect(s.turnActive).toBe(false);
    expect(s.streamingCode).toBe(false);
    expect(s.streamingIds.size).toBe(0); // reset for the next turn
  });

  it("keeps a partial code_output (exec interrupted) but clears empty exec placeholder", () => {
    act(() => {
      useTimelineStore.setState({ items: [], streamingIds: new Set() });
      const ps = useTimelineStore.getState().processSseEvent;
      // exec_start now creates a code_output placeholder (with item_id)
      ps({ role: "exec_start", agent_id: 42, item_id: "5.0" });
      ps({ role: "exec_output_chunk", agent_id: 42, item_id: "5.0", content: "running…" }); // NOT tracked
      // Second exec_start creates a new empty placeholder at 6.0
      ps({ role: "exec_start", agent_id: 42, item_id: "6.0" });
      ps({ role: "cancelled", agent_id: 42 });
    });
    const items = useTimelineStore.getState().items;
    // 5.0 has content → survives cancel
    expect(items.map((i) => i.item_id)).toContain("5.0");
    // 6.0 is empty + no exec_ms → cleared by cancel
    expect(items.map((i) => i.item_id)).not.toContain("6.0");
  });

  it("a snapshot that commits a streamed id stops tracking it, so a later cancel keeps it", () => {
    act(() => {
      useTimelineStore.setState({ items: [], streamingIds: new Set() });
      const ps = useTimelineStore.getState().processSseEvent;
      ps({ role: "chat_start", agent_id: 42, item_id: "3.0" }); // tracked
      // snapshot commits 3.0 → it leaves streamingIds
      ps({
        role: "timeline_snapshot",
        agent_id: 42,
        items: [item({ item_id: "3.0", kind: "agent_chat", payload: "committed" })],
        msg_count: 4,
      });
      ps({ role: "cancelled", agent_id: 42 });
    });
    // 3.0 was committed before the cancel → not dropped
    expect(useTimelineStore.getState().items.map((i) => i.item_id)).toEqual(["3.0"]);
  });
});

describe("processSseEvent: live thinking clock freezes per block", () => {
  // A reasoning item streamed this turn carries the frontend clock anchor
  // (reasoningStartedAt). When a later block starts or the turn ends, the
  // clock is frozen: the anchor is dropped and the block's elapsed is stamped
  // into reasoningElapsedMs, so the chip shows a frozen "Thought for Xs"
  // instead of a clock ticking forever (turn end) or reverting to bare
  // "Thinking" (later block). Each reasoning block is timed independently.
  it("llm_done freezes an uncommitted reasoning item (anchor dropped, elapsed stamped)", () => {
    act(() => {
      useTimelineStore.setState({ items: [], streamingIds: new Set() });
      const ps = useTimelineStore.getState().processSseEvent;
      ps({ role: "reasoning_start", agent_id: 42, item_id: "3.0" }); // anchors the clock
      ps({ role: "reasoning_delta", agent_id: 42, item_id: "3.0", content: "hmm" });
      ps({ role: "llm_done", agent_id: 42 });
    });
    const it30 = useTimelineStore.getState().items.find((i) => i.item_id === "3.0");
    expect(it30).toBeDefined();
    expect(it30?.reasoningStartedAt).toBeUndefined();
    expect(typeof it30?.reasoningElapsedMs).toBe("number");
  });

  it("chat_start freezes the live reasoning clock of the prior block", () => {
    act(() => {
      useTimelineStore.setState({ items: [], streamingIds: new Set() });
      const ps = useTimelineStore.getState().processSseEvent;
      ps({ role: "reasoning_start", agent_id: 42, item_id: "3.0" });
      ps({ role: "reasoning_delta", agent_id: 42, item_id: "3.0", content: "plan" });
      ps({ role: "chat_start", agent_id: 42, item_id: "3.1" });
    });
    const reasoning = useTimelineStore.getState().items.find((i) => i.item_id === "3.0");
    expect(reasoning?.reasoningStartedAt).toBeUndefined();
    expect(typeof reasoning?.reasoningElapsedMs).toBe("number");
  });

  it("a second reasoning_start freezes the prior reasoning block only", () => {
    act(() => {
      useTimelineStore.setState({ items: [], streamingIds: new Set() });
      const ps = useTimelineStore.getState().processSseEvent;
      ps({ role: "reasoning_start", agent_id: 42, item_id: "3.0" });
      ps({ role: "reasoning_delta", agent_id: 42, item_id: "3.0", content: "first" });
      ps({ role: "reasoning_start", agent_id: 42, item_id: "3.2" }); // new block
      ps({ role: "reasoning_delta", agent_id: 42, item_id: "3.2", content: "second" });
    });
    const items = useTimelineStore.getState().items;
    const first = items.find((i) => i.item_id === "3.0");
    const second = items.find((i) => i.item_id === "3.2");
    // prior block frozen, new block still live
    expect(first?.reasoningStartedAt).toBeUndefined();
    expect(typeof first?.reasoningElapsedMs).toBe("number");
    expect(typeof second?.reasoningStartedAt).toBe("number");
    expect(second?.reasoningElapsedMs).toBeUndefined();
  });

  it("a committed item (reasoning_ms set) keeps its fields through turn end", () => {
    act(() => {
      useTimelineStore.setState({
        items: [
          item({
            item_id: "3.0",
            kind: "agent_reasoning",
            payload: "done thinking",
            reasoning_ms: 2200,
          }),
        ],
        streamingIds: new Set(),
      });
      useTimelineStore.getState().processSseEvent({ role: "llm_done", agent_id: 42 });
    });
    const it30 = useTimelineStore.getState().items.find((i) => i.item_id === "3.0");
    expect(it30?.reasoning_ms).toBe(2200);
  });
});

// -- scroll-up older-window loading ─────────────────────────────────────

describe("prependOlder / load-older flags", () => {
  it("beginLoadOlder sets loadingOlder; prependOlder prepends + sorts + clears it", () => {
    act(() => {
      useTimelineStore.setState({
        items: [
          item({ item_id: "5.0", kind: "inbound_chat", payload: "recent" }),
          item({ item_id: "6.0", payload: "newest" }),
        ],
        hasMoreOlder: true,
        loadingOlder: false,
      });
      useTimelineStore.getState().beginLoadOlder();
    });
    expect(useTimelineStore.getState().loadingOlder).toBe(true);

    act(() => {
      useTimelineStore.getState().prependOlder(
        [
          item({ item_id: "1.0", kind: "inbound_chat", payload: "oldest" }),
          item({ item_id: "2.0", payload: "old reply" }),
        ],
        false,
      );
    });
    const s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["1.0", "2.0", "5.0", "6.0"]);
    expect(s.hasMoreOlder).toBe(false);
    expect(s.loadingOlder).toBe(false);
  });

  it("prependOlder dedupes by item_id (a tail snapshot may already hold some)", () => {
    act(() => {
      useTimelineStore.setState({
        items: [item({ item_id: "5.0", kind: "inbound_chat", payload: "recent" })],
        hasMoreOlder: true,
        loadingOlder: true,
      });
      useTimelineStore.getState().prependOlder(
        [
          item({ item_id: "4.0", payload: "older" }),
          item({ item_id: "5.0", kind: "inbound_chat", payload: "dup recent" }),
        ],
        true,
      );
    });
    const s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["4.0", "5.0"]);
    expect(s.items.filter((i) => i.item_id === "5.0")).toHaveLength(1);
    expect(s.hasMoreOlder).toBe(true);
  });

  it("switchThread clears hasMoreOlder + loadingOlder (cold)", () => {
    act(() => {
      useTimelineStore.setState({ hasMoreOlder: true, loadingOlder: true });
      useTimelineStore.getState().switchThread(7, null, false);
    });
    const s = useTimelineStore.getState();
    expect(s.hasMoreOlder).toBe(false);
    expect(s.loadingOlder).toBe(false);
  });
});

// -- per-thread routing: background folding + hot switch-back + LRU (PR3) ────
//
// The all-events SSE stream carries every agent's events. The ACTIVE thread
// folds into the top-level fields; a switched-away (parked) thread folds into
// its `threads` bucket so switch-back is instant (R2/R3); an unvisited thread
// is dropped (bounded). Parked buckets are LRU-capped at MAX_PARKED_THREADS(32).

describe("processSseEvent per-thread routing (R2/R3)", () => {
  it("background SSE for a switched-away thread folds into its parked bucket; switch-back restores it instantly", () => {
    const ps = useTimelineStore.getState().processSseEvent;
    // Stream a chat bubble into thread 1 (active path).
    act(() => {
      useTimelineStore.getState().switchThread(1, null, false);
      ps({ role: "chat_start", agent_id: 1, item_id: "3.0" });
      ps({ role: "chat_delta", agent_id: 1, item_id: "3.0", content: "first" });
    });
    expect(useTimelineStore.getState().items.map((i) => i.item_id)).toEqual(["3.0"]);

    // Switch to thread 2 — thread 1 is parked with its bubble.
    act(() => {
      useTimelineStore.getState().switchThread(2, null, false);
    });
    expect(useTimelineStore.getState().items).toEqual([]); // active view (thread 2) empty
    expect(useTimelineStore.getState().threads.has(1)).toBe(true);

    // A background delta for the PARKED thread 1 folds into its bucket, not the
    // active view.
    act(() => {
      ps({ role: "chat_delta", agent_id: 1, item_id: "3.0", content: " more" });
    });
    expect(useTimelineStore.getState().items).toEqual([]); // active thread 2 untouched
    expect(useTimelineStore.getState().threads.get(1)?.items[0]?.payload).toBe("first more");

    // Switch back to thread 1 → unpark the live-folded bucket instantly (the
    // background delta is already there, no refetch needed).
    act(() => {
      useTimelineStore.getState().switchThread(1, null, false);
    });
    const s = useTimelineStore.getState();
    expect(s.activeThreadId).toBe(1);
    expect(s.items[0]?.payload).toBe("first more");
    expect(s.threads.has(1)).toBe(false); // the active thread is never in the map
  });

  it("background SSE for an unvisited thread is dropped (no bucket is created)", () => {
    act(() => {
      useTimelineStore.getState().switchThread(1, null, false);
      useTimelineStore.getState().processSseEvent({ role: "chat_start", agent_id: 99, item_id: "1.0" });
    });
    const s = useTimelineStore.getState();
    expect(s.items).toEqual([]); // active thread 1 untouched
    expect(s.threads.has(99)).toBe(false); // no bucket spun up for a thread never visited
  });

  it("compact_done for a parked thread marks it and keeps its bucket reset-pending (a history shrink can't be folded; the first snapshot replaces)", () => {
    const ps = useTimelineStore.getState().processSseEvent;
    act(() => {
      useTimelineStore.getState().switchThread(1, null, false);
      ps({ role: "chat_start", agent_id: 1, item_id: "3.0" }); // thread 1 has a bubble
      useTimelineStore.getState().switchThread(2, null, false); // park thread 1, active=2
    });
    expect(useTimelineStore.getState().threads.get(1)?.items).toHaveLength(1);

    act(() => {
      ps({ role: "compact_done", agent_id: 1 }); // the parked thread compacts
    });
    // The bucket is kept (items still visible on switch-back) with the reset
    // window armed, and the thread is marked so a bucketless switch-back
    // seeds cold. The post-compact snapshot then replaces wholesale.
    const s = useTimelineStore.getState();
    expect(s.threads.get(1)?.resetPending).toBe(true);
    expect(s.compactedThreadIds.has(1)).toBe(true);
    expect(s.items).toEqual([]); // active thread 2 untouched

    act(() => {
      ps({
        role: "timeline_snapshot",
        agent_id: 1,
        msg_count: 2,
        items: [item({ item_id: "1.0", payload: "[summary]" })],
      });
    });
    const s2 = useTimelineStore.getState();
    expect(s2.threads.get(1)?.items.map((i) => i.item_id)).toEqual(["1.0"]);
    expect(s2.threads.get(1)?.resetPending).toBe(false);
    expect(s2.compactedThreadIds.has(1)).toBe(false);
  });

  it("compact_done for a thread with no bucket marks it (no bucket created; switch-back seeds cold)", () => {
    act(() => {
      useTimelineStore.getState().switchThread(1, null, false);
      useTimelineStore.getState().processSseEvent({ role: "compact_done", agent_id: 77 });
    });
    const s = useTimelineStore.getState();
    expect(s.threads.has(77)).toBe(false); // no bucket spun up
    expect(s.compactedThreadIds.has(77)).toBe(true); // but the marker is set
  });

  it("token_usage for a parked thread is dropped; for the active thread it writes the top-level token fields", () => {
    const ps = useTimelineStore.getState().processSseEvent;
    act(() => {
      useTimelineStore.getState().switchThread(1, null, false);
      useTimelineStore.getState().switchThread(2, null, false); // thread 1 parked, active=2
      useTimelineStore.setState({ tokenUsage: 0, reasoningTokens: 0 });
      // token for the parked thread 1 → dropped (tokens are React-Query-cached per thread)
      ps({ role: "token_usage", agent_id: 1, input_tokens: 111, output_tokens: 0 });
    });
    expect(useTimelineStore.getState().tokenUsage).toBe(0);
    // token for the active thread 2 → writes top-level
    act(() => {
      ps({ role: "token_usage", agent_id: 2, input_tokens: 222, output_tokens: 0, reasoning_tokens: 7 });
    });
    expect(useTimelineStore.getState().tokenUsage).toBe(222);
    expect(useTimelineStore.getState().reasoningTokens).toBe(7);
  });
});

describe("system-prompt item through park / fold / switch-back", () => {
  // The system-prompt item (0.0, ~128KB) is a normal timeline item: it arrives
  // in full-window snapshots (spawn / compact shrink / claim fallback) and the
  // merge's id-replace keeps one copy per thread; incremental snapshots never
  // carry it. Parked buckets keep whatever the merge produced — the LRU cap
  // (MAX_PARKED_THREADS) bounds retained copies.

  it("parking keeps the system-prompt item; switch-back restores the bucket as-is", () => {
    const sysPrompt = item({ item_id: "0.0", kind: "system_prompt", payload: "X".repeat(10_000) });
    const chat = item({ item_id: "1.0", kind: "agent_chat", payload: "hi" });
    act(() => {
      useTimelineStore.getState().switchThread(1, [sysPrompt, chat], false);
    });
    // Park thread 1 (switch to thread 2): the bucket keeps its items untouched.
    act(() => {
      useTimelineStore.getState().switchThread(2, null, false);
    });
    const bucket = useTimelineStore.getState().threads.get(1);
    expect(bucket?.items.map((i) => i.item_id)).toEqual(["0.0", "1.0"]);
    expect(bucket?.items[0]).toBe(sysPrompt); // exact same object — no re-alloc

    // Switch back: the parked bucket wins; the expandable card is there
    // immediately, no restore dance.
    act(() => {
      useTimelineStore.getState().switchThread(1, [sysPrompt, chat], false);
    });
    const s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["0.0", "1.0"]);
    expect(s.items[0]).toBe(sysPrompt);
  });

  it("a timeline_snapshot folded into a parked bucket keeps the system-prompt item", () => {
    const ps = useTimelineStore.getState().processSseEvent;
    act(() => {
      useTimelineStore.getState().switchThread(1, null, false);
      useTimelineStore.getState().switchThread(2, null, false); // park thread 1
      ps({
        role: "timeline_snapshot",
        agent_id: 1,
        msg_count: 3,
        items: [
          item({ item_id: "0.0", kind: "system_prompt", payload: "BIG" }),
          item({ item_id: "2.0", kind: "agent_chat", payload: "hello" }),
        ],
      });
    });
    const bucket = useTimelineStore.getState().threads.get(1);
    expect(bucket?.items.map((i) => i.item_id)).toEqual(["0.0", "2.0"]);
    expect(bucket?.items[0]?.payload).toBe("BIG");
  });

  it("a snapshot without the system-prompt item keeps the prev 0.0 object verbatim (no 128KB re-alloc)", () => {
    // Incremental snapshots never carry 0.0 (message 0 is below the cursor);
    // the merge's generic keep rule must preserve the existing object — a
    // fresh string would re-allocate ~128KB per snapshot event.
    const sysPrompt = item({ item_id: "0.0", kind: "system_prompt", payload: "same" });
    act(() => {
      useTimelineStore.getState().switchThread(
        1,
        [sysPrompt, item({ item_id: "1.0", kind: "agent_chat", payload: "a" })],
        false,
      );
    });
    const before = useTimelineStore.getState().items[0];
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "timeline_snapshot",
        agent_id: 1,
        msg_count: 3,
        items: [
          item({ item_id: "1.0", kind: "agent_chat", payload: "a" }),
          item({ item_id: "2.0", kind: "agent_chat", payload: "b" }),
        ],
      });
    });
    const after = useTimelineStore.getState().items;
    expect(after[0]).toBe(before); // same object ref — no fresh 128KB string
    expect(after.map((i) => i.item_id)).toEqual(["0.0", "1.0", "2.0"]);
  });

  it("a snapshot that DOES carry a changed 0.0 (full-window paths: spawn / compact / restart) replaces it", () => {
    const sysPrompt = item({ item_id: "0.0", kind: "system_prompt", payload: "old" });
    act(() => {
      useTimelineStore.getState().switchThread(1, [sysPrompt], false);
    });
    const fresh = item({ item_id: "0.0", kind: "system_prompt", payload: "new" });
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "timeline_snapshot",
        agent_id: 1,
        msg_count: 1,
        items: [fresh],
      });
    });
    const s = useTimelineStore.getState();
    expect(s.items[0]).toBe(fresh);
    expect(s.items[0]?.payload).toBe("new");
  });
});

describe("switchThread LRU eviction (parked-thread cap)", () => {
  it("parking beyond MAX_PARKED_THREADS evicts the least-recently-parked; the active thread is never evicted", () => {
    // resetStore leaves active=42. Visiting 1..40 parks 42 then 1..39 (40 parks);
    // the 32-slot cap keeps the 32 most-recently-parked (8..39), evicting 42 and 1..7.
    act(() => {
      for (let id = 1; id <= 40; id++) {
        useTimelineStore.getState().switchThread(id, null, false);
      }
    });
    const s = useTimelineStore.getState();
    expect(s.activeThreadId).toBe(40);
    expect(s.threads.size).toBe(32);
    expect(s.threads.has(42)).toBe(false); // oldest parked, evicted
    expect(s.threads.has(1)).toBe(false); // oldest of the new batch, evicted
    expect(s.threads.has(7)).toBe(false); // last evicted before cap boundary
    expect(s.threads.has(8)).toBe(true);  // first kept (40 - 32 = 8)
    expect(s.threads.has(39)).toBe(true); // most recent parked
    expect(s.threads.has(40)).toBe(false); // active — lives in top-level, not the map
  });

  it("revisiting an LRU-evicted thread cold-seeds (its live bucket is gone → the hook cold-fetches)", () => {
    act(() => {
      for (let id = 1; id <= 40; id++) useTimelineStore.getState().switchThread(id, null, false);
    });
    expect(useTimelineStore.getState().threads.has(1)).toBe(false); // evicted above (1..7 evicted, cap=32)

    act(() => {
      useTimelineStore.getState().switchThread(1, null, false); // no parked bucket → cold
    });
    const s = useTimelineStore.getState();
    expect(s.activeThreadId).toBe(1);
    expect(s.items).toEqual([]); // cold — nothing restored, a fresh fetch would fill it
  });
});

describe("spawn scenario: first snapshot carries 0.0 (#615)", () => {
  it("cold thread (empty GET at spawn) gets the system-prompt item from the first SSE snapshot", () => {
    // At spawn the checkpoint is empty (first super-step uncommitted), so
    // GET /timeline returns nothing and the frontend has no 0.0 copy. The
    // first full-window SSE snapshot now carries it — the id-replace merge
    // must add it to an empty prev.
    act(() => {
      useTimelineStore.getState().switchThread(1, [], false);
    });
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "timeline_snapshot",
        agent_id: 1,
        msg_count: 1,
        items: [
          item({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
        ],
      });
    });
    const s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["0.0"]);
    expect(s.items[0].payload).toBe("PROMPT");
  });
});

describe("processSseEventBatch — frame-level folding", () => {
  /** Minimal AgentRow for sidebar-owned roles the batch must skip. */
  function agentRow(id: number): AgentRow {
    return {
      agent_id: id,
      label: null,
      status: "running",
      spawner: "user",
      fork_source_agent_id: null,
      fork_source_checkpoint_id: null,
      pid: null,
      spawned_at: "2026-01-01T00:00:00Z",
      started_at: "2026-01-01T00:00:01Z",
      machine: "test",
      last_active_at: "2026-01-01T00:00:01Z",
      last_inbound_at: "2026-01-01T00:00:01Z",
      notices_awaiting_response: [],
      unread_notice_count: 0,
      heartbeat_paused_until: null,
      liveness_state: "online",
      last_probe_at: null,
    };
  }
  it("folds a whole frame's deltas in ONE store notification, in arrival order", () => {
    const listener = vi.fn();
    const unsub = useTimelineStore.subscribe(listener);
    act(() => {
      useTimelineStore.getState().processSseEventBatch([
        { role: "chat_start", agent_id: 42, item_id: "1.0" },
        { role: "chat_delta", agent_id: 42, item_id: "1.0", content: "he" },
        { role: "chat_delta", agent_id: 42, item_id: "1.0", content: "llo" },
        { role: "llm_done", agent_id: 42 },
      ]);
    });
    unsub();
    const s = useTimelineStore.getState();
    // One set() for the whole frame — the listener fired exactly once.
    expect(listener).toHaveBeenCalledTimes(1);
    expect(s.items).toHaveLength(1);
    expect(s.items[0].item_id).toBe("1.0");
    expect(s.items[0].payload).toBe("hello");
    expect(s.turnActive).toBe(false); // chat_start then llm_done within the frame
  });

  it("one set() per frame — subscriber notified once for a 4-event burst", () => {
    const listener = vi.fn();
    const unsub = useTimelineStore.subscribe(listener);
    act(() => {
      useTimelineStore.getState().processSseEventBatch([
        { role: "code_start", agent_id: 42, item_id: "2.0" },
        { role: "code_delta", agent_id: 42, item_id: "2.0", content: "x" },
        { role: "code_delta", agent_id: 42, item_id: "2.0", content: "y" },
        { role: "exec_start", agent_id: 42, item_id: "2.1" },
      ]);
    });
    unsub();
    expect(listener).toHaveBeenCalledTimes(1);
    const s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["2.0", "2.1"]);
  });

  it("empty batch and no-op batches never notify", () => {
    const listener = vi.fn();
    const unsub = useTimelineStore.subscribe(listener);
    act(() => {
      useTimelineStore.getState().processSseEventBatch([]);
      // agent_spawned / agent_updated are sidebar-owned — skipped entirely.
      useTimelineStore.getState().processSseEventBatch([
        { role: "agent_spawned", agent_id: 99, snapshot: agentRow(99) },
        { role: "agent_updated", agent_id: 99, snapshot: agentRow(99) },
      ]);
    });
    unsub();
    expect(listener).not.toHaveBeenCalled();
  });

  it("token_usage in a batch applies like the per-event path (last wins)", () => {
    act(() => {
      useTimelineStore.getState().processSseEventBatch([
        { role: "token_usage", agent_id: 42, input_tokens: 100, output_tokens: 1 },
        { role: "token_usage", agent_id: 42, input_tokens: 222, output_tokens: 2 },
        // A parked agent's token event is dropped (its own React Query cache owns it).
        { role: "token_usage", agent_id: 7, input_tokens: 999, output_tokens: 9 },
      ]);
    });
    const s = useTimelineStore.getState();
    expect(s.tokenUsage).toBe(222);
    expect(s.reasoningTokens).toBe(0);
  });

  it("a batch matches sequential per-event application (no divergence)", () => {
    // Park a background thread first.
    act(() => {
      useTimelineStore.getState().switchThread(42, [], false);
      useTimelineStore.getState().switchThread(1, [], false);
    });
    const frame = (): SystemEvent[] => [
      { role: "code_start", agent_id: 1, item_id: "3.0" },
      { role: "code_delta", agent_id: 1, item_id: "3.0", content: "pri" },
      { role: "code_delta", agent_id: 1, item_id: "3.0", content: "nt" },
      { role: "chat_delta", agent_id: 42, item_id: "9.0", content: "bg" },
      { role: "exec_output", agent_id: 1, item_id: "3.0", content: "done" },
    ];

    // Batch path on a fresh store…
    act(() => {
      useTimelineStore.getState().processSseEventBatch(frame());
    });
    const afterBatch = {
      items: useTimelineStore.getState().items,
      streamingCode: useTimelineStore.getState().streamingCode,
      turnActive: useTimelineStore.getState().turnActive,
      threads: new Map(useTimelineStore.getState().threads),
    };

    // …vs per-event path on an identical fresh store.
    resetStore();
    useTimelineStore.setState({ resetPending: false, compactedThreadIds: new Set() });
    act(() => {
      useTimelineStore.getState().switchThread(42, [], false);
      useTimelineStore.getState().switchThread(1, [], false);
      for (const ev of frame()) {
        useTimelineStore.getState().processSseEvent(ev);
      }
    });
    // created_at is stamped per item creation (new Date) — compare the
    // stable business fields only, both for the active thread and the
    // parked buckets.
    const strip = (list: BackendTimelineItem[]) =>
      list.map((i) => ({ item_id: i.item_id, payload: i.payload }));
    const stripThreads = (map: Map<number, unknown>) =>
      [...map.entries()].map(([id, t]) => [id, strip((t as { items: BackendTimelineItem[] }).items)]);
    expect(strip(useTimelineStore.getState().items)).toEqual(strip(afterBatch.items));
    expect(useTimelineStore.getState().streamingCode).toBe(afterBatch.streamingCode);
    expect(useTimelineStore.getState().turnActive).toBe(afterBatch.turnActive);
    expect(stripThreads(useTimelineStore.getState().threads)).toEqual(
      stripThreads(afterBatch.threads),
    );
  });
});
