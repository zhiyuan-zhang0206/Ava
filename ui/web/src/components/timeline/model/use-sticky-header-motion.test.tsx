import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useStickyHeaderMotion } from "./use-sticky-header-motion";

function Harness({ thread = "first", compensated = false, onRestore }: {
  thread?: string;
  compensated?: boolean;
  onRestore: () => void;
}) {
  const wrapperRef = useRef<HTMLDivElement>(null);
  const track = useStickyHeaderMotion(wrapperRef, thread, onRestore);
  return (
    <div ref={wrapperRef} data-testid="wrapper" className="group/timeline">
      <div data-slot="scroll-area-viewport" onScroll={(event) => track(event.currentTarget, compensated)}>
        <button type="button">Message title</button>
        <p>Message body</p>
      </div>
    </div>
  );
}

describe("sticky titles during rapid scrolling", () => {
  beforeEach(() => vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "performance"] }));
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  function scroll(viewport: HTMLElement, top: number, elapsed = 16) {
    act(() => {
      vi.advanceTimersByTime(elapsed);
      viewport.scrollTop = top;
      fireEvent.scroll(viewport);
    });
  }

  function setup() {
    const onRestore = vi.fn();
    const view = render(<Harness onRestore={onRestore} />);
    return {
      ...view,
      onRestore,
      wrapper: view.getByTestId("wrapper"),
      viewport: view.container.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]')!,
    };
  }

  it("keeps slow reading pinned and releases rapid movement in either direction without remounting content", () => {
    const { wrapper, viewport } = setup();
    const title = viewport.querySelector("button");
    const body = viewport.querySelector("p");
    scroll(viewport, 4);
    expect(wrapper.dataset.fastScrolling).toBeUndefined();
    scroll(viewport, 100);
    expect(wrapper.dataset.fastScrolling).toBe("true");
    scroll(viewport, 10);
    expect(wrapper.dataset.fastScrolling).toBe("true");
    expect(viewport.querySelector("button")).toBe(title);
    expect(viewport.querySelector("p")).toBe(body);
    expect(viewport.scrollTop).toBe(10);
  });

  it("waits for settled low speed and does not toggle repeatedly near the entry threshold", () => {
    const { wrapper, viewport, onRestore } = setup();
    scroll(viewport, 60);
    for (let top = 80; top <= 180; top += 20) scroll(viewport, top);
    expect(wrapper.dataset.fastScrolling).toBe("true");
    expect(onRestore).not.toHaveBeenCalled();
    for (let top = 184; top <= 212; top += 4) scroll(viewport, top);
    expect(wrapper.dataset.fastScrolling).toBe("true");
    scroll(viewport, 216);
    expect(wrapper.dataset.fastScrolling).toBeUndefined();
    expect(onRestore).toHaveBeenCalledOnce();
  });

  it("restores titles when movement stops, keeping the viewport fixed", () => {
    const { wrapper, viewport, onRestore } = setup();
    scroll(viewport, 90);
    act(() => { vi.advanceTimersByTime(159); });
    expect(wrapper.dataset.fastScrolling).toBe("true");
    act(() => { vi.advanceTimersByTime(1); });
    expect(wrapper.dataset.fastScrolling).toBeUndefined();
    expect(viewport.scrollTop).toBe(90);
    expect(onRestore).toHaveBeenCalledOnce();
    scroll(viewport, 150, 2000);
    expect(wrapper.dataset.fastScrolling).toBe("true");
  });

  it("does not treat history prepend compensation as a fast gesture", () => {
    const { wrapper, viewport, rerender, onRestore } = setup();
    rerender(<Harness compensated onRestore={onRestore} />);
    scroll(viewport, 3000);
    expect(wrapper.dataset.fastScrolling).toBeUndefined();
    rerender(<Harness onRestore={onRestore} />);
    scroll(viewport, 3004);
    expect(wrapper.dataset.fastScrolling).toBeUndefined();
  });

  it("clears suppression and an obsolete settle callback when switching conversations", () => {
    const { wrapper, viewport, rerender, onRestore } = setup();
    scroll(viewport, 80);
    rerender(<Harness thread="second" onRestore={onRestore} />);
    expect(wrapper.dataset.fastScrolling).toBeUndefined();
    act(() => { vi.advanceTimersByTime(500); });
    expect(onRestore).not.toHaveBeenCalled();
    scroll(viewport, 84);
    expect(wrapper.dataset.fastScrolling).toBeUndefined();
  });

  it("cancels its restore when the timeline unmounts", () => {
    const { viewport, unmount, onRestore } = setup();
    scroll(viewport, 80);
    unmount();
    act(() => { vi.advanceTimersByTime(500); });
    expect(onRestore).not.toHaveBeenCalled();
  });
});
