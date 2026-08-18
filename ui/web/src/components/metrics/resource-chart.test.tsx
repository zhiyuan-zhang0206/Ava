/**
 * Unit tests for ResourceChart + SparkLine (pure rendering, no browser APIs).
 */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ResourceChart } from "@/components/metrics/resource-chart";
import type { ResourcePoint } from "@/lib/types";

afterEach(cleanup);

function makePoint(overrides: Partial<ResourcePoint> = {}): ResourcePoint {
  return {
    ts: 1000 + (overrides.ts ?? 0),
    cpu_pct: overrides.cpu_pct ?? 45,
    mem_used_gb: overrides.mem_used_gb ?? 8.5,
    mem_total_gb: overrides.mem_total_gb ?? 16,
    mem_pct: overrides.mem_pct ?? 53,
    disk_used_gb: overrides.disk_used_gb ?? 120,
    disk_total_gb: overrides.disk_total_gb ?? 500,
    disk_pct: overrides.disk_pct ?? 24,
  };
}

describe("ResourceChart", () => {
  it("renders empty-state message when history is empty", () => {
    render(<ResourceChart history={[]} />);
    expect(screen.getByText("No resource data")).toBeTruthy();
  });

  it("renders CPU / Mem / Disk labels with a single data point", () => {
    render(<ResourceChart history={[makePoint()]} />);
    expect(screen.getByText("CPU")).toBeTruthy();
    expect(screen.getByText("Memory")).toBeTruthy();
    expect(screen.getByText("Disk")).toBeTruthy();
  });

  it("shows the latest percentage values as labels", () => {
    render(
      <ResourceChart
        history={[
          makePoint({ cpu_pct: 10, mem_pct: 50, disk_pct: 90 }),
          makePoint({ cpu_pct: 75, mem_pct: 60, disk_pct: 33 }),
        ]}
      />,
    );
    expect(screen.getByText("75%")).toBeTruthy();
    expect(screen.getByText("60%")).toBeTruthy();
    expect(screen.getByText("33%")).toBeTruthy();
  });

  it("shows memory used/total in GB", () => {
    render(
      <ResourceChart
        history={[
          makePoint({ mem_used_gb: 5.1, mem_total_gb: 16 }),
        ]}
      />,
    );
    expect(screen.getByText("5.1/16GB")).toBeTruthy();
  });

  it("shows disk used/total in GB", () => {
    render(
      <ResourceChart
        history={[
          makePoint({ disk_used_gb: 200, disk_total_gb: 1000 }),
        ]}
      />,
    );
    expect(screen.getByText("200/1000GB")).toBeTruthy();
  });

  it("renders SVG elements for sparklines with >=2 points", () => {
    const { container } = render(
      <ResourceChart
        history={[
          makePoint({ ts: 100, cpu_pct: 10 }),
          makePoint({ ts: 200, cpu_pct: 50 }),
          makePoint({ ts: 300, cpu_pct: 30 }),
        ]}
      />,
    );
    const paths = container.querySelectorAll("path");
    expect(paths.length).toBeGreaterThan(0);
  });

  it("renders compact layout when compact=true", () => {
    const { container: normal } = render(
      <ResourceChart history={[makePoint()]} compact={false} />,
    );
    const { container: compact } = render(
      <ResourceChart history={[makePoint()]} compact={true} />,
    );
    expect(normal.querySelector("svg")).toBeTruthy();
    expect(compact.querySelector("svg")).toBeTruthy();
  });

  it("renders disk as a static metric with no sparkline (CPU + memory only get SVGs)", () => {
    const { container } = render(
      <ResourceChart
        history={[
          makePoint({ ts: 100, disk_pct: 42 }),
          makePoint({ ts: 200, disk_pct: 42 }),
        ]}
      />,
    );
    // Two sparklines (CPU, memory); disk is a plain number, not a chart.
    expect(container.querySelectorAll("svg").length).toBe(2);
    expect(screen.getByText("42%")).toBeTruthy();
  });

  it("plots a low reading near the bottom on an absolute 0-100 scale (not auto-fit)", () => {
    // A flat low CPU trace must sit in the LOWER half of the plot. With the old
    // auto-fit scale it would fill to the top regardless of value.
    const { container } = render(
      <ResourceChart
        history={[
          makePoint({ ts: 100, cpu_pct: 5 }),
          makePoint({ ts: 200, cpu_pct: 5 }),
        ]}
      />,
    );
    // First stroke path (fill="none") is the CPU line.
    const cpuLine = container.querySelector('path[fill="none"]');
    const d = cpuLine?.getAttribute("d") ?? "";
    const ys = [...d.matchAll(/[ML][\d.]+,([\d.]+)/g)].map((mm) => parseFloat(mm[1]));
    expect(ys.length).toBeGreaterThan(0);
    const height = 48; // non-compact default
    for (const y of ys) expect(y).toBeGreaterThan(height / 2);
  });

  it("uses one decimal for values < 10", () => {
    render(<ResourceChart history={[makePoint({ mem_used_gb: 3.456, mem_total_gb: 16 })]} />);
    expect(screen.getByText("3.5/16GB")).toBeTruthy();
  });

  it("rounds to integer for values >= 10", () => {
    render(<ResourceChart history={[makePoint({ disk_used_gb: 123.7, disk_total_gb: 500 })]} />);
    expect(screen.getByText("124/500GB")).toBeTruthy();
  });
});
