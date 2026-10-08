import { describe, expect, it } from "vitest";

import { parseClusterSelection, selectionQuery, toLocalInput } from "./cluster-selection";

const NOW = new Date("2026-10-04T12:00:00Z");

describe("cluster selection in the URL", () => {
  it("round-trips a root and a window", () => {
    const selection = { root: 405, from: "2026-10-03T12:00:00.000Z", to: "2026-10-04T12:00:00.000Z" };
    expect(parseClusterSelection(selectionQuery(selection), NOW).selection).toEqual(selection);
  });

  it("selects nothing unless the root and both ends are valid and ordered", () => {
    const window = "from=2026-10-03T00:00:00Z&to=2026-10-04T00:00:00Z";
    expect(parseClusterSelection(`?${window}`, NOW).selection).toBeNull();
    expect(parseClusterSelection(`?root=0&${window}`, NOW).selection).toBeNull();
    expect(parseClusterSelection(`?root=4x&${window}`, NOW).selection).toBeNull();
    expect(parseClusterSelection("?root=4&from=2026-10-03T00:00:00Z", NOW).selection).toBeNull();
    expect(parseClusterSelection("?root=4&from=2026-10-04T00:00:00Z&to=2026-10-03T00:00:00Z", NOW).selection).toBeNull();
    expect(parseClusterSelection("?root=4&from=nope&to=2026-10-03T00:00:00Z", NOW).selection).toBeNull();
  });

  it("prefills the last 24 hours when the URL names no window", () => {
    expect(parseClusterSelection("", NOW).form).toEqual({
      from: "2026-10-03T12:00:00.000Z",
      to: "2026-10-04T12:00:00.000Z",
    });
  });

  it("formats an instant for a datetime-local input in local time", () => {
    const local = new Date(2026, 9, 4, 8, 5);
    expect(toLocalInput(local.toISOString())).toBe("2026-10-04T08:05");
  });
});
