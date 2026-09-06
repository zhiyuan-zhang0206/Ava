import { renderToString } from "react-dom/server";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/components/ui/resizable", () => ({
  ResizablePanelGroup: ({
    autoSaveId,
    children,
    direction,
  }: {
    autoSaveId: string;
    children: React.ReactNode;
    direction: string;
  }) => (
    <div
      data-testid="resizable-panel-group"
      data-slot="resizable-panel-group"
      data-autosave-id={autoSaveId}
      data-direction={direction}
    >
      {children}
    </div>
  ),
  ResizablePanel: ({
    children,
    defaultSize,
    maxSize,
    minSize,
  }: {
    children: React.ReactNode;
    defaultSize: number;
    maxSize?: number;
    minSize: number;
  }) => (
    <div
      data-slot="resizable-panel"
      data-default-size={defaultSize}
      data-min-size={minSize}
      data-max-size={maxSize}
    >
      {children}
    </div>
  ),
  ResizableHandle: () => <div data-slot="resizable-handle" />,
}));

import { HomeLayout } from "./home-layout";

afterEach(cleanup);

function panes() {
  return {
    sidebar: <aside data-testid="agent-tree" />,
    main: <section data-testid="main-timeline" />,
    inspector: <aside data-testid="inspector-panel" />,
  };
}

function directPanelDefaults(group: HTMLElement): number[] {
  return Array.from(group.children)
    .filter((child) => child.getAttribute("data-slot") === "resizable-panel")
    .map((panel) => Number(panel.getAttribute("data-default-size")));
}

describe("HomeLayout autosave-safe responsive frames", () => {
  it("server render reserves the layout without mounting a panel group", () => {
    const html = renderToString(
      <HomeLayout
        {...panes()}
        isNarrow={false}
        isLarge
        sidebarCollapsed={false}
      />,
    );

    expect(html).toContain('data-testid="home-layout-placeholder"');
    expect(html).not.toContain("resizable-panel-group");
    expect(html).not.toContain('data-testid="main-timeline"');
  });

  it("desktop frame uses fixed horizontal groups whose defaults each total 100", () => {
    render(
      <HomeLayout
        {...panes()}
        isNarrow={false}
        isLarge
        sidebarCollapsed={false}
      />,
    );

    const groups = screen.getAllByTestId("resizable-panel-group");
    expect(groups).toHaveLength(2);
    expect(groups.map((group) => group.getAttribute("data-direction"))).toEqual([
      "horizontal",
      "horizontal",
    ]);
    expect(groups.map((group) => group.getAttribute("data-autosave-id"))).toEqual([
      "ava.home.columns.desktop",
      "ava.home.inspector.desktop",
    ]);
    expect(directPanelDefaults(groups[0])).toEqual([30, 70]);
    expect(directPanelDefaults(groups[0]).reduce((sum, size) => sum + size, 0)).toBe(100);
    expect(directPanelDefaults(groups[1])).toEqual([68, 32]);
    expect(directPanelDefaults(groups[1]).reduce((sum, size) => sum + size, 0)).toBe(100);
  });

  it("collapsed desktop frame still totals 100 without overwriting the expanded constraints", () => {
    render(
      <HomeLayout
        {...panes()}
        isNarrow={false}
        isLarge
        sidebarCollapsed
      />,
    );

    const outer = screen.getAllByTestId("resizable-panel-group")[0];
    expect(directPanelDefaults(outer)).toEqual([3, 97]);
    expect(directPanelDefaults(outer).reduce((sum, size) => sum + size, 0)).toBe(100);
  });

  it("closed inspector frame gives its sole main panel the full group", () => {
    render(
      <HomeLayout
        {...panes()}
        inspector={null}
        isNarrow={false}
        isLarge
        sidebarCollapsed={false}
      />,
    );

    const groups = screen.getAllByTestId("resizable-panel-group");
    expect(directPanelDefaults(groups[1])).toEqual([100]);
    expect(directPanelDefaults(groups[1]).reduce((sum, size) => sum + size, 0)).toBe(100);
  });

  it("compact frame has an independent storage key and keeps overlays outside its horizontal split", () => {
    render(
      <HomeLayout
        {...panes()}
        isNarrow={false}
        isLarge={false}
        sidebarCollapsed={false}
      />,
    );

    const groups = screen.getAllByTestId("resizable-panel-group");
    expect(groups).toHaveLength(1);
    expect(groups[0].getAttribute("data-autosave-id")).toBe("ava.home.columns.mobile");
    expect(groups[0].getAttribute("data-direction")).toBe("horizontal");
    expect(directPanelDefaults(groups[0])).toEqual([40, 60]);
    expect(directPanelDefaults(groups[0]).reduce((sum, size) => sum + size, 0)).toBe(100);
    expect(screen.getByTestId("inspector-panel").parentElement).not.toBe(groups[0]);
  });

  it("remounts onto the independent autosave frame when the breakpoint flips", () => {
    const { rerender } = render(
      <HomeLayout
        {...panes()}
        isNarrow={false}
        isLarge
        sidebarCollapsed={false}
      />,
    );
    const desktopGroup = screen.getAllByTestId("resizable-panel-group")[0];

    rerender(
      <HomeLayout
        {...panes()}
        isNarrow={false}
        isLarge={false}
        sidebarCollapsed={false}
      />,
    );
    const mobileGroup = screen.getByTestId("resizable-panel-group");
    expect(mobileGroup).not.toBe(desktopGroup);
    expect(mobileGroup.getAttribute("data-autosave-id")).toBe("ava.home.columns.mobile");
    expect(directPanelDefaults(mobileGroup).reduce((sum, size) => sum + size, 0)).toBe(100);
  });
});
