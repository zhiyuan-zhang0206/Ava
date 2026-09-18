import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { runTimelineLabels } from "@/test-support/run-timeline-labels";

import { StripLegend } from "./run-timeline-legend";
import { STRIP_LEGEND_CATEGORIES } from "./strip-categories";

const labels = runTimelineLabels();

describe("StripLegend", () => {
  it("renders the nine category rows plus the hint", () => {
    render(<StripLegend active={null} labels={labels} onToggle={vi.fn()} />);
    expect(screen.getAllByRole("button")).toHaveLength(STRIP_LEGEND_CATEGORIES.length);
    for (const name of ["thinking", "text output", "system note", "agent inbound", "compact / system"]) {
      expect(screen.getByRole("button", { name })).not.toBeNull();
    }
    expect(screen.getByText(labels.legendHint)).not.toBeNull();
  });

  it("reports category toggles", () => {
    const onToggle = vi.fn();
    render(<StripLegend active={null} labels={labels} onToggle={onToggle} />);
    fireEvent.click(screen.getByRole("button", { name: "tool call" }));
    expect(onToggle).toHaveBeenCalledWith("call");
  });

  it("marks only the active category pressed", () => {
    const { rerender } = render(<StripLegend active={null} labels={labels} onToggle={vi.fn()} />);
    for (const button of screen.getAllByRole("button")) {
      expect(button.getAttribute("aria-pressed")).toBe("false");
    }
    rerender(<StripLegend active="sys" labels={labels} onToggle={vi.fn()} />);
    expect(screen.getByRole("button", { name: "compact / system" }).getAttribute("aria-pressed")).toBe(
      "true",
    );
    const pressed = screen
      .getAllByRole("button")
      .filter((button) => button.getAttribute("aria-pressed") === "true");
    expect(pressed).toHaveLength(1);
  });
});
