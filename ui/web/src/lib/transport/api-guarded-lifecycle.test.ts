import { afterEach, expect, it, vi } from "vitest";

import type { CompactTarget, NativeWorkTarget } from "../contracts/types";
import { ApiError, api } from "./api";

const work: NativeWorkTarget = {
  protocol: 1, agent_id: 8, work_id: "work", machine: "local", generation: "generation", owner: "owner",
};
const history: CompactTarget = {
  protocol: 1, observation_id: "observation", source: work, checkpoint_id: "checkpoint",
  checkpoint_ns: "", messages_version: "1", compact_channel_version: null,
  segment_version: 0, model: "gpt-5.6-sol",
};

afterEach(() => vi.unstubAllGlobals());

it.each([
  ["cancel", "/cancel-work", work, () => api.cancel(work, "fixed-intent")],
  ["compact", "/compact-history", history, () => api.compact(history, "fixed-intent")],
  ["launch retry", "/retry-launch", { expected_prior_attempt_id: "prior-attempt" },
    () => api.retryAgentLaunch(8, "prior-attempt", "fixed-intent")],
] as const)("keeps the exact %s intent after a lost response", async (_name, suffix, body, submit) => {
  const fetch = vi.fn().mockRejectedValueOnce(new TypeError("response lost"))
    .mockResolvedValue(new Response("{}", { status: 200 }));
  vi.stubGlobal("fetch", fetch);
  await expect(submit()).rejects.toThrow("response lost");
  // Recovery is explicit: transport never observes a newer target or submits again.
  expect(fetch).toHaveBeenCalledTimes(1);
  await submit();
  expect(fetch).toHaveBeenCalledTimes(2);
  const [url, first] = fetch.mock.calls[0] as [string, RequestInit];
  const [replayUrl, replay] = fetch.mock.calls[1] as [string, RequestInit];
  expect(url).toMatch(new RegExp(`/api/keyed/v1/agents/8${suffix}$`));
  expect(replayUrl).toBe(url);
  expect(first.method).toBe("POST");
  expect(JSON.parse(first.body as string)).toEqual(body);
  expect(replay.body).toBe(first.body);
  expect(new Headers(replay.headers).get("Idempotency-Key")).toBe("fixed-intent");
  expect(new Headers(replay.headers).get("Idempotency-Scope")).toBe("principal-v1");
});

it("preserves a stale target refusal without observing or using retired ingress", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "stale target" }), { status: 409 }));
  vi.stubGlobal("fetch", fetch);
  await expect(api.cancel(work, "fixed-intent")).rejects.toBeInstanceOf(ApiError);
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[0][0]).toMatch(/\/cancel-work$/);
});

it.each(["", "x".repeat(129)])("rejects invalid operation keys before sending", (key) => {
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  expect(() => api.cancel(work, key)).toThrow("idempotency key");
  expect(() => api.compact(history, key)).toThrow("idempotency key");
  expect(() => api.retryAgentLaunch(8, "prior-attempt", key)).toThrow("idempotency key");
  expect(fetch).not.toHaveBeenCalled();
});
