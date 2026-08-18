// Vitest setup — global stubs for things the test DOM (happy-dom) doesn't
// implement, plus a network guard (below) that makes unmocked real requests
// fail loud instead of silently hitting a real gateway.
//
// happy-dom ships a no-op IntersectionObserver whose callback never fires, so
// any component that gates work on visibility (the Control page's per-section
// data loading) would stay permanently "not visible" under test. Stub it to
// report every observed element as intersecting the moment it's observed, so
// section content loads deterministically. A test that needs the not-visible
// path installs its own stub via vi.stubGlobal.

/* eslint-disable @typescript-eslint/no-empty-function -- observer teardown methods are intentional no-ops in the stub */

import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

// RTL auto-cleanup registers itself only when `afterEach` exists on
// globalThis — which `globals: false` (vitest.config.ts) never provides — so
// without this line NOTHING unmounts between tests. Rendered components leak
// for the rest of the file; a leaked live-clock interval (see
// src/components/timeline/reasoning-clock.ts useNow) can then fire a setState
// after the worker's happy-dom window is torn down, and react-dom's scheduler
// blows up with "ReferenceError: window is not defined" AFTER every test
// passed. Register the cleanup explicitly.
afterEach(cleanup);

class ImmediateIntersectionObserver implements IntersectionObserver {
  readonly root: Element | Document | null = null;
  readonly rootMargin: string = "";
  readonly scrollMargin: string = "";
  readonly thresholds: readonly number[] = [];
  private readonly callback: IntersectionObserverCallback;

  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback;
  }

  observe(target: Element): void {
    // Fire synchronously with isIntersecting=true — the element is treated as
    // fully on-screen, which is what the gating hook keys off.
    this.callback(
      [
        {
          isIntersecting: true,
          intersectionRatio: 1,
          target,
          boundingClientRect: target.getBoundingClientRect(),
          intersectionRect: target.getBoundingClientRect(),
          rootBounds: null,
          time: 0,
        },
      ],
      this,
    );
  }

  unobserve(): void {}
  disconnect(): void {}
  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }
}

vi.stubGlobal("IntersectionObserver", ImmediateIntersectionObserver);

// -- Global network guard ----------------------------------------------------
//
// Dev boxes run a real Ava gateway on :8000. A test that forgets to mock
// @/lib/api (or @/lib/useEventStream for SSE) doesn't fail locally — it
// silently round-trips against that real gateway — and only breaks in CI,
// where nothing is listening. That gap let two real leaks (providers.test.tsx,
// status/page.test.tsx) sit unnoticed until PR #789's CI job hit a flood of
// ECONNREFUSED; see commit 31191bb5 for the bisection. Make every real
// network path throw immediately, everywhere, by default — a test that
// genuinely owns the network boundary (api.test.ts, useEventStream.test.ts,
// upload-XHR tests) installs its own vi.stubGlobal(...) override for the one
// call it's exercising.
//
// Installed via plain assignment, not vi.stubGlobal: vi.stubGlobal remembers
// the value as of its *first* call on a given key as "original", and
// vi.unstubAllGlobals() restores to that original. If the guard itself used
// vi.stubGlobal, a test's own beforeEach(stubGlobal)/afterEach(unstubAllGlobals)
// cycle would restore the *real, unguarded* fetch after that test — leaving
// every other test in the same file unprotected. Plain assignment here means
// the real fetch/XMLHttpRequest/EventSource never re-enters scope for the
// life of the test process; a test's own stubGlobal call records *this*
// guard as the value to restore to.

type GuardedKind = "fetch" | "XMLHttpRequest" | "EventSource";

// A tripped guard rejects/throws right at the call site, but most call sites
// are query hooks whose caller never awaits or asserts on that rejection
// (React Query's internal try/catch folds it into query.error state, which a
// render-only smoke test never looks at) — so the rejection alone can go
// unnoticed by the test that caused it, and by the time an unhandled
// rejection would surface (a query's later retry, after cleanup) vitest can
// misattribute it to whatever unrelated test happens to be running by then.
// Record every trip and fail it loudly, attributed to the right test, in a
// global afterEach instead of relying on that.
let networkGuardTrips: string[] = [];

