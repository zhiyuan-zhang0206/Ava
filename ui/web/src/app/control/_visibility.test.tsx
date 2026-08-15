// useInView regression test — the fix for the Control page's "collapsed until
// scrolled to" bug: a section's data query must be enabled from first paint,
// not only once an (async) IntersectionObserver callback confirms on-screen
// position. The global test stub (vitest.setup.ts) fires its callback
// synchronously, which would mask this distinction in an integration test —
// so this asserts the hook's return value directly, before any node is ever
// attached / observed.

import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useInView } from "./_visibility";

describe("useInView", () => {
  it("starts visible before any node is attached (no scroll-gated first fetch)", () => {
    const { result } = renderHook(() => useInView());
    const [, inView] = result.current;
    expect(inView).toBe(true);
  });
});
