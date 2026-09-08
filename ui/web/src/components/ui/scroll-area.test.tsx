// ScrollArea — viewportClassName plumbing. The timeline relies on this to set
// `overflow-anchor: none` on the inner scroll viewport (the element that
// actually scrolls), so the prop must land on the viewport, not the root.

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ScrollArea } from "./scroll-area";

const defaultResizeObserver = globalThis.ResizeObserver;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
  globalThis.ResizeObserver = defaultResizeObserver;
});

function renderScrollArea() {
  render(
    <ScrollArea>
      <div>content</div>
    </ScrollArea>,
  );
  const root = document.querySelector('[data-slot="scroll-area"]');
  expect(root).toBeInstanceOf(HTMLElement);
  return root as HTMLElement;
}

function getScrollbar() {
  const scrollbar = document.querySelector('[data-slot="scroll-area-scrollbar"]');
  expect(scrollbar).toBeInstanceOf(HTMLElement);
  return scrollbar as HTMLElement;
}

function mockScrollableGeometry() {
  const observations: {
    callback: ResizeObserverCallback;
    observer: ResizeObserver;
    target: Element;
  }[] = [];

  class ControlledResizeObserver implements ResizeObserver {
    constructor(private readonly callback: ResizeObserverCallback) {}
    disconnect() {
      for (let index = observations.length - 1; index >= 0; index -= 1) {
        if (observations[index].observer === this) observations.splice(index, 1);
      }
    }
    observe(target: Element) {
      observations.push({ callback: this.callback, observer: this, target });
    }
    unobserve(target: Element) {
      const index = observations.findIndex(
        (observation) => observation.observer === this && observation.target === target,
      );
      if (index >= 0) observations.splice(index, 1);
    }
  }

  globalThis.ResizeObserver = ControlledResizeObserver;
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(100);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(100);
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(200);

  return () => {
    act(() => {
      for (const { callback, observer, target } of observations) {
        callback([{ target } as ResizeObserverEntry], observer);
      }
      vi.advanceTimersByTime(30);
    });
  };
}

