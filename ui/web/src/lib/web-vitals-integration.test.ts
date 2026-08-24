import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  __telemetryFlushForTest,
  __telemetryResetForTest,
  track,
} from "./telemetry";
import { __webVitalsResetForTest, initWebVitals } from "./web-vitals";

interface MockObserver {
  callback: PerformanceObserverCallback;
  disconnect: ReturnType<typeof vi.fn>;
  observe: ReturnType<typeof vi.fn>;
  takeRecords: ReturnType<typeof vi.fn<() => PerformanceEntry[]>>;
}

interface BeaconBody {
  events: { element: string; key?: string; value?: string }[];
}

let observers: MockObserver[];

function installPerformanceObserver(): void {
  class MockPerformanceObserver {
    static supportedEntryTypes = [
      "paint",
      "largest-contentful-paint",
      "layout-shift",
      "event",
    ];
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

function emit(type: string, entries: PerformanceEntry[]): void {
  const observer = observers.find((candidate) => {
    const options = candidate.observe.mock.calls[0]?.[0] as PerformanceObserverInit | undefined;
    return options?.type === type;
  });
  if (!observer) throw new Error(`No observer registered for ${type}`);
  const list = {
    getEntries: () => entries,
    getEntriesByName: () => [],
    getEntriesByType: () => entries,
  } as unknown as PerformanceObserverEntryList;
  observer.callback(list, observer as unknown as PerformanceObserver);
}

async function beaconBody(call: unknown[]): Promise<BeaconBody> {
  return JSON.parse(await (call[1] as Blob).text()) as BeaconBody;
}

beforeEach(() => {
  observers = [];
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "visible",
  });
  installPerformanceObserver();
  __telemetryResetForTest();
  __webVitalsResetForTest();
});

afterEach(() => {
  __webVitalsResetForTest();
  __telemetryResetForTest();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Web Vitals telemetry lifecycle", () => {
  it("flushes final Vitals even when telemetry registered its hidden listener first", async () => {
    const sendBeacon = vi.fn(() => true);
    Object.defineProperty(navigator, "sendBeacon", {
      configurable: true,
      value: sendBeacon,
    });
    track("api-timing", { key: "status", value: 900, dedupe: false });
    initWebVitals();
    emit("largest-contentful-paint", [{
      name: "",
      entryType: "largest-contentful-paint",
      startTime: 1234.6,
      duration: 0,
      toJSON: () => ({}),
    }]);
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    });

    document.dispatchEvent(new Event("visibilitychange"));

    expect(sendBeacon).toHaveBeenCalledTimes(2);
    const finalBatch = await beaconBody(sendBeacon.mock.calls[1]);
    expect(finalBatch.events).toEqual(expect.arrayContaining([
      expect.objectContaining({ element: "web-vitals", key: "lcp", value: "1235" }),
      expect.objectContaining({ element: "web-vitals", key: "cls", value: "0" }),
    ]));
    expect(__telemetryFlushForTest()).toBe(0);
  });
});
