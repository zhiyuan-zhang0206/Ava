// Ops tab render test — the Grafana embed page: one full-height iframe, the
// ops dashboard (/grafana/d/ava-ops-main), carrying theme + kiosk with no
// window selector or Refresh — the time range and refresh interval are
// Grafana's native timepicker (2026-08-05 user ruling), so the URL carries
// no from/to. The plugin-metrics panels are merged into ava-ops-main by
// scripts/gen_plugin_dashboard.py (2026-08-06), so there is no separate
// plugins iframe. The alerts block renders below at the API default 24h
// window (its own tests cover the list behavior — here it just needs a
// mocked API so it doesn't fetch live).
// Grafana itself is out of scope (jsdom cannot run it) — this pins the URL
// contract the gateway proxy must serve.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { mockResolvedTheme } = vi.hoisted(() => ({
  mockResolvedTheme: vi.fn<() => string>(() => "dark"),
}));
vi.mock("next-themes", () => ({
  useTheme: () => ({ resolvedTheme: mockResolvedTheme(), theme: mockResolvedTheme(), setTheme: vi.fn() }),
}));

import OpsPage, { EMBED_HEIGHT } from "./page";

afterEach(cleanup);

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function frame(): HTMLIFrameElement {
  return screen.getByTestId("ops-embed-frame") as unknown as HTMLIFrameElement;
}

function expectSrc(theme: string) {
  expect(frame().src).toBe(
    `http://localhost:8000/grafana/d/ava-ops-main?theme=${theme}&kiosk`,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

describe("Ops tab (Grafana embed)", () => {
  it("renders the embed iframe with theme + kiosk params and no window controls", () => {
    mockResolvedTheme.mockReturnValue("dark");
    wrap(<OpsPage />);
    expect(frame()).toBeTruthy();
    expectSrc("dark");
    // the window selector + Refresh were removed (2026-08-05) — the time
    // range and refresh are Grafana's native timepicker
    expect(screen.queryByTestId("ops-header-controls")).toBeNull();
    expect(screen.queryByText("Refresh")).toBeNull();
    expect(screen.queryByText("7d")).toBeNull();
  });

  it("renders at full embed height (no inner scrollbar)", () => {
    mockResolvedTheme.mockReturnValue("dark");
    wrap(<OpsPage />);
    expect(frame().style.height).toBe("5770px");
  });

  // The embed height is a fixed constant tied to the dashboard's gridPos
  // layout: Grafana renders each grid row at 30px with 8px gaps, plus a
  // measured 116px of chrome (page.tsx header comment). The dashboard JSON is
  // generated (scripts/gen_plugin_dashboard.py), so a layout change there
  // silently desyncs the constant — this derives the expected height from the
  // JSON and fails loudly when they drift apart (the "recompute after any
  // panel change" rule becomes a structural check instead of a memory note).
  it("embed height matches the dashboard gridPos-derived height", () => {
    const dash = JSON.parse(
      readFileSync(
        join(__dirname, "../../../../../../dashboards/ops/ava-ops-main.json"),
        "utf-8",
      ),
    ) as { panels: { gridPos: { y: number; h: number } }[] };
    const gridRows = Math.max(...dash.panels.map((p) => p.gridPos.y + p.gridPos.h));
    const expected = gridRows * 30 + (gridRows - 1) * 8 + 116;
    expect(EMBED_HEIGHT).toBe(expected);
  });

  it("follows the light theme", () => {
    mockResolvedTheme.mockReturnValue("light");
    wrap(<OpsPage />);
    expectSrc("light");
  });

  it("no longer embeds the plugin-metrics dashboard separately", () => {
    // Plugin panels merged into ava-ops-main (2026-08-06) — the plugins
    // iframe and its heading are gone, one embed only.
    mockResolvedTheme.mockReturnValue("dark");
    wrap(<OpsPage />);
    expect(screen.queryByTestId("plugins-embed-frame")).toBeNull();
    expect(screen.queryByRole("heading", { name: "Plugin metrics" })).toBeNull();
  });

  it("splits Ops into the Metrics (Grafana) + Alerts sub-headings", () => {
    mockResolvedTheme.mockReturnValue("dark");
    wrap(<OpsPage />);
    // Section sub-heading above the embed.
    expect(screen.getByRole("heading", { name: "Metrics (Grafana)" })).toBeTruthy();
    const metrics = document.getElementById("ops-metrics");
    expect(metrics).toBeTruthy();
    // The embed sits inside the Metrics block.
    expect(metrics?.contains(frame())).toBe(true);
    // The alert history lives in its own Insights section now — the ops tab
    // renders the embed only.
    expect(document.getElementById("ops-alerts")).toBeNull();
  });
});
