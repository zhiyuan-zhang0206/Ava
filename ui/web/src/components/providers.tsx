"use client";

import { QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "next-themes";
import { useState } from "react";

import { AppConnectionBanner } from "@/components/app-connection-banner";
import { OpenTasksNoticeHost } from "@/components/open-tasks-notice";
import { ThemePackTokens } from "@/components/theme-pack-tokens";
import { ToastHost } from "@/components/toast";
import { LanguageProvider } from "@/i18n/language-provider";
import { AuthProvider, notifySessionInvalid } from "@/lib/auth-context";
import { ApiError } from "@/lib/api";
import { NonceProvider } from "@/lib/nonce-context";
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
// Memory share one EventSource instead of tearing it down and reopening on
// every switch; it does close while the tab is hidden — useEventStream.tsx).
// The fold owner (useFoldOwner inside EventStreamProvider) is the single root
// writer folding SSE into the query caches. The all-events, high-frequency
// stream stays scoped to the conversation view (page.tsx).
//
// AppConnectionBanner also rides that connection here at the root, so
// disconnect/recovery chrome protects every page, not just the home view.
export function createQueryClient(): QueryClient {
  return new QueryClient({
    queryCache: new QueryCache({
      onError: (error) => {
        if (error instanceof ApiError && error.status === 401) {
          notifySessionInvalid();
        }
      },
    }),
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
        retry: (failureCount, error) => {
          if (error instanceof ApiError) {
            // Task #1326: a 401 is not transient; retrying amplifies it into a poll storm.
            if (error.status === 401) return false;
            // Other 4xx answers are deterministic too — a retry re-hits the
            // same answer once per poll cadence. Only 408/429 ask for one.
            if (error.status < 500 && error.status !== 408 && error.status !== 429) {
              return false;
            }
          }
          // Task #3895: transient failures get at most two retries. Read paths
          // recover through their own polls / SSE invalidations, so a longer
          // burst against a struggling backend only multiplies load.
          return failureCount < 2;
        },
        // Capped exponential backoff (1s, 2s), so a failing request's retries
        // cannot spread past its poll interval into the next cycle.
        retryDelay: (attempt) => Math.min(1_000 * 2 ** attempt, 8_000),
      },
    },
  });
}

export function Providers({ children, nonce }: { children: React.ReactNode; nonce?: string }) {
  const [queryClient] = useState(() => createQueryClient());
  return (
    <NonceProvider value={nonce}>
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <EventStreamProvider>
            <AlertsProvider>
              <ThemeProvider
                attribute="class"
                defaultTheme="system"
                enableSystem
                disableTransitionOnChange
                nonce={nonce}
              >
                <LanguageProvider>
                  {/* Applies the selected plugin skin's tokens over the active
                      light/dark palette; renders nothing. */}
                  <ThemePackTokens />
                  <AppConnectionBanner />
                  {children}
                  {/* The toast renderer is root-level so error toasts reach the
                      user on every route, not just the Home page (Task #1051). */}
                  <ToastHost />
                  {/* Terminate open-tasks notice — same root-level slot pattern
                      as the toast: a terminate response that reports open tasks
                      must surface wherever the action was taken (task #3374). */}
                  <OpenTasksNoticeHost />
                </LanguageProvider>
              </ThemeProvider>
            </AlertsProvider>
          </EventStreamProvider>
        </AuthProvider>
      </QueryClientProvider>
    </NonceProvider>
  );
}
