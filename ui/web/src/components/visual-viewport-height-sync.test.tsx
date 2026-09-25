// VisualViewportHeightSync — the iOS half of the on-screen keyboard contract
// (the Android half is the `interactiveWidget` viewport export in
// app/layout.tsx). happy-dom has no VisualViewport at all, so every test
// installs a controllable stub and states its own innerHeight; the fake moves
// height and scale the way a real keyboard/toolbar/pinch does.

import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import {
  KEYBOARD_MIN_VISUAL_VIEWPORT_DELTA_PX,
  VisualViewportHeightSync,
} from "./visual-viewport-height-sync";

const LAYOUT_HEIGHT = 800;

type Listener = () => void;

class FakeVisualViewport {
  // happy-dom does not implement VisualViewport; the component only reads
  // height/scale and subscribes to resize/scroll, so that is the whole stub.
  scale = 1;
  readonly listeners: Record<"resize" | "scroll", Set<Listener>> = {
    resize: new Set(),
    scroll: new Set(),
  };

  constructor(public height: number) {}

  addEventListener(type: "resize" | "scroll", listener: Listener): void {
    this.listeners[type].add(listener);
  }

  removeEventListener(type: "resize" | "scroll", listener: Listener): void {
    this.listeners[type].delete(listener);
  }

  /** Move the viewport like the browser would: new height/scale, then resize. */
  set(height: number, scale = 1): void {
    this.height = height;
    this.scale = scale;
    for (const listener of this.listeners.resize) listener();
  }
}

function installVisualViewport(height: number): FakeVisualViewport {
  const viewport = new FakeVisualViewport(height);
  vi.stubGlobal("innerHeight", LAYOUT_HEIGHT);
  vi.stubGlobal("visualViewport", viewport);
  return viewport;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  // cleanup unmounts (which clears the pin); a failed render would otherwise
  // leak the inline height into the next test.
  document.documentElement.style.height = "";
});

it("pins the shell to the visual viewport while the keyboard is up", () => {
  const viewport = installVisualViewport(LAYOUT_HEIGHT - 320);
  render(<VisualViewportHeightSync />);

  expect(document.documentElement.style.height).toBe("480px");
  viewport.set(300); // keyboard grows / user resizes
  expect(document.documentElement.style.height).toBe("300px");
});

it("rounds a fractional visual viewport height", () => {
  installVisualViewport(LAYOUT_HEIGHT - 320.4);
  render(<VisualViewportHeightSync />);

  expect(document.documentElement.style.height).toBe("480px");
});

it("leaves the shell alone when the shortfall is chrome-sized", () => {
  // Exactly at the threshold: a conservative bound, so browser chrome alone
  // (toolbar collapse/expand, tens of px) never shrinks the page.
  installVisualViewport(LAYOUT_HEIGHT - KEYBOARD_MIN_VISUAL_VIEWPORT_DELTA_PX);
  render(<VisualViewportHeightSync />);

  expect(document.documentElement.style.height).toBe("");
});

it("drops the pin while the user pinch-zooms", () => {
  const viewport = installVisualViewport(LAYOUT_HEIGHT - 320);
  render(<VisualViewportHeightSync />);
  expect(document.documentElement.style.height).toBe("480px");

  viewport.set(LAYOUT_HEIGHT - 320, 2);
  expect(document.documentElement.style.height).toBe("");
});

it("restores the layout height when the keyboard closes", () => {
  const viewport = installVisualViewport(LAYOUT_HEIGHT - 320);
  render(<VisualViewportHeightSync />);

  viewport.set(LAYOUT_HEIGHT);
  expect(document.documentElement.style.height).toBe("");
});

it("clears the pin on unmount", () => {
  installVisualViewport(LAYOUT_HEIGHT - 320);
  const { unmount } = render(<VisualViewportHeightSync />);
  expect(document.documentElement.style.height).toBe("480px");

  unmount();
  expect(document.documentElement.style.height).toBe("");
});

it("does nothing when the browser has no VisualViewport", () => {
  vi.stubGlobal("innerHeight", LAYOUT_HEIGHT);
  vi.stubGlobal("visualViewport", undefined);
  render(<VisualViewportHeightSync />);

  expect(document.documentElement.style.height).toBe("");
});
