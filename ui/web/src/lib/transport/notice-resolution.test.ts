import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";

vi.mock("../telemetry/telemetry", () => ({ track: vi.fn() }));

type Fetch = (url: string, init: RequestInit) => Promise<Response>;

afterEach(() => vi.unstubAllGlobals());

describe("guarded browser notice resolution", () => {
  it("uses the fixed path, exact global row and explicit principal-scoped key", async () => {
    const fetch = vi.fn<Fetch>().mockResolvedValue(new Response(JSON.stringify({ status: "idling", inbound_id: 3 }), { status: 201 }));
    vi.stubGlobal("fetch", fetch);
    await api.resolveNotice(7, 11, { action: "answer", reply: "yes" }, "intent");
    const [url, init] = fetch.mock.calls[0];
    expect(url).toMatch(/\/api\/keyed\/v1\/agents\/7\/notices\/11\/resolve$/);
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("Idempotency-Key")).toBe("intent");
    expect(new Headers(init.headers).get("Idempotency-Scope")).toBe("principal-v1");
    if (typeof init.body !== "string") throw new Error("expected a JSON request body");
    expect(JSON.parse(init.body)).toEqual({ action: "answer", reply: "yes" });
  });

  it.each([404, 409, 500])("rejects HTTP %i without fallback or an automatic retry", async (status) => {
    const fetch = vi.fn<Fetch>().mockResolvedValue(new Response(JSON.stringify({ detail: "refused" }), { status }));
    vi.stubGlobal("fetch", fetch);
    await expect(api.resolveNotice(7, 11, { action: "read" }, "intent")).rejects.toMatchObject({ status });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("preserves the same key/body/path when a caller explicitly retries a lost response", async () => {
    const fetch = vi.fn<Fetch>()
      .mockRejectedValueOnce(new TypeError("response lost"))
      .mockResolvedValueOnce(new Response(JSON.stringify({ status: "idling", inbound_id: 3 }), { status: 201 }));
    vi.stubGlobal("fetch", fetch);
    const body = { action: "read" as const, reply: "yes" };
    await expect(api.resolveNotice(7, 11, body, "intent")).rejects.toThrow("response lost");
    expect(fetch).toHaveBeenCalledTimes(1);
    await api.resolveNotice(7, 11, body, "intent");
    expect(fetch.mock.calls[1]).toEqual(fetch.mock.calls[0]);
  });

  it("allocates a different key for separate default-key invocations", async () => {
    const fetch = vi.fn<Fetch>().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ status: "idling" }), { status: 201 })));
    vi.stubGlobal("fetch", fetch);
    await api.resolveNotice(7, 11, { action: "read" });
    await api.resolveNotice(7, 11, { action: "read" });
    const keys = fetch.mock.calls.map(([, init]) => new Headers(init.headers).get("Idempotency-Key"));
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).not.toBe(keys[0]);
  });
});
