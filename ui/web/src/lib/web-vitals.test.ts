import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { flushTelemetry, track } from "./telemetry";
import { __webVitalsResetForTest, initWebVitals } from "./web-vitals";

vi.mock("./telemetry", () => ({ flushTelemetry: vi.fn(), track: vi.fn() }));

interface MockObserver {
  callback: PerformanceObserverCallback;
  disconnect: ReturnType<typeof vi.fn>;
  observe: ReturnType<typeof vi.fn>;
  takeRecords: ReturnType<typeof vi.fn<() => PerformanceEntry[]>>;
}

let observers: MockObserver[];

function installPerformanceObserver(supportedEntryTypes: string[]) {
  class MockPerformanceObserver {
    static supportedEntryTypes = supportedEntryTypes;
    callback: PerformanceObserverCallback;
    disconnect = vi.fn();
    observe = vi.fn();
    takeRecords = vi.fn<() => PerformanceEntry[]>(() => []);

    constructor(callback: PerformanceObserverCallback) {
      this.callback = callback;
      observers.push(this);
    }
  }

  vi.stubGlobal("PerformanceObserver", MockPerformanceObserver);
}

function observerFor(type: string): MockObserver {
  const observer = observers.find((candidate) => {
    const options = candidate.observe.mock.calls[0]?.[0] as PerformanceObserverInit | undefined;
    return options?.type === type;
  });
  if (!observer) throw new Error(`No observer registered for ${type}`);
  return observer;
}

function emit(type: string, entries: PerformanceEntry[]): void {
  const observer = observerFor(type);
  const list = {
    getEntries: () => entries,
    getEntriesByName: () => [],
    getEntriesByType: () => entries,
  } as unknown as PerformanceObserverEntryList;
  observer.callback(list, observer as unknown as PerformanceObserver);
}

function queue(type: string, entries: PerformanceEntry[]): void {
  observerFor(type).takeRecords.mockReturnValueOnce(entries);
}

function entry(
  name: string,
  entryType: string,
  startTime: number,
  extra: Record<string, unknown> = {},
): PerformanceEntry {
  return {
    name,
    entryType,
    startTime,
    duration: 0,
    toJSON: () => ({}),
    ...extra,
  };
}

beforeEach(() => {
  observers = [];
  vi.mocked(track).mockReset();
  vi.mocked(flushTelemetry).mockReset();
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "visible",
  });
  installPerformanceObserver([
    "paint",
    "largest-contentful-paint",
    "layout-shift",
    "event",
  ]);
  __webVitalsResetForTest();
});

