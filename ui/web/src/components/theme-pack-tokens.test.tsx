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

// next-themes resolves "system" to a concrete mode; the component keys the
// half it applies on that, so the tests drive it directly.
let mockMode: string | undefined = "light";
vi.mock("next-themes", () => ({
  useTheme: () => ({ resolvedTheme: mockMode, theme: mockMode, setTheme: vi.fn() }),
}));

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
// A pack with both halves: `tokens` is the light one, `dark_tokens` the dark.
const DUSK = {
  plugin: "skins",
  name: "dusk",
  tokens: { "--background": "#fdf6e3", "--primary": "#268bd2" },
  dark_tokens: { "--background": "#002b36", "--primary": "#93a1a1" },
};
// Halves that name DIFFERENT token sets — nothing requires them to match, so
// the dark half here overrides only the background.
const ASYM = {
  plugin: "skins",
  name: "asym",
  tokens: { "--background": "#fdf6e3", "--primary": "#268bd2" },
  dark_tokens: { "--background": "#002b36" },
};

function renderApplier() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // A builder, not a stored element: re-rendering the identical element
  // reference lets React bail out of the subtree, so a test that flips the
  // resolved mode would observe nothing. Each call makes a fresh element.
  const build = () => (
    <QueryClientProvider client={qc}>
      <ThemePackTokens />
    </QueryClientProvider>
  );
  return { ...render(build()), build };
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
  mockMode = "light";
  document.documentElement.removeAttribute("style");
  vi.mocked(api.getUiContributions).mockResolvedValue({
    themes: [SOLARIZED, MIDNIGHT, DUSK, ASYM],
    nav: [],
  });
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

  it("applies the dark half when the resolved mode is dark", async () => {
    mockMode = "dark";
    resetMockSettings({ "display.theme_pack": "skins/dusk" });

    renderApplier();

    await waitFor(() => expect(token("--background")).toBe("#002b36"));
    expect(token("--primary")).toBe("#93a1a1");
  });

  it("swaps halves when the mode flips, leaving none of the outgoing one", async () => {
    resetMockSettings({ "display.theme_pack": "skins/dusk" });
    const { rerender, build } = renderApplier();
    await waitFor(() => expect(token("--background")).toBe("#fdf6e3"));

    mockMode = "dark";
    rerender(build());

    // The whole point of the field: the mode toggle has to still do something
    // for the colors the pack sets.
    await waitFor(() => expect(token("--background")).toBe("#002b36"));
    expect(token("--primary")).toBe("#93a1a1");
  });

  it("drops a light-only token when the dark half does not name it", async () => {
    // The halves need not name the same tokens. Switching to the one that
    // omits --primary must REMOVE it, not leave the light value behind
    // blended into the dark palette.
    resetMockSettings({ "display.theme_pack": "skins/asym" });
    const { rerender, build } = renderApplier();
    await waitFor(() => expect(token("--primary")).toBe("#268bd2"));

    mockMode = "dark";
    rerender(build());

    await waitFor(() => expect(token("--background")).toBe("#002b36"));
    expect(token("--primary")).toBe("");
  });

  it("pins both modes when the pack declares no dark half", async () => {
    // Omitting darkTokens is a declaration, not an oversight — solarized keeps
    // its light values in dark mode, which is why the picker labels it.
    mockMode = "dark";
    resetMockSettings({ "display.theme_pack": "skins/solarized" });

    renderApplier();

    await waitFor(() => expect(token("--background")).toBe("oklch(0.99 0.02 90)"));
    expect(token("--primary")).toBe("#268bd2");
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
