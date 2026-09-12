// ContextMeter tests — the composer's context readout: a gauge whose fill
// crosses amber past the soft (wind-down) threshold and red past the hard
// (force-compact) ceiling, with tick marks at both, plus a numeric summary.
//
// happy-dom + RTL — vitest globals=false; explicit cleanup.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ContextMeter, ContextMeterGhost, resolveContextMeterWidth } from "./context-meter";

// The ghost reads the gauge-width display setting; stub the settings hook so
// the tests need no QueryClient and can drive the value per case.
const settingsRef = vi.hoisted((): { value: Record<string, unknown> } => ({ value: {} }));
vi.mock("@/lib/use-user-settings", () => ({
  useUserSettings: () => ({ settings: settingsRef.value, setSetting: vi.fn(), isLoading: false }),
}));

afterEach(cleanup);

describe("ContextMeter", () => {
  const full = {
    contextTokens: 300_000,
    maxContextTokens: 1_000_000,
    softCompactTokens: 600_000,
    hardCompactTokens: 800_000,
  };

  it("renders the gauge + numeric summary with soft/hard values", () => {
    render(<ContextMeter {...full} />);
    const text = screen.getByTestId("context-meter").textContent;
    expect(text).toBe("Context: 300.0k/1.00M · soft 600.0k · hard 800.0k");
    // Fill width = occupancy / window; marks at soft/hard percentages.
    expect(screen.getByTestId("context-meter-fill").getAttribute("style")).toContain("width: 30%");
    expect(screen.getByTestId("context-meter-soft-mark").getAttribute("style")).toContain(
      "left: 60%",
    );
    expect(screen.getByTestId("context-meter-hard-mark").getAttribute("style")).toContain(
      "left: 80%",
    );
  });

  it("fill is neutral below the soft threshold", () => {
    render(<ContextMeter {...full} contextTokens={300_000} />);
    expect(screen.getByTestId("context-meter-fill").className).toContain("bg-muted-foreground/60");
  });

  it("fill turns amber in the wind-down band (soft < occupancy <= hard)", () => {
    render(<ContextMeter {...full} contextTokens={700_000} />);
    expect(screen.getByTestId("context-meter-fill").className).toContain("bg-amber-500");
  });

  it("fill turns red past the hard ceiling and clamps the width", () => {
    render(<ContextMeter {...full} contextTokens={1_200_000} />);
    const fill = screen.getByTestId("context-meter-fill");
    expect(fill.className).toContain("bg-destructive");
    expect(fill.getAttribute("style")).toContain("width: 100%"); // clamped, not 120%
  });

  it("omits the gauge + thresholds when the model window is unknown (max 0)", () => {
    render(
      <ContextMeter
        contextTokens={5000}
        maxContextTokens={0}
        softCompactTokens={0}
        hardCompactTokens={0}
      />,
    );
    expect(screen.queryByTestId("context-meter-fill")).toBeNull();
    expect(screen.getByTestId("context-meter").textContent).toBe("Context: 5.0k tokens");
  });

  it("shows the gauge but no marks when the window is known but thresholds are not", () => {
    render(
      <ContextMeter
        contextTokens={26_000}
        maxContextTokens={1_000_000}
        softCompactTokens={0}
        hardCompactTokens={0}
      />,
    );
    expect(screen.getByTestId("context-meter-fill")).toBeTruthy();
    expect(screen.queryByTestId("context-meter-soft-mark")).toBeNull();
    expect(screen.getByTestId("context-meter").textContent).toBe("Context: 26.0k/1.00M tokens");
  });

  it("defaults the gauge track to the compact width when no barWidthClassName is given", () => {
    render(<ContextMeter {...full} />);
    expect(screen.getByRole("img").className).toContain("w-16");
  });

  it("applies a caller-supplied bar width class (the display.context_meter_width tiers)", () => {
    render(<ContextMeter {...full} barWidthClassName="w-48" />);
    const gauge = screen.getByRole("img");
    expect(gauge.className).toContain("w-48");
    expect(gauge.className).not.toContain("w-16");
  });
});

describe("resolveContextMeterWidth", () => {
  it("passes through each valid tier", () => {
    expect(resolveContextMeterWidth("compact")).toBe("compact");
    expect(resolveContextMeterWidth("comfortable")).toBe("comfortable");
    expect(resolveContextMeterWidth("wide")).toBe("wide");
  });

  it("falls back to comfortable for an unset or stale/invalid setting value", () => {
    expect(resolveContextMeterWidth(undefined)).toBe("comfortable");
    expect(resolveContextMeterWidth(null)).toBe("comfortable");
    expect(resolveContextMeterWidth("huge")).toBe("comfortable");
  });
});

describe("ContextMeterGhost", () => {
  it("renders a decorative pulsing track in the meter's geometry", () => {
    settingsRef.value = {};
    render(<ContextMeterGhost />);
    const ghost = screen.getByTestId("context-meter-ghost");
    expect(ghost.getAttribute("aria-hidden")).toBe("true");
    const track = ghost.querySelector("span");
    expect(track?.className).toContain("h-1.5");
    expect(track?.className).toContain("animate-pulse");
    // Unset setting -> the comfortable tier (w-32), the meter default.
    expect(track?.className).toContain("w-32");
  });

  it("follows the display.context_meter_width setting", () => {
    settingsRef.value = { "display.context_meter_width": "wide" };
    render(<ContextMeterGhost />);
    const track = screen.getByTestId("context-meter-ghost").querySelector("span");
    expect(track?.className).toContain("w-48");
  });
});
