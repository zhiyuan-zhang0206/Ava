// useBreakpoint — R4 layer 4's single breakpoint source.
//
// The pure tier math (tierForWidth) is tested exhaustively at every boundary.
// The hook itself is composed from useMediaQuery (mocked here): isNarrow = the
// md query NOT matching, isLarge = the lg query matching, isWide = the 1280px
// query matching, and the initial (pre-mount / SSR) state is the narrow/mobile layout.

import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  BREAKPOINT_LG_PX,
  BREAKPOINT_MD_PX,
  BREAKPOINT_SM_PX,
  BREAKPOINT_WIDE_PX,
  BREAKPOINT_XS_PX,
  tierForWidth,
  useBreakpoint,
} from "./breakpoint";

const { mdMatches, lgMatches, wideMatches } = vi.hoisted(() => ({
  mdMatches: vi.fn<() => boolean>(() => false),
  lgMatches: vi.fn<() => boolean>(() => false),
  wideMatches: vi.fn<() => boolean>(() => false),
}));

vi.mock("./use-media-query", () => ({
  useMediaQuery: (query: string) => {
    if (query === `(min-width: ${BREAKPOINT_MD_PX}px)`) return mdMatches();
    if (query === `(min-width: ${BREAKPOINT_LG_PX}px)`) return lgMatches();
    if (query === `(min-width: ${BREAKPOINT_WIDE_PX}px)`) return wideMatches();
    throw new Error(`Unexpected media query: ${query}`);
  },
}));

describe("tierForWidth — 320/390/768/lg boundaries", () => {
  it("maps every tier band to its name", () => {
    expect(tierForWidth(BREAKPOINT_XS_PX - 1)).toBe("xs");
    expect(tierForWidth(BREAKPOINT_XS_PX)).toBe("sm");
    expect(tierForWidth(BREAKPOINT_SM_PX - 1)).toBe("sm");
    expect(tierForWidth(BREAKPOINT_SM_PX)).toBe("md");
    expect(tierForWidth(BREAKPOINT_MD_PX - 1)).toBe("md");
    expect(tierForWidth(BREAKPOINT_MD_PX)).toBe("lg");
    expect(tierForWidth(BREAKPOINT_LG_PX - 1)).toBe("lg");
    expect(tierForWidth(BREAKPOINT_LG_PX)).toBe("xl");
    expect(tierForWidth(1920)).toBe("xl");
  });
});

describe("useBreakpoint", () => {
  it("defaults to narrow/mobile before mount (SSR-safe)", () => {
    const { result } = renderHook(() => useBreakpoint());
    expect(result.current.isNarrow).toBe(true);
    expect(result.current.isLarge).toBe(false);
    expect(result.current.isWide).toBe(false);
  });

  it("isNarrow = NOT matching the md query; isLarge = matching the lg query", () => {
    mdMatches.mockReturnValue(true);
    lgMatches.mockReturnValue(false);
    const { result, rerender } = renderHook(() => useBreakpoint());
    rerender();
    expect(result.current.isNarrow).toBe(false);
    expect(result.current.isLarge).toBe(false);

    lgMatches.mockReturnValue(true);
    rerender();
    expect(result.current.isNarrow).toBe(false);
    expect(result.current.isLarge).toBe(true);

    mdMatches.mockReturnValue(false);
    lgMatches.mockReturnValue(false);
    rerender();
    expect(result.current.isNarrow).toBe(true);
    expect(result.current.isLarge).toBe(false);
  });

  it("isWide follows the 1280px query without changing the xl tier", () => {
    mdMatches.mockReturnValue(true);
    lgMatches.mockReturnValue(true);
    wideMatches.mockReturnValue(false);
    const { result, rerender } = renderHook(() => useBreakpoint());
    expect(result.current.isLarge).toBe(true);
    expect(result.current.isWide).toBe(false);

    wideMatches.mockReturnValue(true);
    rerender();
    expect(result.current.isWide).toBe(true);
    expect(tierForWidth(BREAKPOINT_WIDE_PX - 1)).toBe("xl");
    expect(tierForWidth(BREAKPOINT_WIDE_PX)).toBe("xl");

    mdMatches.mockReturnValue(false);
    lgMatches.mockReturnValue(false);
    wideMatches.mockReturnValue(false);
  });

  it("tier follows the real viewport width after mount", () => {
    const width = 390;
    const orig = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { value: width, writable: true });
    try {
      const { result } = renderHook(() => useBreakpoint());
      expect(result.current.tier).toBe("md");
      expect(result.current.isNarrow).toBe(true); // 390 < 768
      expect(result.current.isLarge).toBe(false);
    } finally {
      Object.defineProperty(window, "innerWidth", { value: orig, writable: true });
    }
  });
});
