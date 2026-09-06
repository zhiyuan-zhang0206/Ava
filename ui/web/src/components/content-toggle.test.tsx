// ContentToggle: compound Details button renders + select sets mode.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { mockSetSettingCalls, resetMockSettings } from "@/test-support/user-settings-mock";

import { ContentToggle } from "./content-toggle";

vi.mock("@/lib/use-user-settings", () => import("@/test-support/user-settings-mock"));

beforeEach(() => resetMockSettings());
afterEach(cleanup);

describe("ContentToggle", () => {
  it("renders 'Details' label and All/Last/None select", () => {
    render(<ContentToggle />);
    expect(screen.getByText("Details")).toBeTruthy();
    const select = screen.getByRole("combobox", { name: "Details level" });
    expect(select).toBeTruthy();
    expect((select as HTMLSelectElement).value).toBe("all");
    // All three options exist
    expect(screen.getByText("All")).toBeTruthy();
    expect(screen.getByText("Last")).toBeTruthy();
    expect(screen.getByText("None")).toBeTruthy();
  });

  it("uses the UI sans family and named small-size token", () => {
    render(<ContentToggle />);
    const label = screen.getByText("Details");
    const select = screen.getByRole("combobox", { name: "Details level" });
    for (const element of [label, select]) {
      expect(element.classList).toContain("font-sans");
      expect(element.classList).toContain("text-xs");
      expect(element.classList).not.toContain("font-mono");
    }
  });

  it("defaults to 'all' selected", () => {
    render(<ContentToggle />);
    const select = screen.getByRole("combobox", { name: "Details level" });
    expect((select as HTMLSelectElement).value).toBe("all");
  });

  it("selecting Last writes the setting", () => {
    render(<ContentToggle />);
    const select = screen.getByRole("combobox", { name: "Details level" });
    fireEvent.change(select, { target: { value: "last" } });
    expect((select as HTMLSelectElement).value).toBe("last");
    expect(mockSetSettingCalls()).toContainEqual({
      key: "display.expand_runs_mode",
      value: "last",
    });
  });

  it("selecting None writes the setting", () => {
    render(<ContentToggle />);
    const select = screen.getByRole("combobox", { name: "Details level" });
    fireEvent.change(select, { target: { value: "none" } });
    expect((select as HTMLSelectElement).value).toBe("none");
    expect(mockSetSettingCalls()).toContainEqual({
      key: "display.expand_runs_mode",
      value: "none",
    });
  });
});
