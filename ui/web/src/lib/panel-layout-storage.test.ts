import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { normalizeStoredPanelLayout, panelLayoutStorage } from "./panel-layout-storage";

const KEY = "react-resizable-panels:test";
const PAIR = ["panel-sidebar", "panel-main"] as const;

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  localStorage.clear();
});

describe("normalizeStoredPanelLayout", () => {
  it("maps a single legacy shape's layout array onto the current panel ids", () => {
    const legacy = JSON.stringify({
      '{"minSize":20,"maxSize":50},{"minSize":45}': {
        expandToSizes: { "panel-main": 59 },
        layout: [41, 59],
      },
    });

    expect(normalizeStoredPanelLayout(legacy, PAIR)).toBe(
      JSON.stringify({ "panel-sidebar": 41, "panel-main": 59 }),
    );
  });

  it("picks the legacy shape matching the rendered panel count", () => {
    const legacy = JSON.stringify({
      closed: { expandToSizes: {}, layout: [100] },
      open: { expandToSizes: {}, layout: [68, 32] },
    });

    expect(normalizeStoredPanelLayout(legacy, ["panel-timeline"])).toBe(
      JSON.stringify({ "panel-timeline": 100 }),
    );
    expect(normalizeStoredPanelLayout(legacy, ["panel-timeline", "panel-inspector"])).toBe(
      JSON.stringify({ "panel-timeline": 68, "panel-inspector": 32 }),
    );
  });

  it("passes an already-v4 layout through untouched", () => {
    const modern = JSON.stringify({ "panel-sidebar": 41, "panel-main": 59 });
    expect(normalizeStoredPanelLayout(modern, PAIR)).toBe(modern);
  });

  it("leaves an ambiguous legacy record to the library", () => {
    const ambiguous = JSON.stringify({
      a: { expandToSizes: {}, layout: [40, 60] },
      b: { expandToSizes: {}, layout: [30, 70] },
    });
    expect(normalizeStoredPanelLayout(ambiguous, PAIR)).toBe(ambiguous);
  });

  it("answers null for values the library could not parse", () => {
    expect(normalizeStoredPanelLayout("not json", PAIR)).toBeNull();
    expect(normalizeStoredPanelLayout('"just a string"', PAIR)).toBeNull();
    expect(normalizeStoredPanelLayout("[1,2]", PAIR)).toBeNull();
  });
});

describe("panelLayoutStorage", () => {
  it("reads through the bridge and writes plain values", () => {
    const storage = panelLayoutStorage(PAIR);
    localStorage.setItem(KEY, JSON.stringify({ shape: { expandToSizes: {}, layout: [25, 75] } }));

    expect(storage.getItem(KEY)).toBe(
      JSON.stringify({ "panel-sidebar": 25, "panel-main": 75 }),
    );
    expect(storage.getItem("react-resizable-panels:missing")).toBeNull();

    storage.setItem(KEY, JSON.stringify({ "panel-sidebar": 30, "panel-main": 70 }));
    expect(localStorage.getItem(KEY)).toBe(
      JSON.stringify({ "panel-sidebar": 30, "panel-main": 70 }),
    );
  });
});
