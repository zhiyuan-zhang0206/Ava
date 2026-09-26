import { expect, it, vi } from "vitest";

import { reloadThroughGate } from "./gate-maintenance";

it("coalesces racing reload hints for one page lifetime", () => {
  const reload = vi.fn();

  reloadThroughGate(reload); // first poll tick
  reloadThroughGate(reload); // a second tick races it

  expect(reload).toHaveBeenCalledTimes(1);
});
