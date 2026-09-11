import type { CSSProperties, ReactNode } from "react";
import { renderToString } from "react-dom/server";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The ui/resizable wrapper is mocked: the library's real layout engine needs a
// measured container, so these tests pin the home frame contract instead —
// which frames mount, with which panels and defaults, what may persist, and
// how a v3-stored split carries over. The real persistence path runs through
// useDefaultLayout (not mocked) + lib/panel-layout-storage.
vi.mock("@/components/ui/resizable", () => ({
  ResizablePanelGroup: ({
    children,
    defaultLayout,
    onLayoutChanged,
    orientation,
  }: {
    children: ReactNode;
    defaultLayout?: Record<string, number> | undefined;
    onLayoutChanged?: (
      layout: Record<string, number>,
      meta: { isUserInteraction: boolean },
    ) => void;
    orientation: string;
  }) => (
    <div
      data-testid="resizable-panel-group"
      data-slot="resizable-panel-group"
      data-orientation={orientation}
      data-default-layout={JSON.stringify(defaultLayout ?? null)}
    >
      {/* The library commits a layout on mount / on a shape or container
          change (programmatic), and a real drag commits with
          isUserInteraction: true — one button each so tests can drive both. */}
      <button
        data-testid="layout-commit-programmatic"
        onClick={() =>
          onLayoutChanged?.(
            { "panel-sidebar": 30, "panel-main": 70 },
            { isUserInteraction: false },
          )
        }
      />
      <button
        data-testid="layout-commit-drag"
        onClick={() =>
          onLayoutChanged?.(
            { "panel-sidebar": 41, "panel-main": 59 },
            { isUserInteraction: true },
          )
        }
      />
      {children}
    </div>
  ),
  ResizablePanel: ({
    children,
    defaultSize,
    id,
    maxSize,
    minSize,
  }: {
    children: ReactNode;
    defaultSize: string;
    id: string;
    maxSize?: string;
    minSize: string;
  }) => (
    <div
      data-slot="resizable-panel"
      data-panel-id={id}
      data-default-size={defaultSize}
      data-min-size={minSize}
      data-max-size={maxSize}
    >
      {children}
    </div>
  ),
  ResizableHandle: ({
    className,
    style,
  }: {
    className?: string;
    style?: CSSProperties;
  }) => <div data-slot="resizable-handle" className={className} style={style} />,
}));

import { HomeLayout } from "./home-layout";

function installLocalStoragePolyfill(): void {
  const store = new Map<string, string>();
  const fake: Storage = {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (key) => store.get(key) ?? null,
    setItem: (key, value) => store.set(key, value),
    removeItem: (key) => store.delete(key),
    key: (index) => Array.from(store.keys())[index] ?? null,
  };
  Object.defineProperty(globalThis, "localStorage", {
    value: fake,
    writable: true,
    configurable: true,
  });
}

beforeEach(installLocalStoragePolyfill);

afterEach(() => {
  cleanup();
  localStorage.clear();
});

function panes() {
  return {
    sidebar: <aside data-testid="agent-tree" />,
    main: <section data-testid="main-timeline" />,
    inspector: <aside data-testid="inspector-panel" />,
  };
}

function directPanels(group: HTMLElement): HTMLElement[] {
  return Array.from(group.children).filter(
    (child) => child.getAttribute("data-slot") === "resizable-panel",
  ) as HTMLElement[];
}

function directPanelDefaults(group: HTMLElement): number[] {
  return directPanels(group).map((panel) =>
    Number.parseFloat(panel.getAttribute("data-default-size") ?? ""),
  );
}

function directPanelIds(group: HTMLElement): (string | null)[] {
  return directPanels(group).map((panel) => panel.getAttribute("data-panel-id"));
}

// The inspector group nests inside the columns group, so queries must stay on
// direct children of the group being driven.
function clickDirect(group: HTMLElement, testId: string): void {
  const button = Array.from(group.children).find(
    (child) => child.getAttribute("data-testid") === testId,
  );
  if (!button) throw new Error(`no ${testId} among the group's direct children`);
  fireEvent.click(button);
}

