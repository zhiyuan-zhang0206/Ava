import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { useMediaQuery } = vi.hoisted(() => ({ useMediaQuery: vi.fn(() => true) }));
vi.mock("@/lib/layout/use-media-query", () => ({ useMediaQuery }));

import {
  RUN_TIMELINE_SPLIT_LAYOUT_ID,
  RunTimelineWorkspace,
  SIDE_PANEL_MAX,
  SIDE_PANEL_MIN,
} from "./run-timeline-workspace";

beforeEach(() => {
  localStorage.clear();
  useMediaQuery.mockReturnValue(true);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("RunTimelineWorkspace", () => {
  it("splits the page with a keyboard-reachable, labelled separator on a wide viewport", async () => {
    render(<RunTimelineWorkspace main={<p>main</p>} side={<p>side</p>} />);

    const handle = await screen.findByRole("separator", { name: "Resize the details panel" });
    expect(handle.getAttribute("aria-orientation")).toBe("vertical");
    expect(handle.getAttribute("tabindex")).toBe("0");
    expect(screen.getByTestId("run-timeline-main").textContent).toBe("main");
    expect(screen.getByTestId("run-timeline-reader").textContent).toBe("side");
  });

  it("bounds the side panel in pixels", () => {
    expect(SIDE_PANEL_MIN).toBe("300px");
    expect(SIDE_PANEL_MAX).toBe("760px");
    expect(RUN_TIMELINE_SPLIT_LAYOUT_ID).toBe("ava.run-timeline.split");
  });

  it("stacks the details under the timeline on a narrow viewport, with no separator", async () => {
    useMediaQuery.mockReturnValue(false);
    render(<RunTimelineWorkspace main={<p>main</p>} side={<p>side</p>} />);

    expect(await screen.findByTestId("run-timeline-reader")).toBeTruthy();
    expect(screen.queryByRole("separator")).toBeNull();
  });

  it("still renders when localStorage throws on every access", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    render(<RunTimelineWorkspace main={<p>main</p>} side={<p>side</p>} />);

    expect(await screen.findByRole("separator")).toBeTruthy();
  });
});
