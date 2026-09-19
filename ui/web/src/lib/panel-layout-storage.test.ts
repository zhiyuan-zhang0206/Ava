import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { panelLayoutStorage } from "./panel-layout-storage";

const KEY = "react-resizable-panels:test";

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  localStorage.clear();
});

describe("panelLayoutStorage", () => {
  it("reads and writes plain v4 layout values", () => {
    const storage = panelLayoutStorage();

    expect(storage.getItem(KEY)).toBeNull();

    const value = JSON.stringify({ "panel-sidebar": 30, "panel-main": 70 });
    storage.setItem(KEY, value);
    expect(storage.getItem(KEY)).toBe(value);
    expect(localStorage.getItem(KEY)).toBe(value);
  });

  it("returns a legacy v3 record as-is — no conversion; the library falls back to defaults", () => {
    const legacy = JSON.stringify({
      '{"minSize":20,"maxSize":50},{"minSize":45}': {
        expandToSizes: { "panel-main": 59 },
        layout: [41, 59],
      },
    });
    localStorage.setItem(KEY, legacy);

    expect(panelLayoutStorage().getItem(KEY)).toBe(legacy);
  });
});
