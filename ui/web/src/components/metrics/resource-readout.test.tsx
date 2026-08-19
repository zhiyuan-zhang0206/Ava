/**
 * Unit tests for ResourceReadout (pure rendering, no browser APIs).
 */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ResourceReadout } from "@/components/metrics/resource-readout";
import type { ResourceSample } from "@/lib/types";

afterEach(cleanup);

function makeSample(overrides: Partial<ResourceSample> = {}): ResourceSample {
  return {
    ts: 1000,
    cpu_pct: 45,
    mem_used_gb: 8.5,
    mem_total_gb: 16,
    mem_pct: 53,
    disk_used_gb: 120,
    disk_total_gb: 500,
    disk_pct: 24,
    ...overrides,
  };
}

describe("ResourceReadout", () => {
  it("labels all three axes", () => {
    render(<ResourceReadout sample={makeSample()} />);
    expect(screen.getByText("CPU")).toBeTruthy();
    expect(screen.getByText("Memory")).toBeTruthy();
    expect(screen.getByText("Disk")).toBeTruthy();
  });

  it("rounds each percentage to a whole number", () => {
    render(<ResourceReadout sample={makeSample({ cpu_pct: 45.6, mem_pct: 53.2 })} />);
    expect(screen.getByText("46%")).toBeTruthy();
    expect(screen.getByText("53%")).toBeTruthy();
    expect(screen.getByText("24%")).toBeTruthy();
  });

  it("shows used/total GB beside memory and disk", () => {
    render(<ResourceReadout sample={makeSample()} />);
    // Under 10 GB keeps one decimal; at or above it rounds to whole GB.
    expect(screen.getByText("8.5/16GB")).toBeTruthy();
    expect(screen.getByText("120/500GB")).toBeTruthy();
  });
});
