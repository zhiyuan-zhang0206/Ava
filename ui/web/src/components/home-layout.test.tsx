import { useEffect, type CSSProperties, type ReactNode } from "react";
import { renderToString } from "react-dom/server";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/components/ui/resizable", () => ({
  ResizablePanelGroup: ({
    autoSaveId,
    children,
    direction,
  }: {
    autoSaveId: string;
    children: ReactNode;
    direction: string;
  }) => {
    useEffect(() => {
      localStorage.setItem(`react-resizable-panels:${autoSaveId}`, "mock persisted layout");
    });

    return (
      <div
        data-testid="resizable-panel-group"
        data-slot="resizable-panel-group"
        data-autosave-id={autoSaveId}
        data-direction={direction}
      >
        {children}
      </div>
    );
  },
  ResizablePanel: ({
    children,
    defaultSize,
    maxSize,
    minSize,
  }: {
    children: ReactNode;
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
