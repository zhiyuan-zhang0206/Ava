// Providers: QueryClient defaults + root provider composition.

import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";

// EventStreamProvider opens a real EventSource (happy-dom ships one that hits
// the network); stub it to a pass-through so the smoke test stays offline. The
// connection behavior itself is covered in useEventStream.test.ts. With the
// module mocked, the root AgentsCacheSync subscriber's useEventStream is a noop.
vi.mock("@/lib/useEventStream", () => ({
  EventStreamProvider: ({ children }: { children: React.ReactNode }) => children,
  useEventStream: vi.fn(),
}));

// AlertsProvider opens the /api/alerts SSE stream + initial fetch on mount;
// the stream machinery has its own tests (use-alerts.test.tsx) — this smoke
// test mocks the module out like the EventStream one above.
vi.mock("@/lib/use-alerts", () => ({
  AlertsProvider: ({ children }: { children: React.ReactNode }) => children,
}));

vi.mock("next-themes", () => ({
  ThemeProvider: ({ children, nonce }: { children: ReactNode; nonce?: string }) => (
    <div data-testid="theme-provider" data-nonce={nonce}>{children}</div>
  ),
  useTheme: () => ({ resolvedTheme: "light" }),
}));

// Two of Providers' children fire real api calls on mount if left unmocked —
// unmocked, these are live fetches that silently "succeed" against a local
// dev gateway but ECONNREFUSED with no gateway around (CI), the source of a
// flaky "Unhandled Errors" vitest exit surfacing in an unrelated test file:
//   - AppConnectionBanner's useClusterHealth() → api.getClusterStatus()
//   - AuthProvider's mount-time session check → api.checkAuth()
// Stub both to never-resolving promises: this smoke test only checks the
// initial render, not status- or auth-driven content.
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getClusterStatus: vi.fn(() => new Promise(() => undefined)),
      checkAuth: vi.fn(() => new Promise(() => undefined)),
    },
  };
});

import { ApiError } from "@/lib/api";
import { setSessionInvalidHandler } from "@/lib/auth-context";
import { useNonce } from "@/lib/nonce-context";

import { createQueryClient, Providers } from "./providers";

afterEach(() => {
  cleanup();
  setSessionInvalidHandler(null);
  vi.useRealTimers();
});

function QueryProbe({ queryFn }: { queryFn: () => Promise<unknown> }) {
  useQuery({ queryKey: ["query-client-probe"], queryFn });
  return null;
}

describe("QueryClient defaults", () => {
  it("routes a 401 to auth without retrying the query", async () => {
    const sessionInvalidHandler = vi.fn();
    const queryFn = vi
      .fn()
      .mockRejectedValue(new ApiError(401, "authentication required"));
    setSessionInvalidHandler(sessionInvalidHandler);

    render(
      <QueryClientProvider client={createQueryClient()}>
        <QueryProbe queryFn={queryFn} />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(sessionInvalidHandler).toHaveBeenCalledTimes(1));
    expect(queryFn).toHaveBeenCalledTimes(1);
  });

  it("caps transient-failure retries at two (three attempts, task #3895)", async () => {
    vi.useFakeTimers();
    const queryFn = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));

    render(
      <QueryClientProvider client={createQueryClient()}>
        <QueryProbe queryFn={queryFn} />
      </QueryClientProvider>,
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(queryFn).toHaveBeenCalledTimes(3);
  });

  it("retries only transient statuses and caps the retry delay (task #3895)", () => {
    const client = createQueryClient();
    const retry = client.getDefaultOptions().queries?.retry as (
      failureCount: number,
      error: unknown,
    ) => boolean;
    expect(retry(0, new ApiError(401, "auth"))).toBe(false);
    expect(retry(0, new ApiError(404, "not found"))).toBe(false);
    expect(retry(0, new ApiError(422, "unprocessable"))).toBe(false);
    expect(retry(0, new ApiError(408, "timeout"))).toBe(true);
    expect(retry(0, new ApiError(429, "rate limited"))).toBe(true);
    expect(retry(0, new ApiError(503, "unavailable"))).toBe(true);
    expect(retry(1, new TypeError("Failed to fetch"))).toBe(true);
    expect(retry(2, new TypeError("Failed to fetch"))).toBe(false);
    const retryDelay = client.getDefaultOptions().queries?.retryDelay as (n: number) => number;
    expect(retryDelay(0)).toBe(1_000);
    expect(retryDelay(1)).toBe(2_000);
    expect(retryDelay(6)).toBe(8_000);
  });
});

describe("Providers", () => {
  it("passes children through", () => {
    render(
      <Providers>
        <span data-testid="child">child</span>
      </Providers>,
    );
    expect(screen.getByTestId("child")).toBeTruthy();
  });

  it("passes the request nonce to the theme bootstrap script", () => {
    render(
      <Providers nonce="request-nonce">
        <span>child</span>
      </Providers>,
    );

    expect(screen.getByTestId("theme-provider").dataset.nonce).toBe("request-nonce");
  });

  it("exposes the request nonce to the subtree", () => {
    function NonceProbe() {
      return <span data-testid="nonce-probe">{useNonce() ?? "(none)"}</span>;
    }

    render(
      <Providers nonce="request-nonce">
        <NonceProbe />
      </Providers>,
    );

    expect(screen.getByTestId("nonce-probe").textContent).toBe("request-nonce");
  });
});
