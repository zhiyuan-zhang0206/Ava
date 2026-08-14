// API wrapper unit tests — only verify URL + method + body shape.
// Anchors the frontend↔gateway endpoint contract: changing endpoint
// path / HTTP method makes the tests go red.
//
// Doesn't verify happy-path response shape — backend OpenAPI schema
// carries the types; the frontend `ok<T>` only checks res.ok at
// runtime; type safety comes from tsc.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";

interface FetchCall {
  url: string;
  init: RequestInit | undefined;
}

let calls: FetchCall[];

beforeEach(() => {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      calls.push({ url, init });
      // GET /api/agents returns an array by contract (listAgents maps over it);
      // every other stubbed endpoint here only has its URL/method asserted, so a
      // bare object suffices.
      const isListAgents = !init?.method && url.endsWith("/api/agents");
      return Promise.resolve(
        new Response(JSON.stringify(isListAgents ? [] : {}), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("lifecycle endpoints", () => {
  it("listAgents GETs /api/agents", async () => {
    await api.listAgents();
    expect(calls).toHaveLength(1);
    // After happy-dom provides window, API_BASE is inferred as http://localhost:8000;
    // tests only anchor the endpoint path (gateway contract); host part is ignored.
    expect(calls[0].url).toMatch(/\/api\/agents$/);
    // GET goes through the f() wrapper by default (no method, no body).
    expect(calls[0].init?.method).toBeUndefined();
  });

  it("spawnAgent POSTs JSON body to /api/agents", async () => {
    await api.spawnAgent({ prompt: "Search X", spawner: "claude-code", prompt_source: "user" });
    // After happy-dom provides window, API_BASE is inferred as http://localhost:8000;
    // tests only anchor the endpoint path (gateway contract); host part is ignored.
    expect(calls[0].url).toMatch(/\/api\/agents$/);
    expect(calls[0].init?.method).toBe("POST");
    expect(calls[0].init?.headers).toEqual({ "content-type": "application/json" });
    expect(JSON.parse(calls[0].init?.body as string)).toEqual({
      prompt: "Search X",
      spawner: "claude-code",
      prompt_source: "user",
    });
  });

  it("spawnAgent with no args POSTs empty body", async () => {
    await api.spawnAgent();
    expect(JSON.parse(calls[0].init?.body as string)).toEqual({});
  });

  it("terminateAgent POSTs /api/agents/{id}/terminate", async () => {
    await api.terminateAgent(42);
    expect(calls[0].url).toMatch(/\/api\/agents\/42\/terminate$/);
    expect(calls[0].init?.method).toBe("POST");
    // graceful path sends no body — backend defaults force=false
    expect(calls[0].init?.body).toBeUndefined();
  });

  it("terminateAgent(id, true) POSTs JSON {force:true} for force kill", async () => {
    await api.terminateAgent(42, true);
    expect(calls[0].url).toMatch(/\/api\/agents\/42\/terminate$/);
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(calls[0].init?.body as string)).toEqual({ force: true });
  });

  it("restartAgent POSTs /api/agents/{id}/restart", async () => {
    await api.restartAgent(99);
    expect(calls[0].url).toMatch(/\/api\/agents\/99\/restart$/);
    expect(calls[0].init?.method).toBe("POST");
  });

  it("resurrectAgent POSTs JSON {prompt} to /api/agents/{id}/resurrect", async () => {
    await api.resurrectAgent(7, "wake up and resume");
    expect(calls[0].url).toMatch(/\/api\/agents\/7\/resurrect$/);
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(calls[0].init?.body as string)).toEqual({
      prompt: "wake up and resume",
    });
  });
});

describe("agent label / messages / cancel", () => {
  it("patchAgentLabel PATCHes /api/agents/{id} with label body", async () => {
    await api.patchAgentLabel(5, "New Name");
    expect(calls[0].url).toMatch(/\/api\/agents\/5$/);
    expect(calls[0].init?.method).toBe("PATCH");
    expect(JSON.parse(calls[0].init?.body as string)).toEqual({ label: "New Name" });
  });

  it("patchAgentLabel throws on non-ok response", async () => {
    vi.stubGlobal("fetch", vi.fn(() =>
      Promise.resolve(new Response("nope", { status: 500, statusText: "ISE" })),
    ));
    await expect(api.patchAgentLabel(5, "x")).rejects.toThrow(/HTTP 500/);
  });

  it("sendMessage POSTs JSON {content, source} to /api/agents/{id}/messages", async () => {
    await api.sendMessage(3, "hello");
    expect(calls[0].url).toMatch(/\/api\/agents\/3\/messages$/);
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(calls[0].init?.body as string)).toEqual({
      content: "hello",
      source: "user",
    });
  });

  it("cancel POSTs JSON {agent_id} to /api/cancel (global endpoint)", async () => {
    await api.cancel(8);
    expect(calls[0].url).toMatch(/\/api\/cancel$/);
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(calls[0].init?.body as string)).toEqual({ agent_id: 8 });
  });

  it("compact POSTs /api/agents/{id}/compact?mode=framework", async () => {
    await api.compact(5, "framework");
    expect(calls[0].url).toMatch(/\/api\/agents\/5\/compact\?mode=framework$/);
    expect(calls[0].init?.method).toBe("POST");
  });
});

