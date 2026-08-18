# Frontend i18n: next-intl prefixless + framework copy only (ruled 2026-08-05)

Date: 2026-08-05 (user ruling)

## Context

The frontend needs a multi-language UI (Settings page Language row, en/zh). Constraints:

- Language is a **user setting** (`user_settings.display.language`, persisted in the DB, synced across devices), not a URL or browser property;
- Switching must take effect immediately, without refresh flicker (Ava is a resident SPA; routes do not reload);
- Desktop/web are same-origin (the web app is the universal host, the desktop app is a thin wrapper), so language state cannot live only in the shell.

## Decision

- **Choose next-intl, no i18n routing mode**: no URL prefix / middleware; `LanguageProvider` feeds `user_settings`' `display.language` straight into `NextIntlClientProvider` (`frontend/src/i18n/language-provider.tsx`). Default en (before settings load); `<html lang>` syncs after client hydration (same pattern as next-themes).
- **Message catalogs**: `frontend/messages/en.json` is the **single source of truth** (the `Messages` type in `app-config.d.ts` anchors to it; `useTranslations()` gets compile-time key validation); `zh.json` mirrors its structure. The en/zh key sets are forced symmetric by a test (`src/i18n/messages-symmetry.test.ts`) — a missing zh key silently falls back to English (next-intl default behavior), so only the test can pin it.
- **Translation boundary (user ruling)**: only translate **UI framework copy** (navigation/buttons/settings/form labels…). **Data surfaces are not translated**: agent output, event streams, task titles, notice bodies, timeline content stay as-is; backend error messages pass through untranslated in the MVP phase.
- Adding a locale = add `messages/<locale>.json` + one `LOCALES` entry + an `AppConfig.Locale` union member.

## Alternatives rejected

- **Official cookie+refresh scheme**: language state in a cookie, refresh on switch — does not sync across devices (cookies are per-device state; the DB setting is authoritative), and refresh flicker violates the resident SPA's instant-switch requirement.
- **URL-prefix routing mode (/zh/...)**: language is a user setting, not content addressing; a URL prefix pollutes every route, anchor (/control#config deep links) and share semantics; prefixless also keeps migration cost minimal.
- **Self-built i18n provider**: next-intl already covers key validation (typed), pluralization/interpolation, and fallback semantics; building our own reinvents the wheel.

## Consequences

- Translation coverage is incremental: at merge time control/status/settings etc. were translated (431 keys); remaining pages (insights/fleet/memory…) were not (estimated +2-3 person-days) — untranslated pages show English.
- A missing zh key silently falls back to English, invisible visually → rely on the key-symmetry test + a glossary (pending user ruling on whether "agent" should be translated in zh UI copy).
- Existing component tests assert English copy; translation changes must update the tests in the same change (test copy follows en).
- Backend error messages pass through in English — a known MVP boundary.

<!-- Not overturned. -->
