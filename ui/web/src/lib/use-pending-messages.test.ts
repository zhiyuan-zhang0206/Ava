import { describe, expect, it } from "vitest";

import type { PendingInbound } from "./types";
import { withoutTimelineDuplicates } from "./use-pending-messages";

function pending(over: Partial<PendingInbound> & { id: number }): PendingInbound {
  return {
    source: "user",
    content: "hello",
    images: null,
    created_at: "2026-09-16T00:00:00Z",
    ...over,
  };
}

// Task #3683: during an external takeover the capture trigger puts an inbound
// into the timeline at insert time while its row stays status='pending' until
// the executor's ACK — the strip must not show a message the timeline already
// renders.
describe("withoutTimelineDuplicates", () => {
  it("drops a pending inbound already rendered in the timeline", () => {
    const out = withoutTimelineDuplicates(
      [pending({ id: 206838, content: "captured by the takeover" }), pending({ id: 206900, content: "still queued" })],
      [{ inbound_id: 206838 }],
    );
    expect(out.map((p) => p.id)).toEqual([206900]);
  });

  it("keeps everything when no timeline item carries an inbound id", () => {
    const out = withoutTimelineDuplicates([pending({ id: 7 })], [{ inbound_id: null }, { inbound_id: null }]);
    expect(out.map((p) => p.id)).toEqual([7]);
  });

  it("handles empty inputs on either side", () => {
    expect(withoutTimelineDuplicates([], [{ inbound_id: 7 }])).toEqual([]);
    expect(withoutTimelineDuplicates([pending({ id: 7 })], []).map((p) => p.id)).toEqual([7]);
  });

  it("preserves the oldest-first order of the remaining queue", () => {
    const out = withoutTimelineDuplicates(
      [pending({ id: 1 }), pending({ id: 2 }), pending({ id: 3 })],
      [{ inbound_id: 2 }],
    );
    expect(out.map((p) => p.id)).toEqual([1, 3]);
  });
});
