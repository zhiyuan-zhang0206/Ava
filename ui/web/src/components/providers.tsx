"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "next-themes";
import { useState } from "react";

import { AppConnectionBanner } from "@/components/app-connection-banner";
import { ToastHost } from "@/components/toast";
import { LanguageProvider } from "@/i18n/language-provider";
import { AuthProvider } from "@/lib/auth-context";
import { SettingsMigration } from "@/lib/settings-migration";
import { EventStreamProvider } from "@/lib/useEventStream";
import { AlertsProvider } from "@/lib/use-alerts";

// The inline script next-themes injects toggles .dark on <html> before
// hydrate, no FOUC; a matchMedia listener follows system settings.
//
// attribute="class"      → use class, not data-attribute (matches shadcn)
// defaultTheme="system"  → follow system by default
// enableSystem           → allow "system" as a valid theme value
// disableTransitionOnChange → briefly suspend CSS transitions during a
//   theme swap to avoid color-transition flicker mid-swap
//
// QueryClientProvider: TanStack Query's global cache container; provides
// the data layer for hooks like useAgents (replaces the manual useState
// + setInterval polling pattern). useState holds the client instance —
// a module-level `new QueryClient()` would share one client across
// requests under SSR and leak data between users; useState gives each
// mount its own client (standard App Router approach).
//
// EventStreamProvider lives here, above the route tree, so the global
// `/api/system` broadcast survives page navigation (Main / Fleet / Settings /
// Memory share one persistent EventSource instead of tearing it down and
// reopening on every switch). The fold owner (useFoldOwner inside
// EventStreamProvider) is the single root writer folding SSE into the query
// caches. The all-events, high-frequency stream stays scoped to the
// conversation view (page.tsx).
//
// AppConnectionBanner also rides that connection here at the root, so the
// SSE-disconnect / cluster-updating / stranded-recovery chrome (and the
// cluster-health poller behind it) protects every page, not just the home view.
export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // Page switches should hit cache, not refetch: with the global
            // connection now persistent, a 5min staleTime keeps a just-visited
            // page's data warm across navigation (SSE-driven queries override
            // to Infinity; polling queries set their own refetchInterval).
            staleTime: 5 * 60_000,
            // Retain inactive (unobserved) query caches long enough that
            // returning to a page after a detour still hot-hits instead of
            // cold-fetching with a spinner.
            gcTime: 30 * 60_000,
            // Default-refetch-on-window-focus would shadow SSE-pushed
            // live data (e.g. timeline) with stray GETs; turn it off and
            // declare refetchInterval explicitly on useQueries that
            // actually want polling (e.g. status).
            refetchOnWindowFocus: false,
          },
        },
      }),
  );
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <EventStreamProvider>
          <AlertsProvider>
            <SettingsMigration />
            <ThemeProvider
              attribute="class"
              defaultTheme="system"
              enableSystem
              disableTransitionOnChange
            >
              <LanguageProvider>
                <AppConnectionBanner />
                {children}
                {/* The toast renderer is root-level so error toasts reach the
                    user on every route, not just the Home page (Task #1051). */}
                <ToastHost />
              </LanguageProvider>
            </ThemeProvider>
          </AlertsProvider>
        </EventStreamProvider>
      </AuthProvider>
    </QueryClientProvider>
  );
}
