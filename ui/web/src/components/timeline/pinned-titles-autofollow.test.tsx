import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BackendTimelineItem } from "@/lib/contracts/types";

import { TimelineView } from ".";

vi.mock("@/lib/format/display-limits", () => ({
  useDisplayLimit: (_envVar: string, fallback: number) => fallback,
}));

vi.mock("@/lib/state/use-user-settings", () => ({
  useUserSettings: () => ({ settings: {}, setSetting: vi.fn(), isLoading: false }),
}));

vi.mock("@/lib/layout/content-toggle-store", () => ({
  useContentToggle: () => ({ detailsMode: "all", isLoading: false }),
  useContentToggleReset: (selector: (state: { resetToken: number }) => unknown) => selector({ resetToken: 0 }),
}));

vi.mock("../content/markdown", () => ({
  ChatMarkdown: ({ content }: { content: string }) => <div>{content}</div>,
}));

vi.mock("../content/python-code", () => ({
  PythonCode: ({ code }: { code: string }) => <pre>{code}</pre>,
  preloadPythonCodeHighlighter: () => Promise.resolve(),
}));

vi.mock("../ui/scroll-area", () => ({
  ScrollArea: ({ children }: { children: ReactNode }) => (
    <div>
      <div data-slot="scroll-area-viewport">{children}</div>
    </div>
  ),
}));

function makeItems(): BackendTimelineItem[] {
  return [
    { item_id: "1.0", kind: "agent_reasoning", payload: "thinking", source: null, created_at: "2026-05-15T12:00:00Z", inbound_id: null, show_timestamp: false },
    { item_id: "1.1", kind: "agent_code", payload: "code", source: null, created_at: "2026-05-15T12:00:00Z", inbound_id: null, show_timestamp: false },
    { item_id: "1.2", kind: "code_output", payload: "output", source: null, created_at: "2026-05-15T12:00:00Z", inbound_id: null, show_timestamp: false },
  ];
}

function mockRect(top: number, bottom: number, height: number): DOMRect {
  return {
    top,
    bottom,
    height,
    left: 0,
    right: 400,
    width: 400,
    x: 0,
    y: top,
    toJSON: () => undefined,
  };
}

function setupStuckBlock() {
  const { container } = render(<TimelineView items={makeItems()} />);
  const viewport = container.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]')!;
  Object.defineProperty(viewport, "scrollHeight", { value: 2000, configurable: true });
  Object.defineProperty(viewport, "clientHeight", { value: 600, configurable: true });
  vi.spyOn(viewport, "getBoundingClientRect").mockReturnValue(mockRect(0, 800, 800));
  const turnEl = container.querySelector<HTMLElement>('[data-turn-expanded="true"]')!;
  vi.spyOn(turnEl, "getBoundingClientRect").mockReturnValue(mockRect(10, 700, 690));
  const wrapper = container.querySelector<HTMLElement>('[class~="group/timeline"]')!;
  return { viewport, wrapper, toggle: screen.getByTestId("turn-toggle") };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("TimelineView pinned titles during auto-follow", () => {
  it("keeps the pinned header through an auto-follow growth pin", async () => {
    const { viewport, wrapper, toggle } = setupStuckBlock();
    expect(toggle.getAttribute("data-stuck")).toBe("false");

    viewport.scrollTop = 1400;
    act(() => { fireEvent.scroll(viewport); });

    expect(wrapper.dataset.fastScrolling).toBeUndefined();
    await waitFor(() => expect(toggle.getAttribute("data-stuck")).toBe("true"));
    expect(wrapper.dataset.fastScrolling).toBeUndefined();
    expect(toggle.className).toContain("backdrop-blur-md");
  });

  it("still releases the titles for fast movement after the reader leaves the bottom", async () => {
    const { viewport, wrapper, toggle } = setupStuckBlock();

    viewport.scrollTop = 1400;
    act(() => { fireEvent.scroll(viewport); });
    viewport.scrollTop = 1000;
    act(() => { fireEvent.scroll(viewport); });

    await waitFor(() => expect(wrapper.dataset.fastScrolling).toBe("true"));
    await waitFor(() => expect(toggle.getAttribute("data-stuck")).toBe("false"));
  });
});
