// The composed conversation reconcile (agent-reconcile.ts): one read per
// re-attach, ordered so a composed write can never land older data after
// newer. Drives the coordinator factory directly — the hook wiring and the
// per-reader integration live in use-timeline.test.ts /
// use-detail-reconnect.test.ts.

import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";
import { createAgentReconcile } from "./agent-reconcile";
import type { ConversationSnapshotResponse, PendingInbound } from "./types";

vi.mock("./api", () => ({
  api: { getConversationSnapshot: vi.fn() },
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function snapshot(tag: string): ConversationSnapshotResponse {
  return {
    timeline: { items: [], msg_count: 0, has_more: false },
    token_usage: { input_tokens: tag === "new" ? 2 : 1, output_tokens: 0, reasoning_tokens: 0,
      max_input_tokens: 100_000, soft_compact_tokens: 70_000, hard_compact_tokens: 90_000 },
    pending: [{ id: tag === "new" ? 2 : 1, content: tag, source: "user", images: null,
      created_at: "2026-09-18T00:00:00Z" }],
  };
}

function pendingRow(id: number): PendingInbound {
  return { id, content: String(id), source: "user", images: null, created_at: "2026-09-18T00:00:00Z" };
}

let client: QueryClient;
let reconcile: ReturnType<typeof createAgentReconcile>;

beforeEach(() => {
  // resetAllMocks, not clearAllMocks: an unconsumed mockResolvedValueOnce
  // would otherwise leak across tests.
  vi.resetAllMocks();
  vi.useFakeTimers();
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  reconcile = createAgentReconcile(client);
});

afterEach(() => {
  vi.useRealTimers();
  client.clear();
});

describe("composed conversation reconcile", () => {
  it("collapses a same-tick reader burst into one read and writes all three keys", async () => {
    vi.mocked(api.getConversationSnapshot).mockResolvedValue(snapshot("fresh"));
    const unsubscribes = [reconcile.subscribe(42), reconcile.subscribe(42), reconcile.subscribe(42)];

    reconcile.request(42);
    reconcile.request(42);
    reconcile.request(42);
    await vi.advanceTimersByTimeAsync(0);

    expect(api.getConversationSnapshot).toHaveBeenCalledTimes(1);
    expect(api.getConversationSnapshot).toHaveBeenCalledWith(42, expect.any(AbortSignal));
    expect(client.getQueryData<PendingInbound[]>(["pending", 42])![0].content).toBe("fresh");
    expect(client.getQueryData(["token-usage", 42])).toMatchObject({ input_tokens: 1 });
    expect(client.getQueryData(["timeline", 42])).toMatchObject({ msg_count: 0 });

    for (const unsubscribe of unsubscribes) unsubscribe();
  });

  it("joins an in-flight read: the composed read starts only after it settles", async () => {
    const inFlight = deferred<string>();
    void client.query({ queryKey: ["timeline", 42], queryFn: () => inFlight.promise });
    reconcile.subscribe(42);
    vi.mocked(api.getConversationSnapshot).mockResolvedValue(snapshot("fresh"));

    reconcile.request(42);
    await vi.advanceTimersByTimeAsync(600); // several join polls, still fetching
    expect(api.getConversationSnapshot).not.toHaveBeenCalled();

    inFlight.resolve("old");
    await vi.advanceTimersByTimeAsync(400);
    expect(api.getConversationSnapshot).toHaveBeenCalledTimes(1);
  });

  it("a second gap during the composed read owes one trailing read", async () => {
    const first = deferred<ConversationSnapshotResponse>();
    vi.mocked(api.getConversationSnapshot)
      .mockImplementationOnce(() => first.promise)
      .mockResolvedValueOnce(snapshot("new"));
    reconcile.subscribe(42);

    reconcile.request(42);
    await vi.advanceTimersByTimeAsync(0);
    expect(api.getConversationSnapshot).toHaveBeenCalledTimes(1);

    reconcile.request(42); // second open while the read is in flight
    first.resolve(snapshot("fresh"));
    await vi.advanceTimersByTimeAsync(400);

    expect(api.getConversationSnapshot).toHaveBeenCalledTimes(2);
    expect(client.getQueryData<PendingInbound[]>(["pending", 42])![0].content).toBe("new");
  });

  it("drops a snapshot superseded by a newer write and re-reads once", async () => {
    const first = deferred<ConversationSnapshotResponse>();
    vi.mocked(api.getConversationSnapshot)
      .mockImplementationOnce(() => first.promise)
      .mockResolvedValueOnce(snapshot("new"));
    reconcile.subscribe(42);
    client.setQueryData(["pending", 42], [pendingRow(1)]);

    reconcile.request(42);
    await vi.advanceTimersByTimeAsync(0);
    // A newer snapshot lands while the composed read is in flight.
    client.setQueryData(["pending", 42], [pendingRow(1), pendingRow(2)]);
    first.resolve(snapshot("fresh"));
    await vi.advanceTimersByTimeAsync(400);

    // The stale composed payload never replaced the newer write; the trailing
    // read then delivered the newest server state.
    expect(api.getConversationSnapshot).toHaveBeenCalledTimes(2);
    const pending = client.getQueryData<PendingInbound[]>(["pending", 42])!;
    expect(pending.map((row) => row.id)).toEqual([2]);
  });

  it("falls back to invalidating the three keys when the composed read fails", async () => {
    const invalidate = vi.spyOn(client, "invalidateQueries");
    vi.mocked(api.getConversationSnapshot).mockRejectedValue(new Error("gateway down"));
    reconcile.subscribe(42);

    reconcile.request(42);
    await vi.advanceTimersByTimeAsync(0);

    for (const domain of ["timeline", "token-usage", "pending"]) {
      expect(invalidate).toHaveBeenCalledWith({ queryKey: [domain, 42] }, { cancelRefetch: false });
    }
  });

  it("detaching the last reader aborts the read and blocks a late write", async () => {
    let signal: AbortSignal | undefined;
    const first = deferred<ConversationSnapshotResponse>();
    vi.mocked(api.getConversationSnapshot).mockImplementation((_id, readSignal) => {
      signal = readSignal;
      return first.promise;
    });
    const unsubscribe = reconcile.subscribe(42);
    reconcile.request(42);
    await vi.advanceTimersByTimeAsync(0);

    unsubscribe();
    expect(signal?.aborted).toBe(true);
    first.resolve(snapshot("fresh"));
    await vi.advanceTimersByTimeAsync(400);
    expect(client.getQueryData(["timeline", 42])).toBeUndefined();
    expect(client.getQueryData(["pending", 42])).toBeUndefined();
  });

  it("requests without a live reader are no-ops", async () => {
    reconcile.request(42);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(api.getConversationSnapshot).not.toHaveBeenCalled();
  });
});