describe("ScrollArea", () => {
  it("applies viewportClassName to the inner scroll viewport", () => {
    render(
      <ScrollArea viewportClassName="[overflow-anchor:none]">
        <div data-testid="child">content</div>
      </ScrollArea>,
    );
    const viewport = document.querySelector('[data-slot="scroll-area-viewport"]');
    expect(viewport).not.toBeNull();
    expect(viewport?.className).toContain("overflow-anchor");
    // The base viewport classes are preserved alongside it.
    expect(viewport?.className).toContain("size-full");
    // And the content still renders.
    expect(screen.getByTestId("child")).toBeTruthy();
  });

  it("root carries overflow-hidden (required by Radix for correct scroll behavior)", () => {
    render(
      <ScrollArea>
        <div>content</div>
      </ScrollArea>,
    );
    const root = document.querySelector('[data-slot="scroll-area"]');
    expect(root).not.toBeNull();
    expect(root?.className).toContain("overflow-hidden");
  });

  it("omitting viewportClassName leaves the viewport with only its base classes", () => {
    render(
      <ScrollArea>
        <div>content</div>
      </ScrollArea>,
    );
    const viewport = document.querySelector('[data-slot="scroll-area-viewport"]');
    expect(viewport?.className).toContain("size-full");
    expect(viewport?.className).not.toContain("overflow-anchor");
  });

  it("shows the scrollbar while the root is scrolling", () => {
    const root = renderScrollArea();

    fireEvent.scroll(root);

    expect(getScrollbar().getAttribute("data-visible")).toBe("true");
  });

  it("hides the scrollbar after 800 ms without another scroll", () => {
    vi.useFakeTimers();
    const root = renderScrollArea();
    fireEvent.scroll(root);

    act(() => {
      vi.advanceTimersByTime(800);
    });

    expect(getScrollbar().getAttribute("data-visible")).toBe("false");
  });

  it("re-arms the idle timer when another scroll starts", () => {
    vi.useFakeTimers();
    const root = renderScrollArea();
    fireEvent.scroll(root);
    act(() => {
      vi.advanceTimersByTime(800);
    });
    expect(getScrollbar().getAttribute("data-visible")).toBe("false");

    fireEvent.scroll(root);

    expect(getScrollbar().getAttribute("data-visible")).toBe("true");
    act(() => {
      vi.advanceTimersByTime(400);
    });
    fireEvent.scroll(root);
    act(() => {
      vi.advanceTimersByTime(799);
    });
    expect(getScrollbar().getAttribute("data-visible")).toBe("true");
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(getScrollbar().getAttribute("data-visible")).toBe("false");
  });

  it("keeps the scrollbar visible while its thumb has focus", () => {
    vi.useFakeTimers();
    const flushResizeObservers = mockScrollableGeometry();
    renderScrollArea();
    flushResizeObservers();
    const scrollbar = getScrollbar();
    const thumb = document.querySelector('[data-slot="scroll-area-thumb"]');
    expect(thumb).toBeInstanceOf(HTMLElement);

    act(() => (thumb as HTMLElement).focus());

    expect(document.activeElement).toBe(thumb);
    expect(thumb?.getAttribute("tabindex")).toBe("0");
    expect(scrollbar.className).toContain("focus-within:opacity-100");
  });

  it("does not show the scrollbar when the root or scrollbar is hovered", () => {
    const root = renderScrollArea();
    const scrollbar = getScrollbar();

    fireEvent.pointerEnter(root);
    expect(scrollbar.getAttribute("data-visible")).toBe("false");
    fireEvent.pointerEnter(scrollbar);
    expect(scrollbar.getAttribute("data-visible")).toBe("false");
  });

  it("repositions the thumb on EVERY viewport scroll event, mid-gesture", () => {
    // Regression (2026-09-08 user report): the thumb froze for the whole
    // scroll gesture and only jumped at the next discrete event, because the
    // position update depended on Radix's internal rAF polling loop (absent
    // from the deployed bundle). ScrollArea now positions the thumb from the
    // viewport's own scroll events, so a second event inside the same gesture
    // must move the thumb again.
    vi.useFakeTimers();
    const flushResizeObservers = mockScrollableGeometry();
    renderScrollArea();

    const viewport = document.querySelector('[data-slot="scroll-area-viewport"]');
    const track = document.querySelector('[data-slot="scroll-area-scrollbar"]');
    expect(viewport).toBeInstanceOf(HTMLElement);
    expect(track).toBeInstanceOf(HTMLElement);

    // Element-level metrics (set BEFORE the resize flush so Radix's own size
    // measurement agrees with ours): content 400 over a 100px viewport on a
    // 100px track. Radix derives a 25px thumb (100 * 100/400); mirror that in
    // the thumb's own offsetHeight below so every writer computes the same
    // travel (100 - 25 = 75) and the same translate.
    Object.defineProperty(viewport, "scrollHeight", { configurable: true, value: 400 });
    Object.defineProperty(viewport, "clientHeight", { configurable: true, value: 100 });
    Object.defineProperty(viewport, "scrollTop", { configurable: true, writable: true, value: 0 });
    Object.defineProperty(track, "clientHeight", { configurable: true, value: 100 });
    flushResizeObservers();

    const thumb = document.querySelector('[data-slot="scroll-area-thumb"]');
    expect(thumb).toBeInstanceOf(HTMLElement);
    Object.defineProperty(thumb, "offsetHeight", { configurable: true, value: 25 });

    viewport!.scrollTop = 100;
    fireEvent.scroll(viewport as HTMLElement);
    expect((thumb as HTMLElement).style.transform).toBe("translate3d(0, 25px, 0)");

    // A second scroll event in the same gesture (no idle gap) must move the
    // thumb again — the case that froze without the direct scroll listener.
    viewport!.scrollTop = 200;
    fireEvent.scroll(viewport as HTMLElement);
    expect((thumb as HTMLElement).style.transform).toBe("translate3d(0, 50px, 0)");
  });

  it("keeps the scrollbar visible for a pointer drag until the release idle window", () => {
    vi.useFakeTimers();
    renderScrollArea();
    const scrollbar = getScrollbar();

    fireEvent.pointerDown(scrollbar, { button: 0, pointerId: 1 });
    expect(scrollbar.getAttribute("data-visible")).toBe("true");
    act(() => {
      vi.advanceTimersByTime(800);
    });
    expect(scrollbar.getAttribute("data-visible")).toBe("true");

    fireEvent.pointerUp(scrollbar, { button: 0, pointerId: 1 });
    expect(scrollbar.getAttribute("data-visible")).toBe("true");
    act(() => {
      vi.advanceTimersByTime(800);
    });
    expect(scrollbar.getAttribute("data-visible")).toBe("false");
  });
});