function networkGuardError(kind: GuardedKind, target: string): Error {
  const message =
    `[network-guard] blocked a real ${kind} request to ${target} -- tests must mock ` +
    `@/lib/api (or @/lib/useEventStream for SSE) instead of hitting the network. If this ` +
    `test file legitimately owns the network boundary, install its own ` +
    `vi.stubGlobal("${kind}", ...) override (see src/lib/api.test.ts / useEventStream.test.ts).`;
  networkGuardTrips.push(message);
  return new Error(message);
}

afterEach(() => {
  if (networkGuardTrips.length === 0) return;
  const trips = networkGuardTrips;
  networkGuardTrips = [];
  throw new Error(
    `network-guard: ${trips.length} unmocked real network call(s) during this test:\n` +
      trips.join("\n"),
  );
});

globalThis.fetch = (input: RequestInfo | URL) => {
  const target =
    typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  return Promise.reject(networkGuardError("fetch", target));
};

class GuardedXMLHttpRequest {
  // Minimal surface api.ts's uploadFiles() touches before send() — enough that
  // assigning to them doesn't throw for a caller that hasn't hit the guard yet.
  upload: { onprogress: ((e: ProgressEvent) => void) | null } = { onprogress: null };
  withCredentials = false;
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  private targetUrl = "(unknown — open() was never called)";

  open(_method: string, target: string | URL): void {
    this.targetUrl = String(target);
  }

  send(): void {
    // Real XMLHttpRequest.send() is synchronous-looking but async under the
    // hood; api.ts's only caller wraps it in `new Promise(...)`, which
    // catches a synchronous throw from the executor and turns it into a
    // rejection — matching guardedFetch's Promise.reject above.
    throw networkGuardError("XMLHttpRequest", this.targetUrl);
  }
}
globalThis.XMLHttpRequest = GuardedXMLHttpRequest as unknown as typeof XMLHttpRequest;

// happy-dom doesn't implement EventSource at all (constructing one today
// throws a bare "EventSource is not defined" ReferenceError) — this stub is
// defense in depth against a future Node/happy-dom version adding a real one,
// and gives the same URL + mock hint as the fetch/XHR guards above instead of
// an opaque ReferenceError.
function GuardedEventSource(target: string | URL): never {
  throw networkGuardError("EventSource", String(target));
}
globalThis.EventSource = GuardedEventSource as unknown as typeof EventSource;

// -- next-intl test double ---------------------------------------------------
//
// Components render with useTranslations() under test but no real
// NextIntlClientProvider is mounted (tests render components directly). Mock
// useTranslations to read the canonical en.json catalog — the same strings
// the production English UI renders — so existing assertions on English copy
// keep passing unchanged, and new i18n'd components need no provider wrapper.
// Interpolation ({name}) is substituted so t("key", {n: 2}) behaves like the
// real ICU path for the simple cases this suite exercises. The real
// NextIntlClientProvider / useLocale / etc. are preserved; tests that need the
// real bridge (language-provider.test.tsx) re-mock next-intl to the original.
vi.mock("next-intl", async (importOriginal) => {
  // eslint-disable-next-line @typescript-eslint/consistent-type-imports -- test double
  const actual = await importOriginal<typeof import("next-intl")>();
  // eslint-disable-next-line @typescript-eslint/no-explicit-any, @typescript-eslint/no-unsafe-assignment -- test double
  const en = (await import("./messages/en.json")).default as any;

  // Resolve a dotted path ("control.sections.guide") against the messages
  // tree; missing segments fall back to the raw key so a typo surfaces as the
  // key itself (visible in a failed assertion) instead of silently.
  const lookup = (obj: unknown, path: string): unknown =>
    path.split(".").reduce<unknown>(
      (acc, seg) =>
        acc && typeof acc === "object"
          ? (acc as Record<string, unknown>)[seg]
          : undefined,
      obj,
    );

  return {
    ...actual,
    useTranslations: (namespace?: string) => {
      const t = (key: string, values?: Record<string, string | number>): string => {
        const full = namespace ? `${namespace}.${key}` : key;
        const found = lookup(en, full);
        let s = typeof found === "string" ? found : key;
        if (values) {
          for (const [k, v] of Object.entries(values)) {
            s = s.replaceAll(`{${k}}`, String(v));
          }
        }
        return s;
      };
      return t as ReturnType<typeof actual.useTranslations>;
    },
  };
});