describe("HomeLayout frame contract", () => {
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
    expect(groups.map((group) => group.getAttribute("data-orientation"))).toEqual([
      "horizontal",
      "horizontal",
    ]);
    expect(directPanelIds(groups[0])).toEqual(["panel-sidebar", "panel-main"]);
    expect(directPanelIds(groups[1])).toEqual(["panel-timeline", "panel-inspector"]);
    expect(directPanelDefaults(groups[0])).toEqual([30, 70]);
    expect(directPanelDefaults(groups[0]).reduce((sum, size) => sum + size, 0)).toBe(100);
    expect(directPanelDefaults(groups[1])).toEqual([68, 32]);
    expect(directPanelDefaults(groups[1]).reduce((sum, size) => sum + size, 0)).toBe(100);
  });

  it("insets both desktop divider lines across the same header-to-composer segment", () => {
    render(
      <HomeLayout
        {...panes()}
        isNarrow={false}
        isLarge
        sidebarCollapsed={false}
      />,
    );

    const handles = Array.from(
      document.querySelectorAll<HTMLElement>('[data-slot="resizable-handle"]'),
    );
    expect(handles).toHaveLength(2);
    expect(handles.map((handle) => handle.className)).toEqual([
      "after:top-[var(--home-divider-line-top)] after:bottom-[var(--home-divider-line-bottom)]",
      "after:top-[var(--home-divider-line-top)] after:bottom-[var(--home-divider-line-bottom)]",
    ]);
    expect(
      handles.map((handle) => ({
        top: handle.style.getPropertyValue("--home-divider-line-top"),
        bottom: handle.style.getPropertyValue("--home-divider-line-bottom"),
      })),
    ).toEqual([
      { top: "84px", bottom: "89px" },
      { top: "84px", bottom: "89px" },
    ]);
  });

  it("collapsed desktop frame is static and cannot write panel layout storage", () => {
    localStorage.setItem("sentinel", "unchanged");

    render(
      <HomeLayout
        {...panes()}
        inspector={null}
        isNarrow={false}
        isLarge
        sidebarCollapsed
      />,
    );

    expect(screen.queryAllByTestId("resizable-panel-group")).toHaveLength(0);
    expect(screen.getByTestId("agent-tree").parentElement?.style.flexBasis).toBe("3%");
    expect(document.querySelector('[data-slot="static-divider"]')).not.toBeNull();
    expect(screen.getByTestId("main-timeline")).toBeTruthy();
    expect(localStorage.length).toBe(1);
    expect(localStorage.getItem("sentinel")).toBe("unchanged");
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
    expect(directPanelIds(groups[1])).toEqual(["panel-timeline"]);
    expect(directPanelDefaults(groups[1])).toEqual([100]);
    expect(directPanelDefaults(groups[1]).reduce((sum, size) => sum + size, 0)).toBe(100);
  });

  it("compact frame has an independent layout id and keeps overlays outside its horizontal split", () => {
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
    expect(groups[0].getAttribute("data-orientation")).toBe("horizontal");
    expect(directPanelIds(groups[0])).toEqual(["panel-sidebar", "panel-main"]);
    expect(directPanelDefaults(groups[0])).toEqual([40, 60]);
    expect(directPanelDefaults(groups[0]).reduce((sum, size) => sum + size, 0)).toBe(100);
    expect(screen.getByTestId("inspector-panel").parentElement).not.toBe(groups[0]);

    clickDirect(groups[0], "layout-commit-drag");
    expect(localStorage.getItem("react-resizable-panels:ava.home.columns.mobile")).not.toBeNull();
    expect(localStorage.getItem("react-resizable-panels:ava.home.columns.desktop")).toBeNull();
  });

  it("remounts onto the independent frame when the breakpoint flips", () => {
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
    expect(directPanelDefaults(mobileGroup).reduce((sum, size) => sum + size, 0)).toBe(100);
  });

  it("persists only user-driven commits, under each frame's own layout id", () => {
    render(
      <HomeLayout
        {...panes()}
        isNarrow={false}
        isLarge
        sidebarCollapsed={false}
      />,
    );

    const groups = screen.getAllByTestId("resizable-panel-group");
    for (const group of groups) {
      clickDirect(group, "layout-commit-programmatic");
    }
    expect(localStorage.length).toBe(0);

    for (const group of groups) {
      clickDirect(group, "layout-commit-drag");
    }
    expect(localStorage.getItem("react-resizable-panels:ava.home.columns.desktop")).not.toBeNull();
    expect(localStorage.getItem("react-resizable-panels:ava.home.inspector.desktop")).not.toBeNull();
  });

  it("restores a v3-stored split by mapping its layout onto the panel ids", () => {
    localStorage.setItem(
      "react-resizable-panels:ava.home.columns.desktop",
      JSON.stringify({
        '{"minSize":20,"maxSize":50},{"minSize":45}': {
          expandToSizes: {},
          layout: [41, 59],
        },
      }),
    );

    render(
      <HomeLayout
        {...panes()}
        isNarrow={false}
        isLarge
        sidebarCollapsed={false}
      />,
    );

    const groups = screen.getAllByTestId("resizable-panel-group");
    expect(JSON.parse(groups[0].getAttribute("data-default-layout")!)).toEqual({
      "panel-sidebar": 41,
      "panel-main": 59,
    });
    expect(groups[1].getAttribute("data-default-layout")).toBe("null");
  });
});
