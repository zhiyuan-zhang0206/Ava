import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { API_BASE } from "./api";
import {
  __telemetryBufferSize,
  __telemetryFlushForTest,
  __telemetryResetForTest,
  __telemetrySessionId,
  __telemetrySetEnabledForTest,
  normalizePage,
  setTelemetryPage,
  track,
} from "./telemetry";

// The telemetry module is browser-only: every test runs against happy-dom's
// window. State is module-global, so each test resets it (and the timers)
// first; sendBeacon is stubbed so flushes are observable.
//
// Note: the module starts enabled because `typeof window !== "undefined"`
// under happy-dom — the SSR no-op path is exercised via the disable flag.

function stubBeacon(impl?: (url: string, data?: BodyInit) => boolean) {
  const sendBeacon = vi.fn(impl ?? (() => true));
  Object.defineProperty(navigator, "sendBeacon", {
    configurable: true,
    value: sendBeacon,
  });
  return sendBeacon;
}

/** One tracked interaction as serialized on the wire. */
interface BeaconEvent {
  page: string;
  element: string;
  key?: string;
  value?: string;
  ts: number;
}

interface BeaconBody {
  session_id: string;
  events: BeaconEvent[];
}

/** Parse the JSON payload of the first beacon call. */
async function beaconBody(sendBeacon: ReturnType<typeof vi.fn>): Promise<BeaconBody> {
  const blob = sendBeacon.mock.calls[0][1] as Blob;
  return JSON.parse(await blob.text()) as BeaconBody;
}

beforeEach(() => {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "visible",
  });
  __telemetryResetForTest();
});

afterEach(() => {
  __telemetryResetForTest();
  vi.restoreAllMocks();
});

describe("normalizePage", () => {
  it("maps the root route to home", () => {
    expect(normalizePage("/")).toBe("home");
  });
  it("keeps nested routes", () => {
    expect(normalizePage("/control/config")).toBe("control/config");
    expect(normalizePage("/insights/metrics")).toBe("insights/metrics");
  });
  it("collapses agent ids in shell routes", () => {
    expect(normalizePage("/shell/42")).toBe("shell");
    expect(normalizePage("/shell/42/")).toBe("shell");
  });
  it("strips leading slashes", () => {
    expect(normalizePage("/fleet")).toBe("fleet");
  });
});

