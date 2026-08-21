"use client";

// useSettledAnchorScroll: the mount-time hash scroll plus the settle
// re-scroll. happy-dom has no ResizeObserver and no real layout, so the
// re-scroll path is exercised through a fake ResizeObserver whose trigger()
// runs the hook's callback, with the container's scrollHeight stubbed to
// simulate async content growth.

import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSettledAnchorScroll } from "./_anchor-scroll";

class FakeResizeObserver {
  static instances: FakeResizeObserver[] = [];
  callback: ResizeObserverCallback;
  observed: Element | null = null;
  disconnected = false;

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
    FakeResizeObserver.instances.push(this);
  }
  observe(target: Element) {
    this.observed = target;
  }
  unobserve() {
    // noop — the hook only calls observe/disconnect.
  }
  disconnect() {
    this.disconnected = true;
  }
  trigger() {
    this.callback([], this);
  }
}

function Harness({ targetId }: { targetId: string | null }) {
  useSettledAnchorScroll("scroll", targetId);
  return (
    <div id="scroll">
      <div data-testid="content">
        <section id="alerts">Alerts</section>
      </div>
    </div>
  );
}

function installFakeObserver(): FakeResizeObserver[] {
  const instances: FakeResizeObserver[] = (FakeResizeObserver.instances = []);
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  return instances;
}

let scrollIntoView: (arg?: boolean | ScrollIntoViewOptions) => void;

beforeEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  scrollIntoView = vi.fn();
  Element.prototype.scrollIntoView = scrollIntoView;
});

afterEach(cleanup);

describe("useSettledAnchorScroll", () => {
  it("scrolls the resolved target into view on mount", () => {
    render(<Harness targetId="alerts" />);
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
  });

  it("does nothing for a null target", () => {
    render(<Harness targetId={null} />);
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it("re-scrolls when the content height changes, then stops once settled", () => {
    vi.useFakeTimers();
    try {
      const instances = installFakeObserver();
      const { container } = render(<Harness targetId="alerts" />);
      const scrollContainer = container.querySelector("#scroll")!;
      expect(instances).toHaveLength(1);
      const observer = instances[0];
      expect(observer.observed).toBe(container.querySelector("[data-testid=content]"));

      // The async content above the target grows — the observer fires and the
      // hook re-applies the scroll.
      Object.defineProperty(scrollContainer, "scrollHeight", {
        configurable: true,
        value: 4000,
      });
      observer.trigger();
      expect(scrollIntoView).toHaveBeenCalledTimes(2);

      // A further height change within the settle window re-scrolls again.
      Object.defineProperty(scrollContainer, "scrollHeight", {
        configurable: true,
        value: 5000,
      });
      observer.trigger();
      expect(scrollIntoView).toHaveBeenCalledTimes(3);

      // Once the height stays stable for the grace period the observer
      // detaches — the settle re-scroll is over.
      vi.advanceTimersByTime(600);
      expect(observer.disconnected).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops re-scrolling once the user takes over the scroll position", () => {
    const instances = installFakeObserver();
    const { container } = render(<Harness targetId="alerts" />);
    const scrollContainer = container.querySelector("#scroll")!;
    const observer = instances[0];

    // User scrolls during the settle window (the hook's lastScrollTop was 0).
    Object.defineProperty(scrollContainer, "scrollTop", {
      configurable: true,
      value: 400,
    });
    Object.defineProperty(scrollContainer, "scrollHeight", {
      configurable: true,
      value: 4000,
    });
    observer.trigger();

    expect(observer.disconnected).toBe(true);
    // The user-takeover re-scroll is suppressed: only the mount scroll ran.
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
  });
});
