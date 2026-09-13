// row.tsx — the per-item card-config cache. Keyed on the item reference AND
// the resolved color map: unchanged colors keep returning the same config
// object (memoized rows skip re-render), while a color-map change must
// recompute — otherwise a palette edit would repaint nothing.

import { describe, expect, it } from "vitest";

import { resolveTimelineColors } from "@/lib/timeline-colors";
import type { BackendTimelineItem } from "@/lib/types";

import { cardConfigFor } from "./row";

function item(): BackendTimelineItem {
  return {
    item_id: "1.0",
    kind: "agent_code",
    source: null,
    payload: "",
    created_at: "2026-05-15T12:00:00Z",
    inbound_id: null,
    show_timestamp: true,
  };
}

describe("cardConfigFor", () => {
  it("same item + same colors → the cached config reference", () => {
    const row = item();
    const colors = resolveTimelineColors({});
    const first = cardConfigFor(row, colors);
    const second = cardConfigFor(row, colors);
    expect(second).toBe(first);
  });

  it("changed colors → recomputed config (a palette edit must repaint)", () => {
    const row = item();
    const before = cardConfigFor(row, resolveTimelineColors({}));
    const after = cardConfigFor(
      row,
      resolveTimelineColors({ "display.color.agent_code": "pink" }),
    );
    expect(after).not.toBe(before);
    expect(after!.border).toBe("border-pink-500/70");
  });
});
