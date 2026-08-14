// LanguageProvider — the settings→next-intl bridge. Uses the REAL next-intl
// module (unlike the global test double in vitest.setup.ts, which serves
// components rendered without a provider): this test verifies the actual
// provider wiring — locale + messages handed to NextIntlClientProvider, and
// the <html lang> sync effect.
//
// The settings store is the shared mock (user-settings-mock.ts), so flipping
// display.language re-renders consumers immediately — the same path the
// production optimistic update takes.

import { act, cleanup, render, screen } from "@testing-library/react";
import { useTranslations } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Restore the real next-intl for this file (setup's double is per-file; the
// re-mock below replaces it).
vi.mock("next-intl", async (importOriginal) => {
  // eslint-disable-next-line @typescript-eslint/consistent-type-imports -- dynamic import type in mock factory
  const actual = await importOriginal<typeof import("next-intl")>();
  return actual;
});

vi.mock("@/lib/use-user-settings", () =>
  import("@/test-support/user-settings-mock"),
);

import {
  resetMockSettings,
  setMockSetting,
} from "@/test-support/user-settings-mock";
import { LanguageProvider } from "./language-provider";

// A consumer that reads a key from the "spawn" namespace — renders the
// English string when locale=en, the Chinese string when locale=zh.
function Probe() {
  const t = useTranslations("spawn");
  return <span>{t("spawnAgent")}</span>;
}

afterEach(() => {
  cleanup();
  document.documentElement.lang = "en";
});

beforeEach(() => {
  resetMockSettings();
});

describe("LanguageProvider", () => {
  it("defaults to English when the language setting is absent", () => {
    render(
      <LanguageProvider>
        <Probe />
      </LanguageProvider>,
    );
    expect(screen.getByText("Spawn agent")).toBeTruthy();
    expect(document.documentElement.lang).toBe("en");
  });

  it("renders the Chinese catalog when display.language=zh and syncs <html lang>", () => {
    resetMockSettings({ "display.language": "zh" });
    render(
      <LanguageProvider>
        <Probe />
      </LanguageProvider>,
    );
    expect(screen.getByText("创建 agent")).toBeTruthy();
    expect(document.documentElement.lang).toBe("zh-CN");
  });

  it("switches locale live when the setting changes", () => {
    const { rerender } = render(
      <LanguageProvider>
        <Probe />
      </LanguageProvider>,
    );
    expect(screen.getByText("Spawn agent")).toBeTruthy();

    // Simulate the settings optimistic update landing (setMockSetting flips
    // the shared store and emits). act() wraps the store emit so the
    // useSyncExternalStore update commits before the assertions.
    act(() => {
      setMockSetting("display.language", "zh");
    });

    expect(screen.getByText("创建 agent")).toBeTruthy();
    expect(document.documentElement.lang).toBe("zh-CN");
    rerender(<LanguageProvider><Probe /></LanguageProvider>);
    expect(screen.getByText("创建 agent")).toBeTruthy();
  });
});