afterEach(() => {
  __webVitalsResetForTest();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("initWebVitals", () => {
  it("reports FCP when the first-contentful-paint entry arrives", () => {
    initWebVitals();
    emit("paint", [entry("first-paint", "paint", 20), entry("first-contentful-paint", "paint", 456.6)]);

    expect(track).toHaveBeenCalledWith("web-vitals", {
      key: "fcp",
      value: 457,
      dedupe: false,
    });
    expect(observerFor("paint").disconnect).toHaveBeenCalledTimes(1);
  });

  it("reports the final LCP, cumulative CLS, and available INP when the page hides", () => {
    initWebVitals();
    emit("largest-contentful-paint", [entry("", "largest-contentful-paint", 1800.2)]);
    emit("largest-contentful-paint", [entry("", "largest-contentful-paint", 2400.7)]);
    emit("layout-shift", [
      entry("", "layout-shift", 10, { value: 0.03, hadRecentInput: false }),
      entry("", "layout-shift", 20, { value: 0.8, hadRecentInput: true }),
      entry("", "layout-shift", 30, { value: 0.02, hadRecentInput: false }),
    ]);
    emit("event", [
      entry("pointerdown", "event", 40, { duration: 120.2, interactionId: 7 }),
      entry("click", "event", 50, { duration: 310.6, interactionId: 9 }),
      entry("mousemove", "event", 60, { duration: 999, interactionId: 0 }),
      entry("keydown", "event", 70, { duration: 1200 }),
    ]);

    window.dispatchEvent(new Event("pagehide"));

    expect(track).toHaveBeenCalledWith("web-vitals", {
      key: "lcp",
      value: 2401,
      dedupe: false,
    });
    expect(track).toHaveBeenCalledWith("web-vitals", {
      key: "cls",
      value: "0.05",
      dedupe: false,
    });
    expect(track).toHaveBeenCalledWith("web-vitals", {
      key: "inp",
      value: 311,
      dedupe: false,
    });
    for (const observer of observers) {
      expect(observer.disconnect).toHaveBeenCalled();
    }
    expect(flushTelemetry).toHaveBeenCalledTimes(1);
  });

  it("drains queued observer records before reporting final metrics", () => {
    initWebVitals();
    queue("paint", [entry("first-contentful-paint", "paint", 321.4)]);
    queue("largest-contentful-paint", [entry("", "largest-contentful-paint", 1900.6)]);
    queue("layout-shift", [
      entry("", "layout-shift", 10, { value: 0.04, hadRecentInput: false }),
    ]);
    queue("event", [
      entry("click", "event", 20, { duration: 75.4, interactionId: 1 }),
    ]);

    window.dispatchEvent(new Event("pagehide"));

    expect(track).toHaveBeenCalledWith("web-vitals", {
      key: "fcp",
      value: 321,
      dedupe: false,
    });
    expect(track).toHaveBeenCalledWith("web-vitals", {
      key: "lcp",
      value: 1901,
      dedupe: false,
    });
    expect(track).toHaveBeenCalledWith("web-vitals", {
      key: "cls",
      value: "0.04",
      dedupe: false,
    });
    expect(track).toHaveBeenCalledWith("web-vitals", {
      key: "inp",
      value: 75,
      dedupe: false,
    });
  });

  it("observes responsive interactions below the browser's default event threshold", () => {
    initWebVitals();

    expect(observerFor("event").observe).toHaveBeenCalledWith({
      type: "event",
      buffered: true,
      durationThreshold: 40,
    });
    emit("event", [
      entry("click", "event", 20, { duration: 65.4, interactionId: 1 }),
    ]);
    window.dispatchEvent(new Event("pagehide"));

    expect(track).toHaveBeenCalledWith("web-vitals", {
      key: "inp",
      value: 65,
      dedupe: false,
    });
  });

  it("reports final metrics only once across hidden and pagehide", () => {
    initWebVitals();
    emit("largest-contentful-paint", [entry("", "largest-contentful-paint", 900)]);
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    });

    document.dispatchEvent(new Event("visibilitychange"));
    window.dispatchEvent(new Event("pagehide"));

    expect(track).toHaveBeenCalledTimes(2);
    expect(vi.mocked(track).mock.calls.map(([, options]) => options?.key)).toEqual([
      "lcp",
      "cls",
    ]);
  });

  it("does not report INP when no interaction timing exists", () => {
    initWebVitals();
    window.dispatchEvent(new Event("pagehide"));

    expect(track).toHaveBeenCalledWith("web-vitals", {
      key: "cls",
      value: "0",
      dedupe: false,
    });
    expect(track).not.toHaveBeenCalledWith(
      "web-vitals",
      expect.objectContaining({ key: "inp" }),
    );
  });

  it("skips unsupported entry types", () => {
    installPerformanceObserver(["paint"]);
    initWebVitals();

    expect(observers).toHaveLength(1);
    expect(observers[0].observe).toHaveBeenCalledWith({ type: "paint", buffered: true });
  });

  it("is idempotent", () => {
    initWebVitals();
    initWebVitals();

    expect(observers).toHaveLength(4);
  });

  it("is a no-op during SSR", () => {
    __webVitalsResetForTest();
    vi.stubGlobal("window", undefined);

    initWebVitals();

    expect(observers).toHaveLength(0);
  });
});
