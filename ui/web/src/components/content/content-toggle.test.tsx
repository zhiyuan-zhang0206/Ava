import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { mockSetSettingCalls, resetMockSettings, setMockLoading, setMockSetting } from "@/test-support/user-settings-mock";

import { ContentToggle } from "./content-toggle";

vi.mock("@/lib/state/use-user-settings", () => import("@/test-support/user-settings-mock"));

beforeEach(() => resetMockSettings());
afterEach(cleanup);

describe("ContentToggle", () => {
  it("summarizes the current details mode and links to its setting", () => {
    render(<ContentToggle />);
    const link = screen.getByRole("link", { name: "Details: None" });
    expect(link.getAttribute("href")).toBe("/control#display-timeline");
    expect(screen.getByTestId("details-summary-mode").textContent).toBe("None");
  });

  it("stays keyboard reachable", () => {
    render(<ContentToggle />);
    const link = screen.getByTestId("details-summary");
    expect(link.tagName).toBe("A");
    expect(link.tabIndex).toBe(0);
  });

  it("reads a stored mode back into the summary", () => {
    setMockSetting("display.expand_runs_mode", "last");
    render(<ContentToggle />);
    expect(screen.getByTestId("details-summary-mode").textContent).toBe("Last");
  });

  it("renders nothing while the stored mode is still unknown", () => {
    setMockLoading(true);
    render(<ContentToggle />);
    expect(screen.queryByTestId("details-summary")).toBeNull();
  });

  it("offers no selector of its own in the composer", () => {
    render(<ContentToggle />);
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(mockSetSettingCalls()).toHaveLength(0);
  });
});
