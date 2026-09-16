import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createQueryRepairScheduler } from "./repair";

afterEach(() => { vi.useRealTimers(); });

describe("read-model repair", () => {
  it("repairs a second short disconnect instead of leaving an infinite-stale cache", async () => {
    vi.useFakeTimers();
    const client = new QueryClient();
    let serverValue = "first";
    const observer = new QueryObserver(client, {
      queryKey: ["roster"], queryFn: () => Promise.resolve(serverValue),
      initialData: "old", staleTime: Infinity,
    });
    const unsubscribe = observer.subscribe(() => undefined);
    const repairs = createQueryRepairScheduler(client);
    repairs.request(["roster"], true);
    await vi.advanceTimersByTimeAsync(0);
    expect(client.getQueryData(["roster"])).toBe("first");
    serverValue = "changed in a second gap";
    repairs.request(["roster"], true);
    await vi.advanceTimersByTimeAsync(0);
    expect(client.getQueryData(["roster"])).toBe(serverValue);
    repairs.dispose(); unsubscribe(); client.clear();
  });

  it("repairs hints during a pending snapshot without cancelling or overlapping it", async () => {
    vi.useFakeTimers();
    const client = new QueryClient();
    let resolveRead!: () => void;
    let serverValue = "first";
    let inFlight = 0;
    let maxInFlight = 0;
    const read = vi.fn(() => {
      const snapshot = serverValue;
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      return new Promise<string>((resolve) => {
        resolveRead = () => { inFlight -= 1; resolve(snapshot); };
      });
    });
    const observer = new QueryObserver(client, {
      queryKey: ["roster"], queryFn: read, initialData: "old", staleTime: Infinity,
    });
    const unsubscribe = observer.subscribe(() => undefined);
    const repairs = createQueryRepairScheduler(client);
    repairs.request(["roster"], true);
    serverValue = "last";
    for (let n = 0; n < 100; n += 1) repairs.request(["roster"], true);
    expect(read).toHaveBeenCalledTimes(1);
    resolveRead();
    await vi.advanceTimersByTimeAsync(200);
    expect(read).toHaveBeenCalledTimes(2);
    resolveRead();
    await vi.advanceTimersByTimeAsync(0);
    expect(client.getQueryData(["roster"])).toBe("last");
    expect(maxInFlight).toBe(1);
    repairs.dispose(); unsubscribe(); client.clear();
  });

  it("a hint during an independently-started initial fetch still forces a newer snapshot", async () => {
    vi.useFakeTimers();
    const client = new QueryClient();
    let serverValue = "before hint";
    let resolveRead!: () => void;
    const read = vi.fn(() => {
      const snapshot = serverValue;
      return new Promise<string>((resolve) => { resolveRead = () => resolve(snapshot); });
    });
    const observer = new QueryObserver(client, { queryKey: ["roster"], queryFn: read });
    const unsubscribe = observer.subscribe(() => undefined);
    serverValue = "after hint";
    const repairs = createQueryRepairScheduler(client);
    repairs.request(["roster"], true);
    expect(read).toHaveBeenCalledTimes(1);
    resolveRead();
    await vi.advanceTimersByTimeAsync(200);
    expect(read).toHaveBeenCalledTimes(2);
    resolveRead();
    await vi.advanceTimersByTimeAsync(0);
    expect(client.getQueryData(["roster"])).toBe("after hint");
    repairs.dispose(); unsubscribe(); client.clear();
  });

  it("does not postpone deadlines under continuous hints or conflate agent keys", async () => {
    vi.useFakeTimers();
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const repairs = createQueryRepairScheduler(client);
    for (let n = 0; n < 10; n += 1) {
      repairs.request(["agent-detail", 1]);
      repairs.request(["agent-detail", 2]);
      await vi.advanceTimersByTimeAsync(20);
    }
    expect(invalidate).toHaveBeenCalledTimes(2);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["agent-detail", 1] }, { cancelRefetch: false });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["agent-detail", 2] }, { cancelRefetch: false });
    repairs.dispose(); client.clear();
  });

  it("disposal removes scheduled repairs", async () => {
    vi.useFakeTimers();
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const repairs = createQueryRepairScheduler(client);
    repairs.request(["roster"]);
    repairs.dispose();
    await vi.advanceTimersByTimeAsync(500);
    expect(invalidate).not.toHaveBeenCalled();
    client.clear();
  });
});
