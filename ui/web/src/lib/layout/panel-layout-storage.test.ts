import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

  it("survives a storage that throws on read or write", () => {
    const boom = () => {
      throw new Error("storage unavailable");
    };
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(boom);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(boom);
    try {
      const storage = panelLayoutStorage();
      expect(storage.getItem(KEY)).toBeNull();
      expect(() => storage.setItem(KEY, "{}")).not.toThrow();
    } finally {
      vi.restoreAllMocks();
    }
  });
});
