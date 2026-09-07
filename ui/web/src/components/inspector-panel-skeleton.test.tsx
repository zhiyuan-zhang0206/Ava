import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  InspectorPanelSkeleton,
  LiveSectionsSkeleton,
  SectionSkeleton,
  WindowedSectionsSkeleton,
} from "./inspector-panel-skeleton";

const isLargeMock = vi.fn<() => boolean>(() => true);
vi.mock("@/lib/breakpoint", () => ({
  useBreakpoint: () => ({
    tier: isLargeMock() ? "xl" : "xs",
    isNarrow: !isLargeMock(),
    isLarge: isLargeMock(),
  }),
}));

describe("InspectorPanelSkeleton", () => {
  beforeEach(() => {
    isLargeMock.mockReturnValue(true);
  });

  it("renders the skeleton header, live sections skeleton, and windowed sections skeleton on desktop", () => {
    const { container } = render(<InspectorPanelSkeleton />);

    const aside = screen.getByTestId("inspector-panel-skeleton");
    expect(aside).toBeTruthy();
    expect(screen.getByText("Inspector")).toBeTruthy();
    expect(screen.getByLabelText("Persistent shells loading")).toBeTruthy();
    expect(screen.getByLabelText("Liveness loading")).toBeTruthy();
    expect(screen.getByLabelText("Configuration overlay loading")).toBeTruthy();
    expect(screen.getByLabelText("Cost loading")).toBeTruthy();
    expect(screen.getByLabelText("Activity loading")).toBeTruthy();
    expect(container.querySelector(".animate-pulse")).toBeTruthy();
  });

  it("renders full-screen overlay container on mobile", () => {
    isLargeMock.mockReturnValue(false);
    render(<InspectorPanelSkeleton />);

    const aside = screen.getByTestId("inspector-panel-skeleton");
    expect(aside.parentElement?.className.split(" ")).toEqual(
      expect.arrayContaining(["fixed", "inset-0", "z-50", "flex"]),
    );
    expect(document.querySelector('div[aria-hidden="true"]')).toBeTruthy();
  });
});

describe("Individual Skeletons", () => {
  it("SectionSkeleton renders the given title in aria-label and row count", () => {
    render(<SectionSkeleton title="Test Section" rows={3} />);
    const section = screen.getByLabelText("Test Section loading");
    expect(section).toBeTruthy();
    const rows = section.querySelectorAll(".grid > div");
    expect(rows.length).toBe(3);
  });

  it("LiveSectionsSkeleton renders shells, liveness, and config overlay", () => {
    render(<LiveSectionsSkeleton />);
    expect(screen.getByLabelText("Persistent shells loading")).toBeTruthy();
    expect(screen.getByLabelText("Liveness loading")).toBeTruthy();
    expect(screen.getByLabelText("Configuration overlay loading")).toBeTruthy();
  });

  it("WindowedSectionsSkeleton renders cost and activity", () => {
    render(<WindowedSectionsSkeleton />);
    expect(screen.getByLabelText("Cost loading")).toBeTruthy();
    expect(screen.getByLabelText("Activity loading")).toBeTruthy();
  });
});
