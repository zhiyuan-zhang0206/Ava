"use client";

// LanguageProvider — the settings→next-intl bridge.
//
// Ava's UI language is a DB-backed user setting (display.language, see
// USER_SETTING_DEFAULTS in lib/types.ts), NOT a URL prefix or a cookie: it
// syncs across devices through user_settings and switches instantly, without
// a router refresh. next-intl runs in its "without i18n routing" mode — the
// provider below feeds the active locale + its message catalog straight to
// NextIntlClientProvider, and every useTranslations() consumer in the tree
// re-renders the moment the setting flips (the optimistic update in
// useUserSettings lands in the React Query cache synchronously).
//
// Default locale is "en" until settings load (a logged-out login page has no
// settings row yet); the language setting then takes over. `<html lang>` is
// synced here on the client — the server-rendered shell can't read the
// setting, so layout.tsx renders a neutral lang and this effect corrects it
// after hydration (same pattern as next-themes' class injection, which
// already lives in this tree).

import { NextIntlClientProvider } from "next-intl";
import { useEffect } from "react";

import { useUserSettings } from "@/lib/use-user-settings";

import en from "../../messages/en.json";
import zh from "../../messages/zh.json";

// The supported locales and their catalogs. Adding a locale = add a
// messages/<locale>.json + one entry here (and the AppConfig Locale union in
// i18n/app-config.d.ts).
export const LOCALES = {
  en,
  zh,
} as const;

export type AppLocale = keyof typeof LOCALES;

export function isAppLocale(v: unknown): v is AppLocale {
  return v === "en" || v === "zh";
}

export function LanguageProvider({ children }: { children: React.ReactNode }) {
  const { settings } = useUserSettings();
  const locale: AppLocale = isAppLocale(settings["display.language"])
    ? settings["display.language"]
    : "en";

  // Keep <html lang> in sync with the active UI language. Runs on every
  // locale change; the initial "en" from the server shell is corrected
  // right after hydration.
  useEffect(() => {
    document.documentElement.lang = locale === "zh" ? "zh-CN" : "en";
  }, [locale]);

  return (
    <NextIntlClientProvider locale={locale} messages={LOCALES[locale]}>
      {children}
    </NextIntlClientProvider>
  );
}
