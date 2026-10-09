import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";

vi.mock("../telemetry/telemetry", () => ({ track: vi.fn() }));

const drafts = [
  { name: "Guide", path: "/api/keyed/v1/guide/draft", body: { nl: "operate the cluster" },
    submit: (key?: string) => api.draftGuide("operate the cluster", key) },
  { name: "Schedule Maker", path: "/api/keyed/v1/schedules/draft", body: { nl: "run nightly" },
    submit: (key?: string) => api.draftSchedule("run nightly", key) },
  { name: "Package Installer", path: "/api/keyed/v1/packages/draft", body: { kind: "mcp", nl: "install tools" },
    submit: (key?: string) => api.draftPackage("mcp", "install tools", key) },
];

function accepted(body: unknown = { agent_id: 42 }, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  fetchMock.mockReset().mockImplementation(() => Promise.resolve(accepted()));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe.each(drafts)("$name guarded draft", ({ path, body, submit }) => {
  it("submits the immutable intent and scoped key with the session credential", async () => {
    expect(await submit("intent-1")).toEqual({ agent_id: 42 });
    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain(path);
    expect(init?.method).toBe("POST");
    expect(init?.credentials).toBe("include");
    expect(JSON.parse(init?.body as string)).toEqual(body);
    const headers = new Headers(init?.headers);
    expect(headers.get("Idempotency-Key")).toBe("intent-1");
    expect(headers.get("Idempotency-Scope")).toBe("principal-v1");
    expect(headers.get("content-type")).toBe("application/json");
  });

  it("preserves one explicit key and request after a lost response", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("response lost"));
    await expect(submit("intent-1")).rejects.toThrow("response lost");
    expect(fetchMock).toHaveBeenCalledOnce();
    await submit("intent-1");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const [first, second] = fetchMock.mock.calls;
    expect(second).toEqual(first);
  });

  it("uses distinct keys for deliberately new submissions", async () => {
    await submit();
    await submit();
    const keys = fetchMock.mock.calls.map(([, init]) => new Headers(init?.headers).get("Idempotency-Key"));
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).not.toBe(keys[0]);
  });

  it.each([404, 409, 500])("surfaces HTTP %i without a retry or legacy fallback", async (status) => {
    fetchMock.mockResolvedValue(accepted({ detail: "refused" }, status));
    await expect(submit("intent-1")).rejects.toThrow("refused");
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toContain(path);
  });

  it.each([null, {}, { agent_id: "42" }, { agent_id: 0 }, { agent_id: -1 }, { agent_id: 1.5 }])(
    "rejects an unconfirmed receipt %j", async (receipt) => {
      fetchMock.mockResolvedValue(accepted(receipt));
      await expect(submit("intent-1")).rejects.toThrow("acceptance is unconfirmed");
      expect(fetchMock).toHaveBeenCalledOnce();
    },
  );

  it("rejects an unexpected successful status without resending", async () => {
    fetchMock.mockResolvedValue(accepted({ agent_id: 42 }, 201));
    await expect(submit("intent-1")).rejects.toThrow("acceptance is unconfirmed");
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it.each(["", "x".repeat(129)])("rejects invalid keys before HTTP", async (key) => {
    await expect(submit(key)).rejects.toThrow("idempotency key");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
