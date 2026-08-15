// ScrollArea — viewportClassName plumbing. The timeline relies on this to set
// `overflow-anchor: none` on the inner scroll viewport (the element that
// actually scrolls), so the prop must land on the viewport, not the root.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ScrollArea } from "./scroll-area";

afterEach(cleanup);

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
});
