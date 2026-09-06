// /insights shell tests — the vertical observability layout: the Status +
// Ops sections render at once (Ctrl-F / deep-link reachable) in order, the
// left nav lists them under an "Insights sections" label and jumps to an
// anchor, and the Control-only sections (Config, Guide, …) are absent. Section
// bodies are mocked to lightweight stubs so this covers only the shell wiring
// (each body has its own test file), and keeps the heavy deps (api) out.
// The Metrics section was retired 2026-08-04 (route now redirects to Grafana).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Stubs carry the section element ids (their bodies are covered by each
// page's own test file) so the nav's sub-anchor jumps have a real target.
vi.mock("@/app/insights/status/page", () => ({
  default: () => (
    <div id="status-services">
      STATUS_BODY
      <div id="status-gateway" />
    </div>
  ),
}));
vi.mock("@/app/insights/ops/page", () => ({ default: () => <div id="ops-metrics">OPS_BODY</div> }));
vi.mock("@/components/ops/alerts-section", () => ({ default: () => <div id="alerts-section">ALERTS_BODY</div> }));

import { INSIGHTS_SECTIONS } from "@/app/control/_sections";

import InsightsPage from "./page";

afterEach(cleanup);

let scrolledIds: string[];

beforeEach(() => {
  vi.restoreAllMocks();
  window.location.hash = "";
  // happy-dom doesn't implement scrollIntoView; the nav + hash effect call it.
  scrolledIds = [];
  Element.prototype.scrollIntoView = vi.fn(function (this: Element) {
    scrolledIds.push(this.id);
  });
});

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("InsightsPage shell", () => {
  it("renders a back-to-conversation link and the Insights page title", () => {
    wrap(<InsightsPage />);
    expect(screen.getByRole("heading", { name: "Insights" })).toBeTruthy();
    expect(screen.getByRole("link", { name: /back to agents/i })).toBeTruthy();
  });

  it("keeps the observation content in the UI sans family", () => {
    wrap(<InsightsPage />);
    const content = document.getElementById("insights-scroll")?.firstElementChild;

    expect(content?.classList.contains("font-sans")).toBe(true);
    expect(content?.classList.contains("font-mono")).toBe(false);
  });

  it("renders the Status + Ops section headings + anchors", () => {
    wrap(<InsightsPage />);
    for (const section of INSIGHTS_SECTIONS) {
      expect(screen.getByRole("heading", { name: section.label })).toBeTruthy();
      expect(document.getElementById(section.id)).toBeTruthy();
    }
    expect(screen.getByText("STATUS_BODY")).toBeTruthy();
    expect(screen.getByText("OPS_BODY")).toBeTruthy();
  });

  it("composes Status before Ops", () => {
    const { container } = wrap(<InsightsPage />);
    const text = container.innerHTML;
    const status = text.indexOf("STATUS_BODY");
    const ops = text.indexOf("OPS_BODY");
    expect(status).toBeGreaterThanOrEqual(0);
    expect(status).toBeLessThan(ops);
  });

  it("requires an agent id before exposing the run timeline link", () => {
    wrap(<InsightsPage />);

    expect(screen.getByRole("button", { name: "Open run timeline" }).hasAttribute("disabled")).toBe(true);
    fireEvent.change(screen.getByRole("spinbutton", { name: "Agent ID" }), {
      target: { value: "405" },
    });
    expect(screen.getByRole("link", { name: "Open run timeline" }).getAttribute("href")).toBe(
      "/insights/run/405",
    );
  });

  it("does NOT render the Control-only sections (they live on /control)", () => {
    wrap(<InsightsPage />);
    for (const label of ["Guide", "Config", "Presets", "Display", "Plugins", "MCP", "Skills", "Schedules"]) {
      expect(screen.queryByRole("heading", { name: label })).toBeNull();
    }
  });

  it("nav lists every Insights section under the label and jumps on click", () => {
    wrap(<InsightsPage />);
    const nav = screen.getByRole("navigation", { name: /insights sections/i });
    for (const section of INSIGHTS_SECTIONS) {
      expect(within(nav).getAllByText(section.label).length).toBeGreaterThan(0);
    }
    const opsEl = document.getElementById("ops");
    const spy = vi.spyOn(opsEl as Element, "scrollIntoView");
    fireEvent.click(within(nav).getByRole("button", { name: "Ops" }));
    expect(spy).toHaveBeenCalled();
    expect(window.location.hash).toBe("#ops");
  });

  it("nav lists every section sub-anchor and jumps to it", () => {
    wrap(<InsightsPage />);
    const nav = screen.getByRole("navigation", { name: /insights sections/i });
    // Every sub-anchor (Status' blocks, Ops' Metrics/Alerts) appears in the nav.
    for (const section of INSIGHTS_SECTIONS) {
      for (const sub of section.subs ?? []) {
        expect(within(nav).getByRole("button", { name: sub.label })).toBeTruthy();
      }
    }
    // Clicking a sub link jumps to the sub element (stubbed with its id here)
    // and writes the sub hash, not the parent section's.
    const metricsEl = document.getElementById("ops-metrics");
    expect(metricsEl).toBeTruthy();
    const spy = vi.spyOn(metricsEl as Element, "scrollIntoView");
    fireEvent.click(within(nav).getByRole("button", { name: "Metrics (Grafana)" }));
    expect(spy).toHaveBeenCalled();
    expect(window.location.hash).toBe("#ops-metrics");
  });

  it.each([
    ["resources", "status"],
    ["gateway-daemons", "status-gateway"],
  ])("forwards retired Status %s deep links to %s", (anchorSuffix, targetId) => {
    window.location.hash = `#status-${anchorSuffix}`;
    wrap(<InsightsPage />);
    expect(scrolledIds).toContain(targetId);
  });
});
