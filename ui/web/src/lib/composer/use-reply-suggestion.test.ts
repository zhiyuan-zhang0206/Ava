import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BackendTimelineItem, ReplySuggestionResponse } from "../contracts/types";
import { api } from "../transport/api";
import { REPLY_SUGGESTION_DELAY_MS, useReplySuggestion } from "./use-reply-suggestion";

function item(kind: BackendTimelineItem["kind"], id: string, extra: Partial<BackendTimelineItem> = {}) {
  return {
    item_id: id,
    kind,
    payload: "text",
    source: null,
    created_at: null,
    inbound_id: null,
    ...extra,
  } as BackendTimelineItem;
}

const AGENT_SAYS = [item("inbound_chat", "1.0", { source: "user" }), item("agent_chat", "2.0")];

type Props = Parameters<typeof useReplySuggestion>[0];

const idle: Props = { agentId: 7, status: "idling", busy: false, items: AGENT_SAYS, enabled: true };

interface Pending {
  agentId: number;
  signal: AbortSignal;
  resolve: (response: ReplySuggestionResponse) => void;
  reject: (error: unknown) => void;
}

let pending: Pending[];
let visibility: "visible" | "hidden";

function setVisibility(value: "visible" | "hidden") {
  visibility = value;
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
}

async function elapse(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

async function settle(response: ReplySuggestionResponse, index = pending.length - 1) {
  await act(async () => {
    pending[index].resolve(response);
    await Promise.resolve();
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  pending = [];
  visibility = "visible";
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
  vi.spyOn(api, "getReplySuggestion").mockImplementation(
    (agentId, signal) =>
      new Promise<ReplySuggestionResponse>((resolve, reject) => {
        pending.push({ agentId, signal, resolve, reject });
      }),
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("useReplySuggestion", () => {
  it("requests after the quiet period, not before, and returns the answer", async () => {
    const { result } = renderHook((props: Props) => useReplySuggestion(props), {
      initialProps: idle,
    });

    await elapse(REPLY_SUGGESTION_DELAY_MS - 1);
    expect(api.getReplySuggestion).not.toHaveBeenCalled();

    await elapse(1);
    expect(api.getReplySuggestion).toHaveBeenCalledTimes(1);
    expect(pending[0].agentId).toBe(7);
    expect(result.current).toBeNull();

    await settle({ suggestions: ["Use A.", "Use B.", "Use C.", "Use D."], reason: null });
    expect(result.current).toEqual(["Use A.", "Use B.", "Use C.", "Use D."]);
  });

  it("starts nothing when a new turn begins within the quiet period", async () => {
    const { rerender } = renderHook((props: Props) => useReplySuggestion(props), {
      initialProps: idle,
    });

    await elapse(REPLY_SUGGESTION_DELAY_MS - 100);
    rerender({ ...idle, busy: true });
    await elapse(REPLY_SUGGESTION_DELAY_MS * 2);

    expect(api.getReplySuggestion).not.toHaveBeenCalled();
  });

  it("waits for the running -> idling transition: no request while busy or running", async () => {
    const { rerender } = renderHook((props: Props) => useReplySuggestion(props), {
      initialProps: { ...idle, busy: true, status: "running" },
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS * 2);
    expect(api.getReplySuggestion).not.toHaveBeenCalled();

    rerender(idle);
    await elapse(REPLY_SUGGESTION_DELAY_MS);
    expect(api.getReplySuggestion).toHaveBeenCalledTimes(1);
  });

  it("aborts the request and clears the suggestion when a new turn starts", async () => {
    const { result, rerender } = renderHook((props: Props) => useReplySuggestion(props), {
      initialProps: idle,
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS);

    rerender({ ...idle, busy: true });
    expect(pending[0].signal.aborted).toBe(true);

    // A late answer for the aborted request must not show.
    await settle({ suggestions: ["stale"], reason: null });
    rerender(idle);
    expect(result.current).toBeNull();
  });

  it("clears a shown suggestion as soon as a new turn starts", async () => {
    const { result, rerender } = renderHook((props: Props) => useReplySuggestion(props), {
      initialProps: idle,
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS);
    await settle({ suggestions: ["Yes"], reason: null });
    expect(result.current).toEqual(["Yes"]);

    rerender({ ...idle, busy: true });
    expect(result.current).toBeNull();
  });

  it("clears it when the user's own message becomes the last item", async () => {
    const { result, rerender } = renderHook((props: Props) => useReplySuggestion(props), {
      initialProps: idle,
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS);
    await settle({ suggestions: ["Yes"], reason: null });

    rerender({ ...idle, items: [...AGENT_SAYS, item("inbound_chat", "3.0", { source: "user" })] });
    expect(result.current).toBeNull();
  });

  it("aborts when the selected agent changes", async () => {
    const { rerender } = renderHook((props: Props) => useReplySuggestion(props), {
      initialProps: idle,
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS);

    rerender({ ...idle, agentId: 8, items: [item("agent_chat", "9.0")] });

    expect(pending[0].signal.aborted).toBe(true);
    await elapse(REPLY_SUGGESTION_DELAY_MS);
    expect(pending[1].agentId).toBe(8);
  });

  it("aborts on unmount", async () => {
    const { unmount } = renderHook((props: Props) => useReplySuggestion(props), {
      initialProps: idle,
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS);

    unmount();

    expect(pending[0].signal.aborted).toBe(true);
  });

  it("aborts when the page is hidden and asks again when it is visible", async () => {
    renderHook((props: Props) => useReplySuggestion(props), { initialProps: idle });
    await elapse(REPLY_SUGGESTION_DELAY_MS);

    setVisibility("hidden");
    expect(pending[0].signal.aborted).toBe(true);

    setVisibility("visible");
    await elapse(REPLY_SUGGESTION_DELAY_MS);
    expect(pending).toHaveLength(2);
  });

  it("sends nothing while the page is hidden", async () => {
    visibility = "hidden";
    renderHook((props: Props) => useReplySuggestion(props), { initialProps: idle });

    await elapse(REPLY_SUGGESTION_DELAY_MS * 3);

    expect(api.getReplySuggestion).not.toHaveBeenCalled();
  });

  it("sends nothing when the setting is off", async () => {
    renderHook((props: Props) => useReplySuggestion(props), {
      initialProps: { ...idle, enabled: false },
    });

    await elapse(REPLY_SUGGESTION_DELAY_MS * 3);

    expect(api.getReplySuggestion).not.toHaveBeenCalled();
  });

  it("aborts when the setting is turned off mid-request", async () => {
    const { rerender } = renderHook((props: Props) => useReplySuggestion(props), {
      initialProps: idle,
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS);

    rerender({ ...idle, enabled: false });

    expect(pending[0].signal.aborted).toBe(true);
  });

  it.each([
    ["the agent is terminated", { status: "terminated" as const }],
    ["there is no agent", { agentId: null }],
    ["the last item is not the agent's message", { items: [item("inbound_chat", "1.0", { source: "user" })] }],
    ["the agent's message is still streaming", { items: [item("agent_chat", "2.0", { partial: true })] }],
    ["the agent ended on code output", { items: [...AGENT_SAYS, item("code_output", "3.0")] }],
    ["the timeline is empty", { items: [] }],
  ])("sends nothing when %s", async (_name, override) => {
    renderHook((props: Props) => useReplySuggestion(props), {
      initialProps: { ...idle, ...override },
    });

    await elapse(REPLY_SUGGESTION_DELAY_MS * 3);

    expect(api.getReplySuggestion).not.toHaveBeenCalled();
  });

  it("looks through trailing framework markers to the agent's message", async () => {
    renderHook((props: Props) => useReplySuggestion(props), {
      initialProps: { ...idle, items: [...AGENT_SAYS, item("system_marker", "3.0")] },
    });

    await elapse(REPLY_SUGGESTION_DELAY_MS);

    expect(api.getReplySuggestion).toHaveBeenCalledTimes(1);
  });

  it("requests each turn once, however many times the conditions flicker", async () => {
    const { rerender } = renderHook((props: Props) => useReplySuggestion(props), {
      initialProps: idle,
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS);
    await settle({ suggestions: [], reason: "not_useful" });

    rerender({ ...idle, busy: true });
    rerender(idle);
    await elapse(REPLY_SUGGESTION_DELAY_MS * 3);

    expect(api.getReplySuggestion).toHaveBeenCalledTimes(1);
  });

  it("does not request a turn again after a failure", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const { rerender } = renderHook((props: Props) => useReplySuggestion(props), {
      initialProps: idle,
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS);
    await act(async () => {
      pending[0].reject(new Error("HTTP 503"));
      await Promise.resolve();
    });
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("HTTP 503"));

    rerender({ ...idle, busy: true });
    rerender(idle);
    await elapse(REPLY_SUGGESTION_DELAY_MS * 2);

    expect(api.getReplySuggestion).toHaveBeenCalledTimes(1);
  });

  it("requests the next turn when the agent speaks again", async () => {
    const { rerender } = renderHook((props: Props) => useReplySuggestion(props), {
      initialProps: idle,
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS);
    await settle({ suggestions: ["first"], reason: null });

    rerender({ ...idle, busy: true });
    rerender({
      ...idle,
      items: [...AGENT_SAYS, item("inbound_chat", "3.0", { source: "user" }), item("agent_chat", "4.0")],
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS);

    expect(api.getReplySuggestion).toHaveBeenCalledTimes(2);
  });

  it("returns nothing for an empty suggestion", async () => {
    const { result } = renderHook((props: Props) => useReplySuggestion(props), {
      initialProps: idle,
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS);

    await settle({ suggestions: [], reason: "not_useful" });

    expect(result.current).toBeNull();
  });
});
