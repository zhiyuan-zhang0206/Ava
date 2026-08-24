import { expect, it, vi } from "vitest";

import { reloadThroughGate } from "./gate-maintenance";

it("coalesces interleaved SSE and poll reload hints for one page lifetime", () => {
  const reload = vi.fn();

  reloadThroughGate(reload); // SSE
  reloadThroughGate(reload); // persisted-state poll races it

  expect(reload).toHaveBeenCalledTimes(1);
});
