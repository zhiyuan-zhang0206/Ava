// The fleet page's route-id parsing — the deep links an inspector widget
// button produces (task #2909). Malformed values must be inert: a bad link
// routes to the default view, never to a wrong row.

import { describe, expect, it } from "vitest";

import { readFleetRouteIds } from "./fleet-route";

function search(qs: string): URLSearchParams {
  return new URLSearchParams(qs);
}

describe("readFleetRouteIds", () => {
  it("parses all three ids", () => {
    expect(readFleetRouteIds(search("agent_id=3&notice=7&task=9"))).toEqual({
      agentId: 3,
      noticeId: 7,
      taskId: 9,
    });
  });

  it("absent params are null", () => {
    expect(readFleetRouteIds(search(""))).toEqual({ agentId: null, noticeId: null, taskId: null });
  });

  it("malformed and non-positive values are null, never 0 or NaN", () => {
    expect(readFleetRouteIds(search("notice=abc&task=-2"))).toEqual({
      agentId: null,
      noticeId: null,
      taskId: null,
    });
    expect(readFleetRouteIds(search("notice=0&task=")).taskId).toBeNull();
  });
});