describe("stats / config / timeline / system status", () => {
  it("getStatsDashboard GETs /api/stats/dashboard with the hours window", async () => {
    await api.getStatsDashboard(24);
    expect(calls[0].url).toMatch(/\/api\/stats\/dashboard\?hours=24$/);
  });

  it("getStatsDashboard passes a non-default hours window through", async () => {
    await api.getStatsDashboard(168);
    expect(calls[0].url).toMatch(/\/api\/stats\/dashboard\?hours=168$/);
  });

  it("getConfig GETs /api/config", async () => {
    await api.getConfig();
    expect(calls[0].url).toMatch(/\/api\/config$/);
  });

  it("getConfig appends ?machine= when a machine is given", async () => {
    await api.getConfig("runner-1");
    expect(calls[0].url).toMatch(/\/api\/config\?machine=runner-1$/);
  });

  it("getResolvedConfig GETs /api/config/resolved with the model", async () => {
    await api.getResolvedConfig("claude-opus-5");
    expect(calls[0].url).toMatch(/\/api\/config\/resolved\?model=claude-opus-5$/);
  });

  it("getResolvedConfig omits ?model= so the gateway resolves its own", async () => {
    await api.getResolvedConfig();
    expect(calls[0].url).toMatch(/\/api\/config\/resolved$/);
  });

  it("getMachines GETs /api/cluster/machines", async () => {
    await api.getMachines();
    expect(calls[0].url).toMatch(/\/api\/cluster\/machines$/);
  });

  it("putConfig PUTs JSON body to /api/config and parses the result", async () => {
    vi.stubGlobal("fetch", vi.fn((_url: string, init?: RequestInit) => {
      calls.push({ url: _url, init });
      return Promise.resolve(
        new Response(
          JSON.stringify({ applied: true, results: {}, restart_required: ["all"] }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    }));
    const result = await api.putConfig({ log_level: "DEBUG" });
    expect(calls[0].init?.method).toBe("PUT");
    expect(calls[0].url).toMatch(/\/api\/config$/);
    expect(JSON.parse(calls[0].init?.body as string)).toEqual({ log_level: "DEBUG" });
    expect(result.applied).toBe(true);
    expect(result.restart_required).toEqual(["all"]);
  });

  it("putConfig appends ?machine= when targeting a host", async () => {
    await api.putConfig({ host_label: "rack-Z" }, "runner-1");
    expect(calls[0].init?.method).toBe("PUT");
    expect(calls[0].url).toMatch(/\/api\/config\?machine=runner-1$/);
  });

  it("getTimeline GETs /api/agents/{id}/timeline with no params by default", async () => {
    await api.getTimeline(22);
    expect(calls[0].url).toMatch(/\/api\/agents\/22\/timeline$/);
  });

  it("getTimeline forwards limit + before query params", async () => {
    await api.getTimeline(22, { limit: 100, before: "12.0" });
    expect(calls[0].url).toMatch(/\/api\/agents\/22\/timeline\?limit=100&before=12\.0$/);
  });

  it("getSystemStatus GETs /api/status", async () => {
    await api.getSystemStatus();
    expect(calls[0].url).toMatch(/\/api\/status$/);
  });

  it("getTokenUsage GETs /api/agents/{id}/token-usage", async () => {
    await api.getTokenUsage(33);
    expect(calls[0].url).toMatch(/\/api\/agents\/33\/token-usage$/);
  });

  it("getMemoryGraph GETs /api/memory/graph", async () => {
    await api.getMemoryGraph();
    expect(calls[0].url).toMatch(/\/api\/memory\/graph$/);
  });
});


describe("uploadFiles", () => {
  interface XhrInstance {
    open: ReturnType<typeof vi.fn>;
    send: ReturnType<typeof vi.fn>;
    upload: { onprogress: ((e: ProgressEvent) => void) | null };
    onload: (() => void) | null;
    onerror: (() => void) | null;
    status: number;
    responseText: string;
    withCredentials: boolean;
  }

  let inst: XhrInstance;

  function makeXhr(): XhrInstance {
    return {
      open: vi.fn(),
      send: vi.fn(),
      upload: { onprogress: null },
      onload: null,
      onerror: null,
      status: 200,
      responseText: "",
      withCredentials: false,
    };
  }

  beforeEach(() => {
    // regular function → constructable with `new`. assign last so caller code
    // path inside Promise executor sees the populated stub when it sets
    // .upload.onprogress / .onload etc.
    function FakeXHR(this: XhrInstance) {
      Object.assign(this, makeXhr());
      // eslint-disable-next-line @typescript-eslint/no-this-alias -- must capture into closure for subsequent inst.* operations
      inst = this;
    }
    vi.stubGlobal("XMLHttpRequest", FakeXHR);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("POSTs one file as FormData to /api/agents/{id}/uploads", async () => {
    const file = new File(["hello"], "test.txt", { type: "text/plain" });
    const promise = api.uploadFiles(42, [file]);

    expect(inst.open).toHaveBeenCalledWith(
      "POST",
      expect.stringMatching(/\/api\/agents\/42\/uploads\?deliver=true$/),
    );

    expect(inst.send).toHaveBeenCalledTimes(1);
    const fd = inst.send.mock.calls[0][0] as FormData;
    expect(fd).toBeInstanceOf(FormData);
    const sent = fd.getAll("files") as File[];
    expect(sent).toHaveLength(1);
    expect(sent[0].name).toBe("test.txt");

    inst.responseText = JSON.stringify({
      files: [{ filename: "test.txt", path: "/tmp/test.txt", size: 5, content_type: "text/plain" }],
    });
    inst.onload?.();

    const result = await promise;
    expect(result).toEqual({
      files: [{ filename: "test.txt", path: "/tmp/test.txt", size: 5, content_type: "text/plain" }],
    });
  });

  it("appends every file under the 'files' field for a batch", () => {
    const f1 = new File(["a"], "a.txt", { type: "text/plain" });
    const f2 = new File(["bb"], "b.txt", { type: "text/plain" });
    const f3 = new File(["ccc"], "c.txt", { type: "text/plain" });
    void api.uploadFiles(7, [f1, f2, f3]);

    const fd = inst.send.mock.calls[0][0] as FormData;
    const sent = fd.getAll("files") as File[];
    expect(sent.map((f) => f.name)).toEqual(["a.txt", "b.txt", "c.txt"]);
  });

  it("calls onProgress when lengthComputable", () => {
    const onProgress = vi.fn();
    void api.uploadFiles(1, [new File([], "x")], onProgress);
    inst.upload.onprogress?.({ lengthComputable: true, loaded: 500, total: 1000 } as ProgressEvent);
    expect(onProgress).toHaveBeenCalledWith(50);
  });

  it("skips onProgress when not lengthComputable", () => {
    const onProgress = vi.fn();
    void api.uploadFiles(1, [new File([], "x")], onProgress);
    inst.upload.onprogress?.({ lengthComputable: false, loaded: 500, total: 1000 } as ProgressEvent);
    expect(onProgress).not.toHaveBeenCalled();
  });

  it("rejects on HTTP 404 with detail", async () => {
    const promise = api.uploadFiles(1, [new File([], "x")]);
    inst.status = 404;
    inst.responseText = JSON.stringify({ detail: "Agent not found" });
    inst.onload?.();
    await expect(promise).rejects.toThrow("Agent not found");
  });

  it("rejects with HTTP status when no detail", async () => {
    const promise = api.uploadFiles(1, [new File([], "x")]);
    inst.status = 500;
    inst.responseText = "boom";
    inst.onload?.();
    await expect(promise).rejects.toThrow("HTTP 500");
  });

  it("rejects on network error", async () => {
    const promise = api.uploadFiles(1, [new File([], "x")]);
    inst.onerror?.();
    await expect(promise).rejects.toThrow("Upload failed");
  });

  it("rejects on invalid JSON error body", async () => {
    const promise = api.uploadFiles(1, [new File([], "x")]);
    inst.status = 400;
    inst.responseText = "not json";
    inst.onload?.();
    await expect(promise).rejects.toThrow("HTTP 400");
  });
});
