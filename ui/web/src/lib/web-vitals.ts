import { flushTelemetry, track } from "./telemetry";

interface LayoutShiftEntry extends PerformanceEntry {
  value: number;
  hadRecentInput: boolean;
}

interface InteractionTimingEntry extends PerformanceEntry {
  interactionId: number;
}

let initialized = false;
let finalized = false;
interface MetricObserver {
  observer: PerformanceObserver;
  consume: (entries: PerformanceEntry[], observer: PerformanceObserver) => void;
}

let observers: MetricObserver[] = [];
let fcpReported = false;
let latestLcp: number | undefined;
let cumulativeLayoutShift = 0;
let collectingLayoutShift = false;
let interactionToNextPaint: number | undefined;

function reportMetric(key: "fcp" | "lcp" | "cls" | "inp", value: number | string): void {
  track("web-vitals", { key, value, dedupe: false });
}

function observe(
  type: string,
  consume: MetricObserver["consume"],
  options: { durationThreshold?: number } = {},
): PerformanceObserver | undefined {
  if (!PerformanceObserver.supportedEntryTypes.includes(type)) return undefined;
  const observer = new PerformanceObserver((list, currentObserver) => {
    consume(list.getEntries(), currentObserver);
  });
  try {
    observer.observe({ type, buffered: true, ...options });
  } catch {
    observer.disconnect();
    return undefined;
  }
  observers.push({ observer, consume });
  return observer;
}

function stopObservers(): void {
  for (const { observer } of observers) observer.disconnect();
}

function drainObservers(): void {
  for (const { observer, consume } of observers) {
    const pending = observer.takeRecords();
    if (pending.length > 0) consume(pending, observer);
  }
}

function detachFinalizers(): void {
  if (typeof window === "undefined") return;
  window.removeEventListener("pagehide", reportFinalMetrics);
  document.removeEventListener("visibilitychange", reportWhenHidden);
}

function reportFinalMetrics(): void {
  if (finalized) return;
  finalized = true;
  drainObservers();
  if (latestLcp !== undefined) reportMetric("lcp", Math.round(latestLcp));
  if (collectingLayoutShift) {
    const stableScore = Math.round(cumulativeLayoutShift * 10_000) / 10_000;
    reportMetric("cls", String(stableScore));
  }
  if (interactionToNextPaint !== undefined) {
    reportMetric("inp", Math.round(interactionToNextPaint));
  }
  stopObservers();
  detachFinalizers();
  flushTelemetry();
}

function reportWhenHidden(): void {
  if (document.visibilityState === "hidden") reportFinalMetrics();
}

/** Start one-shot native Web Vitals observers for the current page load. */
export function initWebVitals(): void {
  if (
    initialized ||
    typeof window === "undefined" ||
    typeof PerformanceObserver === "undefined"
  ) {
    return;
  }
  initialized = true;

  observe("paint", (entries, observer) => {
    if (fcpReported) return;
    const fcp = entries.find((candidate) => candidate.name === "first-contentful-paint");
    if (fcp === undefined) return;
    fcpReported = true;
    reportMetric("fcp", Math.round(fcp.startTime));
    observer.disconnect();
  });

  observe("largest-contentful-paint", (entries) => {
    for (const candidate of entries) latestLcp = candidate.startTime;
  });

  collectingLayoutShift = observe("layout-shift", (entries) => {
    for (const candidate of entries as LayoutShiftEntry[]) {
      if (!candidate.hadRecentInput) cumulativeLayoutShift += candidate.value;
    }
  }) !== undefined;

  observe("event", (entries) => {
    for (const candidate of entries as InteractionTimingEntry[]) {
      if (typeof candidate.interactionId !== "number" || candidate.interactionId <= 0) continue;
      interactionToNextPaint = Math.max(interactionToNextPaint ?? 0, candidate.duration);
    }
  }, { durationThreshold: 40 });

  window.addEventListener("pagehide", reportFinalMetrics);
  document.addEventListener("visibilitychange", reportWhenHidden);
}

/** Reset module state — tests only. */
export function __webVitalsResetForTest(): void {
  stopObservers();
  detachFinalizers();
  initialized = false;
  finalized = false;
  observers = [];
  fcpReported = false;
  latestLcp = undefined;
  cumulativeLayoutShift = 0;
  collectingLayoutShift = false;
  interactionToNextPaint = undefined;
}
