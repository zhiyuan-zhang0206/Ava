// Display settings page tests — basic render verification.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import DisplaySettingsPage from "./page";

vi.mock("@/lib/api", () => ({
  api: {
    getSettings: vi.fn(),
    putSetting: vi.fn(),
    getModels: vi.fn(),
  },
}));

// Viewport breakpoint for the timeline-width slider (task #805): desktop by
// default; the narrow-viewport test flips it. R4 layer 4: the page consumes
// useBreakpoint — the single breakpoint source.
let isDesktop = true;
vi.mock("@/lib/breakpoint", () => ({
  useBreakpoint: () => ({
    tier: isDesktop ? "xl" : "xs",
    isNarrow: !isDesktop,
    isLarge: isDesktop,
  }),
}));

beforeEach(() => {
  isDesktop = true;
});

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <DisplaySettingsPage />
    </QueryClientProvider>,
  );
}

// Explicit cleanup: globals are off (vitest.config.ts), so RTL's auto-cleanup
// never registers and the DOM would accumulate one page per test — the
// multiple-elements failures in the slider tests exposed that. Other frontend
// test files (page.test.tsx, composer.test.tsx) follow the same pattern.
afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getSettings).mockResolvedValue({ settings: [] });
  vi.mocked(api.getModels).mockResolvedValue({ providers: {}, models: {}, default: "" });
});

describe("DisplaySettingsPage", () => {
  it("renders all setting labels", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("Show machine name")).toBeTruthy();
    });

    expect(screen.getByText("Show agent status dot")).toBeTruthy();
    expect(screen.getByText("Show activity line")).toBeTruthy();
    expect(screen.getByText("Show terminated agents")).toBeTruthy();
    expect(screen.getByText("Time display mode")).toBeTruthy();
    expect(screen.getByText("Date format")).toBeTruthy();
    expect(screen.getByText("Awaiting-reply notification")).toBeTruthy();
    expect(screen.getByText("Confirm before terminate")).toBeTruthy();
    expect(screen.getByText("Confirm before restart")).toBeTruthy();
    expect(screen.getByText("Confirm before force kill")).toBeTruthy();
  });

  it("renders the Timeline defaults toggle", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("Collapse details by default")).toBeTruthy();
    });
  });

  // Task #715: the Timeline max-width slider renders with the default ratio
  // (0.4 → "40%") and persists changes (debounced PUT) to
  // display.timeline_width_ratio.
  it("renders the Timeline max width slider with the default ratio", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("Timeline max width")).toBeTruthy();
    });
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- RTL getByLabelText returns HTMLElement; narrowing to access .type/.value
    const slider = screen.getByLabelText("Timeline max width") as HTMLInputElement;
    expect(slider.type).toBe("range");
    expect(slider.value).toBe("0.4");
    expect(screen.getByText("40%")).toBeTruthy();
  });

  // Task #805: on narrow viewports (phones) the timeline is full-width and
  // the ratio setting is inert — the slider is disabled (greyed out) rather
  // than offering an effect it won't have.
  it("narrow viewport → timeline max-width slider is disabled", async () => {
    isDesktop = false;
    renderPage();

    await waitFor(() => {
      expect(screen.getByLabelText("Timeline max width")).toBeTruthy();
    });
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- RTL getByLabelText returns HTMLElement; narrowing to access .disabled
    const slider = screen.getByLabelText("Timeline max width") as HTMLInputElement;
    expect(slider.disabled).toBe(true);
    // Changing a disabled slider must not persist.
    fireEvent.change(slider, { target: { value: "0.6" } });
    expect(api.putSetting).not.toHaveBeenCalled();
  });

  it("desktop viewport → timeline max-width slider is enabled", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByLabelText("Timeline max width")).toBeTruthy();
    });
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- RTL getByLabelText returns HTMLElement; narrowing to access .disabled
    const slider = screen.getByLabelText("Timeline max width") as HTMLInputElement;
    expect(slider.disabled).toBe(false);
  });

  it("persists a slider change to display.timeline_width_ratio (debounced)", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByLabelText("Timeline max width")).toBeTruthy();
    });
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- RTL getByLabelText returns HTMLElement; narrowing to access .value
    const slider = screen.getByLabelText("Timeline max width") as HTMLInputElement;
    fireEvent.change(slider, { target: { value: "0.6" } });
    expect(slider.value).toBe("0.6");
    await waitFor(() => {
      expect(api.putSetting).toHaveBeenCalledWith("display.timeline_width_ratio", 0.6);
    });
  });

  it("groups the model picker toggles by provider with a header per group", async () => {
    vi.mocked(api.getModels).mockResolvedValue({
      providers: {
        deepseek: ["deepseek-v4-pro"],
        claude: ["claude-sonnet-5", "claude-haiku-4-5-20251001"],
      },
      models: {},
      default: "deepseek-v4-pro",
    });
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("deepseek-v4-pro")).toBeTruthy();
    });
    expect(screen.getByText("DeepSeek")).toBeTruthy();
    expect(screen.getByText("Claude")).toBeTruthy();
    expect(screen.getByText("claude-sonnet-5")).toBeTruthy();
    expect(screen.getByText("claude-haiku-4-5-20251001")).toBeTruthy();
  });

  // i18n MVP: the Language row renders English/中文 options and writes the
  // display.language setting on change.
  it("renders the Language row and persists a locale choice", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getAllByText("Language").length).toBeGreaterThan(0);
    });
    const english = screen.getByLabelText("English") as unknown as HTMLInputElement;
    const chinese = screen.getByLabelText("中文") as unknown as HTMLInputElement;
    expect(english.checked).toBe(true);
    expect(chinese.checked).toBe(false);

    fireEvent.click(chinese);
    await waitFor(() => {
      expect(api.putSetting).toHaveBeenCalledWith("display.language", "zh");
    });
  });
});
