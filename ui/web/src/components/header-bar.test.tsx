// HeaderBar: label render + sidebar button (mobile) + Memory Graph / Fleet / Insights / Control navigation + children slot.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BAR_DIVIDER_CLASS, BAR_HEIGHT_CLASS } from "@/lib/layout";

import { HeaderBar } from "./header-bar";

afterEach(() => {
  cleanup();
});

describe("HeaderBar", () => {
  it("renders label text", () => {
    render(<HeaderBar label="Agent #5 · idle" onOpenSidebar={vi.fn()} />);
    expect(screen.getByText("Agent #5 · idle")).toBeTruthy();
  });

  it("uses sans typography for the UI title", () => {
    const { container } = render(<HeaderBar label="Agent #5 · idle" onOpenSidebar={vi.fn()} />);
    const header = container.querySelector("header")!;
    expect(header.classList).toContain("font-sans");
    expect(header.classList).not.toContain("font-mono");
  });

  it("click sidebar button → onOpenSidebar called", () => {
    const onOpen = vi.fn();
    render(<HeaderBar label="x" onOpenSidebar={onOpen} />);
    fireEvent.click(screen.getByRole("button", { name: "Open sidebar" }));
    expect(onOpen).toHaveBeenCalled();
  });

  it("floats above the timeline with a blurred translucent background", () => {
    // User ruling 2026-08-06: the header bar is a floating layer over the
    // full-bleed timeline surface (absolute + backdrop blur), not a sticky
    // row in the content column.
    const { container } = render(<HeaderBar label="x" onOpenSidebar={vi.fn()} />);
    const header = container.firstChild as HTMLElement;
    expect([...header.classList]).toContain("absolute");
    expect([...header.classList]).not.toContain("relative");
    expect(header.className).toContain("top-0");
    expect(header.className).toContain("z-20");
    expect(header.className).toContain("bg-background/80");
    expect(header.className).toContain("backdrop-blur-md");
  });

  it("puts the shared title height and inset divider on the outer header", () => {
    const { container } = render(<HeaderBar label="x" onOpenSidebar={vi.fn()} />);
    const header = container.querySelector("header")!;
    const inner = container.querySelector("header > div")!;

    expect(header.className).toContain(BAR_HEIGHT_CLASS);
    for (const dividerClass of BAR_DIVIDER_CLASS.split(" ")) {
      expect(header.className).toContain(dividerClass);
    }
    expect(header.className).not.toContain("border-b");
    expect(inner.className).not.toContain("border-b");
  });

  it("maxWidthCss centers the inner content with the timeline column", () => {
    const { container } = render(
      <HeaderBar label="x" onOpenSidebar={vi.fn()} maxWidthCss="min(40vw, 1280px)" />,
    );
    const inner = container.querySelector("header > div");
    expect((inner as HTMLElement | null)?.style.maxWidth).toBe("min(40vw, 1280px)");
    expect(inner?.className).toContain("mx-auto");
    // w-full: on narrow viewports (no maxWidthCss) the inner bar spans
    // the pane like every other bar instead of shrink-wrapping its
    // content (Task #881).
    expect(inner?.className).toContain("w-full");
  });

  it("children slot renders custom content on the right", () => {
    render(
      <HeaderBar label="x" onOpenSidebar={vi.fn()}>
        <span data-testid="header-extra">extra</span>
      </HeaderBar>,
    );
    expect(screen.getByTestId("header-extra")).toBeTruthy();
  });
});
