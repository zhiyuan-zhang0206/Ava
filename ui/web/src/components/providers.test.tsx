// Providers: mounts QueryClient + EventStreamProvider + ThemeProvider — smoke
// test (children pass through + no throw).

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

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

// Two of Providers' children fire real api calls on mount if left unmocked —
// unmocked, these are live fetches that silently "succeed" against a local
// dev gateway but ECONNREFUSED with no gateway around (CI), the source of a
// flaky "Unhandled Errors" vitest exit surfacing in an unrelated test file:
//   - AppConnectionBanner's useClusterHealth() → api.getSystemStatus()
//   - AuthProvider's mount-time session check → api.checkAuth()
// Stub both to never-resolving promises: this smoke test only checks the
// initial render, not status- or auth-driven content.
vi.mock("@/lib/api", () => ({
  api: {
    getSystemStatus: vi.fn(() => new Promise(() => undefined)),
    checkAuth: vi.fn(() => new Promise(() => undefined)),
  },
}));

import { Providers } from "./providers";

afterEach(cleanup);

describe("Providers", () => {
  it("passes children through", () => {
    render(
      <Providers>
        <span data-testid="child">child</span>
      </Providers>,
    );
    expect(screen.getByTestId("child")).toBeTruthy();
  });
});
