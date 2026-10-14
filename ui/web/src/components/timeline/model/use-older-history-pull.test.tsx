import { act, cleanup, render } from "@testing-library/react";
import { useEffect, useLayoutEffect, useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useOlderHistoryPull } from "./use-older-history-pull";

function Harness({ thread = "first", available = true, busy = false, loadPage, intent }: {
  thread?: string; available?: boolean; busy?: boolean; loadPage: () => boolean; intent: () => void;
}) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const spaceRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const previousBusy = useRef(busy);
  const pull = useOlderHistoryPull(thread, spaceRef, contentRef, () => available && !busy, loadPage, intent);
  const { onWheel, onScroll, onScrollEnd, onTouchStart, onTouchMove, onTouchEnd, onTouchCancel, finishLoad } = pull;
  useLayoutEffect(() => {
    if (previousBusy.current && !busy) finishLoad();
    previousBusy.current = busy;
  }, [busy, finishLoad]);
  useEffect(() => {
    const viewport = viewportRef.current!;
    const wheel = (event: WheelEvent) => onWheel(event, viewport);
    const scroll = () => onScroll(viewport);
    const start = (event: TouchEvent) => onTouchStart(event, viewport);
    const move = (event: TouchEvent) => onTouchMove(event, viewport);
    viewport.addEventListener("wheel", wheel, { passive: false });
    viewport.addEventListener("scroll", scroll);
    viewport.addEventListener("scrollend", onScrollEnd);
    viewport.addEventListener("touchstart", start);
    viewport.addEventListener("touchmove", move, { passive: false });
    viewport.addEventListener("touchend", onTouchEnd);
    viewport.addEventListener("touchcancel", onTouchCancel);
    return () => {
      viewport.removeEventListener("wheel", wheel);
      viewport.removeEventListener("scroll", scroll);
      viewport.removeEventListener("scrollend", onScrollEnd);
      viewport.removeEventListener("touchstart", start);
      viewport.removeEventListener("touchmove", move);
      viewport.removeEventListener("touchend", onTouchEnd);
      viewport.removeEventListener("touchcancel", onTouchCancel);
    };
  }, [onWheel, onScroll, onScrollEnd, onTouchStart, onTouchMove, onTouchEnd, onTouchCancel]);
  return <div ref={viewportRef} data-testid="viewport" data-slot="scroll-area-viewport"><div ref={spaceRef} data-testid="space" />
    <div ref={contentRef}><output>{pull.offset}</output><p>Reading anchor</p></div></div>;
}