describe("track + flush", () => {
  it("buffers then flushes one batch with the session id", async () => {
    const sendBeacon = stubBeacon();
    track("spawn");
    expect(__telemetryBufferSize()).toBe(1);
    expect(__telemetryFlushForTest()).toBe(1);
    expect(sendBeacon).toHaveBeenCalledTimes(1);
    const [url, blob] = sendBeacon.mock.calls[0] as [string, Blob];
    expect(url).toBe(`${API_BASE}/api/frontend-telemetry`);
    expect(blob.type).toBe("application/json");
    const body = await beaconBody(sendBeacon);
    expect(body.session_id).toMatch(/^[0-9a-f-]{8,64}$/);
    expect(body.events).toEqual([
      { page: "unknown", element: "spawn", ts: expect.any(Number) as number },
    ]);
  });

  it("inherits the current page from setTelemetryPage", async () => {
    const sendBeacon = stubBeacon();
    setTelemetryPage("fleet");
    track("spawn");
    expect(__telemetryFlushForTest()).toBe(1);
    const body = await beaconBody(sendBeacon);
    expect(body.events[0].page).toBe("fleet");
  });

  it("falls back to fetch+keepalive when sendBeacon is unavailable", async () => {
    stubBeacon().mockImplementation(() => false);
    const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<Response>>(
      () => Promise.resolve(new Response(null, { status: 204 })),
    );
    vi.stubGlobal("fetch", fetchMock);
    track("page-view");
    __telemetryFlushForTest();
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${API_BASE}/api/frontend-telemetry`);
    expect(init?.method).toBe("POST");
    expect(init?.keepalive).toBe(true);
    expect(init?.credentials).toBe("include");
    vi.unstubAllGlobals();
  });

  it("drops nothing when flush finds an empty buffer", () => {
    const sendBeacon = stubBeacon();
    expect(__telemetryFlushForTest()).toBe(0);
    expect(sendBeacon).not.toHaveBeenCalled();
  });

  it("flushes buffered metrics when the document becomes hidden", () => {
    const sendBeacon = stubBeacon();
    track("web-vitals", { key: "lcp", value: 900, dedupe: false });
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    });

    document.dispatchEvent(new Event("visibilitychange"));

    expect(sendBeacon).toHaveBeenCalledTimes(1);
  });
});

describe("dedupe", () => {
  it("suppresses the same (page, element, key) within 2 s", () => {
    stubBeacon();
    setTelemetryPage("control/config");
    track("setting-change", { key: "display.show_machine_name", value: false });
    track("setting-change", { key: "display.show_machine_name", value: true });
    track("setting-change", { key: "display.show_machine_name", value: false });
    expect(__telemetryBufferSize()).toBe(1);
  });

  it("does not dedupe different keys or elements", () => {
    stubBeacon();
    setTelemetryPage("control/config");
    track("setting-change", { key: "display.show_machine_name", value: false });
    track("setting-change", { key: "display.language", value: "zh" });
    track("spawn");
    expect(__telemetryBufferSize()).toBe(3);
  });

  it("tracks again after the dedupe window", () => {
    vi.useFakeTimers();
    try {
      stubBeacon();
      track("spawn");
      vi.advanceTimersByTime(2500);
      track("spawn");
      expect(__telemetryBufferSize()).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps repeated samples when dedupe is disabled", () => {
    stubBeacon();
    track("web-vitals", { key: "lcp", value: 900, dedupe: false });
    track("web-vitals", { key: "lcp", value: 950, dedupe: false });
    expect(__telemetryBufferSize()).toBe(2);
  });
});

describe("rate limit", () => {
  it("caps pending + sent at 100 events per minute", () => {
    stubBeacon();
    for (let i = 0; i < 105; i++) track("page-view", { page: `p${i}` });
    expect(__telemetryBufferSize()).toBe(100);
  });

  it("resets the budget after the window elapses", () => {
    vi.useFakeTimers();
    try {
      stubBeacon();
      for (let i = 0; i < 100; i++) track("page-view", { page: `p${i}` });
      // flush sends them — budget spent
      __telemetryFlushForTest();
      track("page-view", { page: "after" });
      expect(__telemetryBufferSize()).toBe(0);
      vi.advanceTimersByTime(61_000);
      track("page-view", { page: "later" });
      expect(__telemetryBufferSize()).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("still caps events when dedupe is disabled", () => {
    stubBeacon();
    for (let i = 0; i < 105; i++) {
      track("web-vitals", { key: "lcp", value: i, dedupe: false });
    }
    expect(__telemetryBufferSize()).toBe(100);
  });
});

describe("instrumentation vocabulary", () => {
  it("accepts performance telemetry elements", async () => {
    const sendBeacon = stubBeacon();
    track("api-timing", { key: "status", value: 801, dedupe: false });
    track("composer-latency", { key: "send-to-turn-start", value: 120 });
    track("web-vitals", { key: "fcp", value: 450, dedupe: false });

    expect(__telemetryFlushForTest()).toBe(3);
    const body = await beaconBody(sendBeacon);
    expect(body.events.map((event) => event.element)).toEqual([
      "api-timing",
      "composer-latency",
      "web-vitals",
    ]);
  });
});

describe("value sanitization", () => {
  it("serializes booleans and numbers", async () => {
    const sendBeacon = stubBeacon();
    setTelemetryPage("control/config");
    track("setting-change", { key: "display.show_machine_name", value: false });
    track("setting-change", { key: "display.timeline_width_ratio", value: 0.4 });
    expect(__telemetryFlushForTest()).toBe(2);
    const body = await beaconBody(sendBeacon);
    const byKey = Object.fromEntries(
      body.events
        .filter((e): e is BeaconEvent & { key: string; value: string } => e.key !== undefined)
        .map((e) => [e.key, e.value]),
    );
    expect(byKey["display.show_machine_name"]).toBe("false");
    expect(byKey["display.timeline_width_ratio"]).toBe("0.4");
  });

  it("truncates long strings to 64 chars", async () => {
    const sendBeacon = stubBeacon();
    setTelemetryPage("control/config");
    track("setting-change", { key: "display.custom", value: "x".repeat(200) });
    expect(__telemetryFlushForTest()).toBe(1);
    const body = await beaconBody(sendBeacon);
    expect(body.events[0].value).toBe("x".repeat(64));
  });

  it("omits the value for non-scalar payloads", async () => {
    const sendBeacon = stubBeacon();
    setTelemetryPage("control/config");
    track("setting-change", { key: "display.sidebar_sort", value: { key: "id" } });
    expect(__telemetryFlushForTest()).toBe(1);
    const body = await beaconBody(sendBeacon);
    expect("value" in body.events[0]).toBe(false);
  });
});

describe("disabled / SSR", () => {
  it("is a no-op when disabled (SSR-safe path)", () => {
    const sendBeacon = stubBeacon();
    __telemetrySetEnabledForTest(false);
    track("spawn");
    expect(__telemetryBufferSize()).toBe(0);
    expect(__telemetryFlushForTest()).toBe(0);
    expect(sendBeacon).not.toHaveBeenCalled();
  });

  it("mints a stable session id per tab", () => {
    const a = __telemetrySessionId();
    const b = __telemetrySessionId();
    expect(a).toBe(b);
    expect(a).toMatch(/^[0-9a-f-]{8,64}$/);
  });
});
