// /control shell tests — the vertical layout: every section's heading + anchor
// renders at once (Ctrl-F / deep-link reachable), the sections compose in order,
// the left nav jumps to an anchor, and the observability sections (Status +
// Metrics) are NOT here — they moved to /insights. Section bodies are mocked to
// lightweight stubs so this test covers only the shell wiring (each body has its
// own test file); that also keeps the heavy per-section deps (router, api) out.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Control's hash effect forwards migrated anchors to /insights via router.replace.
const { replaceSpy } = vi.hoisted(() => ({ replaceSpy: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: replaceSpy }) }));

vi.mock("@/app/control/guide/page", () => ({ default: () => <div>GUIDE_BODY</div> }));
vi.mock("@/app/control/config/page", () => ({ default: () => <div>CONFIG_BODY</div> }));
vi.mock("@/app/control/presets/page", () => ({ default: () => <div>PRESETS_BODY</div> }));
vi.mock("@/app/control/display/page", () => ({ default: () => <div>DISPLAY_BODY</div> }));
vi.mock("@/app/control/schedules/page", () => ({ default: () => <div>SCHEDULES_BODY</div> }));
vi.mock("@/app/control/skills/page", () => ({ default: () => <div>SKILLS_BODY</div> }));
vi.mock("@/app/control/inventory/page", () => ({
  PluginsInventory: () => <div>PLUGINS_BODY</div>,
  McpInventory: () => <div>MCP_BODY</div>,
}));
// The Plugins section also carries plugin-contributed page links, which read
// the contributions endpoint. Kept real (it renders nothing when no plugin
// declares one) with the api boundary mocked, like every other test here.
vi.mock("@/lib/api", () => ({
  api: { getUiContributions: vi.fn().mockResolvedValue({ themes: [], nav: [] }) },
  assetUrl: (p: string) => p,
}));

import ControlPage from "./page";
import { CONTROL_SCROLL_ID, CONTROL_SECTIONS } from "./_sections";

afterEach(cleanup);

beforeEach(() => {
  vi.restoreAllMocks();
  replaceSpy.mockClear();
  window.location.hash = "";
  // happy-dom doesn't implement scrollIntoView; the nav + hash effect call it.
  Element.prototype.scrollIntoView = vi.fn();
});

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("ControlPage shell", () => {
  it("renders a back-to-conversation link and the Control page title", () => {
    wrap(<ControlPage />);
    expect(screen.getByRole("heading", { name: "Control" })).toBeTruthy();
    expect(screen.getByRole("link", { name: /back to agents/i })).toBeTruthy();
  });

  it("uses the wider readable content column without forcing prose monospace", () => {
    wrap(<ControlPage />);
    const content = document.getElementById(CONTROL_SCROLL_ID)?.firstElementChild;

    expect(content?.classList.contains("max-w-6xl")).toBe(true);
    expect(content?.classList.contains("font-mono")).toBe(false);
  });

  it("renders every section heading + anchor at once (Ctrl-F / deep-link reachable)", () => {
    wrap(<ControlPage />);
    for (const section of CONTROL_SECTIONS) {
      // Heading always in the DOM, regardless of scroll position…
      expect(screen.getByRole("heading", { name: section.label })).toBeTruthy();
      // …and its anchor id resolves for URL hashes.
      expect(document.getElementById(section.id)).toBeTruthy();
    }
  });

  it("composes the section bodies in order", () => {
    const { container } = wrap(<ControlPage />);
    const markers = [
      "GUIDE_BODY",
      "CONFIG_BODY",
      "PRESETS_BODY",
      "DISPLAY_BODY",
      "PLUGINS_BODY",
      "MCP_BODY",
      "SKILLS_BODY",
      "SCHEDULES_BODY",
    ];
    const text = container.innerHTML;
    const positions = markers.map((m) => text.indexOf(m));
    expect(positions.every((p) => p >= 0)).toBe(true);
    // Strictly increasing → rendered top-to-bottom in the expected order.
    expect(positions).toEqual([...positions].sort((a, b) => a - b));
  });

  it("nav lists every section and jumps to its anchor on click", () => {
    wrap(<ControlPage />);
    const nav = screen.getByRole("navigation", { name: /control sections/i });
    // Every section is a nav entry (scope to the nav — labels also appear as
    // section headings on the page).
    for (const section of CONTROL_SECTIONS) {
      expect(within(nav).getAllByText(section.label).length).toBeGreaterThan(0);
    }
    const skillsEl = document.getElementById("skills");
    const spy = vi.spyOn(skillsEl as Element, "scrollIntoView");
    fireEvent.click(within(nav).getByRole("button", { name: "Skills" }));
    expect(spy).toHaveBeenCalled();
    expect(window.location.hash).toBe("#skills");
  });

  it("does NOT render the Status, Ops or Metrics sections (moved to /insights)", () => {
    wrap(<ControlPage />);
    for (const label of ["Status", "Ops", "Metrics"]) {
      expect(screen.queryByRole("heading", { name: label })).toBeNull();
    }
    expect(document.getElementById("status")).toBeNull();
    expect(document.getElementById("ops")).toBeNull();
    expect(document.getElementById("metrics")).toBeNull();
    // The nav rail doesn't list them either.
    const nav = screen.getByRole("navigation", { name: /control sections/i });
    for (const label of ["Status", "Ops", "Metrics"]) {
      expect(within(nav).queryByRole("button", { name: label })).toBeNull();
    }
  });
});

describe("ControlPage deep-link forwarding", () => {
  // Status + retired Metrics anchors moved to /insights; their old /control#… deep
  // links redirect there so a bookmark still lands on the right section.
  it.each(["#status", "#ops"])(
    "forwards a migrated %s deep link to /insights",
    (hash) => {
      window.location.hash = hash;
      wrap(<ControlPage />);
      expect(replaceSpy).toHaveBeenCalledWith(`/insights${hash}`);
    },
  );

  // The Metrics page was retired 2026-08-04 (replaced by Grafana): its old
  // /control#metrics / #metrics-* deep links now land on the Ops section,
  // which embeds the dashboard that replaced them.
  it.each(["#metrics", "#metrics-per-agents", "#metrics-sdk-usage"])(
    "forwards a retired %s Metrics deep link to /insights#ops",
    (hash) => {
      window.location.hash = hash;
      wrap(<ControlPage />);
      expect(replaceSpy).toHaveBeenCalledWith("/insights#ops");
    },
  );

  it("does NOT forward a Control-owned anchor (#config stays put)", () => {
    window.location.hash = "#config";
    wrap(<ControlPage />);
    expect(replaceSpy).not.toHaveBeenCalled();
  });
});