describe("native release-to-load older history", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => { cleanup(); vi.useRealTimers(); });

  function setup() {
    const loadPage = vi.fn(() => true);
    const intent = vi.fn();
    const view = render(<Harness loadPage={loadPage} intent={intent} />);
    const viewport = view.getByTestId("viewport");
    Object.defineProperty(viewport, "clientHeight", { value: 600, configurable: true });
    Object.defineProperty(viewport, "onscrollend", { value: null, configurable: true });
    viewport.scrollTo = vi.fn((options?: ScrollToOptions | number) => {
      if (typeof options === "object") viewport.scrollTop = options.top ?? 0;
      viewport.dispatchEvent(new Event("scroll"));
      viewport.dispatchEvent(new Event("scrollend"));
    });
    const offset = () => Number(viewport.querySelector("output")!.textContent);
    return { ...view, viewport, loadPage, intent, offset, space: view.getByTestId("space") };
  }

  function wheel(viewport: HTMLElement, deltaY: number, momentum?: boolean, extra: WheelEventInit = {}) {
    const event = new WheelEvent("wheel", { deltaY, bubbles: true, cancelable: true, ...extra });
    if (extra.ctrlKey !== undefined) Object.defineProperty(event, "ctrlKey", { value: extra.ctrlKey });
    if (momentum !== undefined) Object.defineProperty(event, "momentum", { value: momentum });
    act(() => {
      viewport.dispatchEvent(event);
      // jsdom has no default scrolling. Simulate only the browser-owned move.
      if (!event.defaultPrevented && !event.ctrlKey && Math.abs(event.deltaX) <= Math.abs(deltaY)) {
        const pixels = deltaY * (extra.deltaMode === 1 ? 16 : extra.deltaMode === 2 ? 600 : 1);
        viewport.scrollTop = Math.max(0, viewport.scrollTop + pixels);
        viewport.dispatchEvent(new Event("scroll"));
      }
    });
    return event;
  }
  function end(viewport: HTMLElement) { act(() => { viewport.dispatchEvent(new Event("scrollend")); }); }
  function quiet() { act(() => { vi.advanceTimersByTime(5000); }); }
  function touch(viewport: HTMLElement, type: string, y?: number, fingers = 1) {
    const event = new Event(type, { bubbles: true, cancelable: true });
    Object.defineProperty(event, "touches", { value: y === undefined ? [] : Array.from({ length: fingers }, () => ({ clientY: y })) });
    act(() => { viewport.dispatchEvent(event); });
    return event;
  }

  it("keeps unavailable history in natural flow and prepares only when history arrives", () => {
    const loadPage = vi.fn(() => true);
    const intent = vi.fn();
    const { getByTestId, rerender } = render(<Harness available={false} loadPage={loadPage} intent={intent} />);
    const viewport = getByTestId("viewport");
    const space = getByTestId("space");
    const content = space.nextElementSibling as HTMLElement;
    expect(space.style.height).toBe("");
    expect(content.style.minHeight).toBe("");
    viewport.scrollTop = 40;
    viewport.dispatchEvent(new Event("scroll"));
    expect(viewport.scrollTop).toBe(40);
    expect(wheel(viewport, -10).defaultPrevented).toBe(false);
    expect(loadPage).not.toHaveBeenCalled();
    rerender(<Harness available loadPage={loadPage} intent={intent} />);
    expect(space.style.height).toBe("80px");
    expect(viewport.scrollTop).toBe(80);
  });

  it("stops an arriving gesture and its momentum at the top until a new gesture starts", () => {
    const { viewport, loadPage, offset } = setup();
    viewport.scrollTop = 120;
    wheel(viewport, -120, false);
    expect(wheel(viewport, -100, false).defaultPrevented).toBe(true);
    expect(wheel(viewport, -200, true).defaultPrevented).toBe(true);
    end(viewport);
    expect(offset()).toBe(0);
    expect(loadPage).not.toHaveBeenCalled();
    wheel(viewport, -40, false);
    expect(offset()).toBe(40);
  });

  it("moves the list in a real scroll range and holds indefinitely while fingers are stationary", () => {
    const { viewport, loadPage, offset, space } = setup();
    expect(wheel(viewport, -40, false).defaultPrevented).toBe(false);
    expect(space.style.height).toBe("80px");
    expect(offset()).toBe(40);
    wheel(viewport, -40, false);
    expect(offset()).toBe(80);
    quiet();
    expect(offset()).toBe(80);
    expect(space.style.height).toBe("80px");
    expect(loadPage).not.toHaveBeenCalled();
    end(viewport); // native phase end, including a lift with no fling
    expect(loadPage).toHaveBeenCalledOnce();
    expect(offset()).toBe(80);
    expect(space.style.height).toBe("80px");
    end(viewport);
    expect(loadPage).toHaveBeenCalledOnce();
  });

  it("uses native momentum as release and consumes its tail after a stable landing", () => {
    const { viewport, loadPage, intent, rerender, offset, space } = setup();
    const anchor = viewport.querySelector("p");
    wheel(viewport, -80, false);
    expect(wheel(viewport, -40, true).defaultPrevented).toBe(true);
    expect(loadPage).toHaveBeenCalledOnce();
    expect(offset()).toBe(80);
    rerender(<Harness busy loadPage={loadPage} intent={intent} />);
    viewport.scrollTop = 380; // prepend compensation, with the 80px reserve
    rerender(<Harness loadPage={loadPage} intent={intent} />);
    expect(space.style.height).toBe("80px");
    expect(viewport.scrollTop).toBe(380); // unchanged reserve preserves every row
    expect(offset()).toBe(0);
    for (let i = 0; i < 5; i++) expect(wheel(viewport, -50, true).defaultPrevented).toBe(true);
    quiet();
    expect(wheel(viewport, -1, true).defaultPrevented).toBe(true);
    expect(loadPage).toHaveBeenCalledOnce();
    expect(viewport.querySelector("p")).toBe(anchor);
    expect(wheel(viewport, -50, false).defaultPrevented).toBe(false);
    expect(viewport.scrollTop).toBe(330);
  });

  it("smoothly retracts a subthreshold pull after release without requesting", () => {
    const { viewport, loadPage, offset, space } = setup();
    wheel(viewport, -40, false);
    quiet();
    expect(offset()).toBe(40);
    end(viewport);
    expect(viewport.scrollTop).toBe(80);
    act(() => { viewport.dispatchEvent(new Event("scroll")); });
    end(viewport);
    quiet();
    expect(offset()).toBe(0);
    expect(space.style.height).toBe("80px");
    expect(loadPage).not.toHaveBeenCalled();
  });

  it("reversing completely closes the reserve without rearming the same gesture", () => {
    const { viewport, loadPage, offset, space } = setup();
    wheel(viewport, -40, false);
    wheel(viewport, 50, false);
    expect(space.style.height).toBe("80px");
    expect(viewport.scrollTop).toBe(90);
    wheel(viewport, -150, false);
    end(viewport);
    expect(offset()).toBe(0);
    expect(loadPage).not.toHaveBeenCalled();
  });

  it.each([{ ctrlKey: true }, { deltaX: 200 }])("cancels for zoom/horizontal intent: %j", (extra) => {
    const { viewport, loadPage } = setup();
    wheel(viewport, -80, false);
    expect(wheel(viewport, -20, false, extra).defaultPrevented).toBe(false);
    end(viewport); quiet();
    expect(loadPage).not.toHaveBeenCalled();
  });

  it.each([{ available: false }, { busy: true }])("does not load after history becomes unavailable: %j", (state) => {
    const { viewport, loadPage, intent, rerender } = setup();
    wheel(viewport, -80, false);
    rerender(<Harness {...state} loadPage={loadPage} intent={intent} />);
    end(viewport); quiet();
    expect(loadPage).not.toHaveBeenCalled();
  });

  it("drops the reserve and old release state on conversation change", () => {
    const { viewport, loadPage, intent, rerender, offset, space } = setup();
    wheel(viewport, -80, false);
    const nextLoad = vi.fn(() => true);
    rerender(<Harness thread="second" loadPage={nextLoad} intent={intent} />);
    expect(offset()).toBe(0);
    expect(space.style.height).toBe("80px");
    end(viewport); quiet();
    expect(loadPage).not.toHaveBeenCalled();
    expect(nextLoad).not.toHaveBeenCalled();
    viewport.scrollTop = 80;
    wheel(viewport, -80, false); end(viewport);
    expect(nextLoad).toHaveBeenCalledOnce();
  });

  it("does not replace missing native scrollend with a pause-to-load timer", () => {
    const { viewport, loadPage, offset } = setup();
    Reflect.deleteProperty(viewport, "onscrollend");
    wheel(viewport, -100, false); quiet();
    expect(offset()).toBe(0);
    expect(loadPage).not.toHaveBeenCalled();
  });

  it("cleans a canceled return on unmount without requesting", () => {
    const { viewport, loadPage, unmount } = setup();
    wheel(viewport, -40, false); end(viewport);
    unmount(); quiet();
    expect(loadPage).not.toHaveBeenCalled();
  });

  it("holds touch pulls until touchend and does not arm on arrival from within the list", () => {
    const { viewport, loadPage, offset } = setup();
    viewport.scrollTop = 120;
    touch(viewport, "touchstart", 100);
    viewport.scrollTop = 80;
    touch(viewport, "touchmove", 250);
    touch(viewport, "touchend");
    expect(loadPage).not.toHaveBeenCalled();
    touch(viewport, "touchstart", 100);
    expect(touch(viewport, "touchmove", 140).defaultPrevented).toBe(true);
    expect(offset()).toBe(40);
    touch(viewport, "touchmove", 180);
    end(viewport); quiet();
    expect(offset()).toBe(80);
    expect(loadPage).not.toHaveBeenCalled();
    touch(viewport, "touchend");
    expect(loadPage).toHaveBeenCalledOnce();
    expect(offset()).toBe(80);
  });

  it.each(["touchcancel", "multitouch"])("cancels touch intent on %s", (type) => {
    const { viewport, loadPage } = setup();
    touch(viewport, "touchstart", 100);
    touch(viewport, "touchmove", 220);
    if (type === "multitouch") touch(viewport, "touchmove", 240, 2);
    else touch(viewport, type);
    touch(viewport, "touchend"); quiet();
    expect(loadPage).not.toHaveBeenCalled();
  });

  it("releases a rejected request without leaving the list displaced", () => {
    const { viewport, loadPage, space } = setup();
    loadPage.mockReturnValue(false);
    wheel(viewport, -80, false); end(viewport); quiet();
    expect(loadPage).toHaveBeenCalledOnce();
    expect(space.style.height).toBe("80px");
  });
});
