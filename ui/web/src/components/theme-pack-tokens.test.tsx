// ThemePackTokens — a declared token pack reaches the root element.
//
// The assertions read the real custom properties off <html> after render, so
// they fail if the wiring stops applying (or stops cleaning up) rather than
// re-stating what the component was written to do.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ThemePackTokens } from "@/components/theme-pack-tokens";
import { api } from "@/lib/api";
import { resetMockSettings, setMockSetting } from "@/test-support/user-settings-mock";

vi.mock("@/lib/api", () => ({
  api: { getUiContributions: vi.fn() },
}));
vi.mock("@/lib/use-user-settings", () => import("@/test-support/user-settings-mock"));

const SOLARIZED = {
  plugin: "skins",
  name: "solarized",
  tokens: { "--background": "oklch(0.99 0.02 90)", "--primary": "#268bd2" },
};
const MIDNIGHT = {
  plugin: "skins",
  name: "midnight",
  tokens: { "--background": "oklch(0.15 0.02 260)" },
};

function renderApplier() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ThemePackTokens />
    </QueryClientProvider>,
  );
}

/** The value a component's `var(--x)` would resolve against: read through
 *  computed style, not off the inline style map, so the assertion is about the
 *  token being live in the cascade rather than about the write having happened. */
function token(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

beforeEach(() => {
  vi.clearAllMocks();
  resetMockSettings();
  document.documentElement.removeAttribute("style");
  vi.mocked(api.getUiContributions).mockResolvedValue({ themes: [SOLARIZED, MIDNIGHT] });
});

afterEach(cleanup);

describe("ThemePackTokens", () => {
  it("applies the selected pack's tokens to the root element", async () => {
    resetMockSettings({ "display.theme_pack": "skins/solarized" });

    renderApplier();

    await waitFor(() => expect(token("--background")).toBe("oklch(0.99 0.02 90)"));
    expect(token("--primary")).toBe("#268bd2");
  });

  it("applies nothing while the choice is the console's own palette", async () => {
    renderApplier();

    await waitFor(() => expect(api.getUiContributions).toHaveBeenCalled());
    expect(token("--background")).toBe("");
    expect(token("--primary")).toBe("");
  });

  it("drops the previous pack's tokens when the choice changes", async () => {
    resetMockSettings({ "display.theme_pack": "skins/solarized" });
    renderApplier();
    await waitFor(() => expect(token("--primary")).toBe("#268bd2"));

    setMockSetting("display.theme_pack", "skins/midnight");

    // midnight names only --background, so solarized's --primary must go —
    // otherwise a switch leaves a blend of the two packs on screen.
    await waitFor(() => expect(token("--background")).toBe("oklch(0.15 0.02 260)"));
    expect(token("--primary")).toBe("");
  });

  it("clears the tokens when the choice goes back to default", async () => {
    resetMockSettings({ "display.theme_pack": "skins/solarized" });
    renderApplier();
    await waitFor(() => expect(token("--background")).toBe("oklch(0.99 0.02 90)"));

    setMockSetting("display.theme_pack", null);

    await waitFor(() => expect(token("--background")).toBe(""));
  });

  it("falls back to the console palette when the stored pack is gone", async () => {
    // The plugin that shipped it was disabled or uninstalled since the choice
    // was made: no tokens, rather than whatever else happens to be installed.
    resetMockSettings({ "display.theme_pack": "removed/gone" });

    renderApplier();

    await waitFor(() => expect(api.getUiContributions).toHaveBeenCalled());
    expect(token("--background")).toBe("");
  });
});
