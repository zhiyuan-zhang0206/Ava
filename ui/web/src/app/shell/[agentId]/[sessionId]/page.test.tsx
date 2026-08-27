// ShellMonitorPage: dark full-screen terminal renders shell output from a
// mocked GET /api/agents/{id}/shell/{sid}?lines=N response. Tests cover the
// header (agent/shell ids, session name), the lines input control (default 200,
// Enter/blur commit, invalid revert), manual refresh button, and terminal output
// (content / empty / error states).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render as rtlRender,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ShellMonitorPage from "@/app/shell/[agentId]/[sessionId]/page";
import type { ShellCapture } from "@/lib/types";

// vi.hoisted so the mock fn is initialised before the hoisted vi.mock factory runs.
const { getAgentShell } = vi.hoisted(() => ({
  getAgentShell: vi.fn<(agentId: number, sessionId: number, lines?: number) => Promise<ShellCapture>>(),
}));
vi.mock("@/lib/api", () => ({
  api: { getAgentShell },
}));

// Terminal theme is a DB-backed user setting; the reactive mock cycles it +
// re-renders on setSetting (no React Query network for settings).
vi.mock("@/lib/use-user-settings", () => import("@/test-support/user-settings-mock"));
import { resetMockSettings } from "@/test-support/user-settings-mock";

afterEach(() => {
  vi.useRealTimers();
  cleanup();
  getAgentShell.mockReset();
  resetMockSettings();
});

function render() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  // In Next.js 16, params is a Promise.  The page unwraps it via
  // useState+useEffect — no Suspense boundary needed.
  const params = Promise.resolve({ agentId: "5", sessionId: "12" });
  return rtlRender(
    <QueryClientProvider client={qc}>
      <ShellMonitorPage params={params} />
    </QueryClientProvider>,
  );
}

function shellData(overrides: Partial<ShellCapture> = {}): ShellCapture {
  return {
    agent_id: 5,
    session_id: 12,
    session_name: "ava-main-agent-5-shell-12-dev-server",
    lines: ["$ npm run dev", "> ready on :3000"],
    // Offsets from fixture-build time so the tick-computed title-bar meta
    // lands on stable formatted spans; the meta tests freeze Date for
    // exact determinism.
    created_at: new Date(Date.now() - 8040_000).toISOString(),
    uptime_seconds: 8040,
    expires_at: new Date(Date.now() + 7900_000).toISOString(),
    ...overrides,
  };
}

describe("ShellMonitorPage", () => {
  it("renders the header with agent and shell ids", async () => {
    getAgentShell.mockResolvedValue(shellData());
    render();

    // The h1 contains "Agent #5 Shell #12" with # and numbers
    // split across span + text nodes, so query the h1 directly.
    await waitFor(() => {
      const h1 = document.querySelector("h1");
      expect(h1?.textContent).toContain("Agent #5 Shell #12");
    });
  });

  it("shows the session name when loaded", async () => {
    getAgentShell.mockResolvedValue(shellData());
    render();

    await waitFor(() =>
      expect(screen.getByText("ava-main-agent-5-shell-12-dev-server")).toBeTruthy(),
    );
  });

  it("shows runtime + TTL in the title bar", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-06-14T12:00:00Z"));
    getAgentShell.mockResolvedValue(shellData());
    render();

    // Runtime 8040s → "2h 14m"; TTL 7900s → "2h 11m" (exact under the
    // frozen clock; expiry time varies with the test host's timezone, so
    // match the shape only).
    await waitFor(() => expect(screen.getByText("Runtime 2h 14m")).toBeTruthy());
    expect(screen.getByText(/TTL 2h 11m · expires \d{1,2}\/\d{1,2} \d{2}:\d{2}/)).toBeTruthy();
  });

  it("clamps an already-expired TTL deadline to 0s remaining", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-06-14T12:00:00Z"));
    getAgentShell.mockResolvedValue(
      shellData({
        created_at: new Date(Date.now() - 3600_000).toISOString(),
        uptime_seconds: 3600,
        expires_at: new Date(Date.now() - 10_000).toISOString(),
      }),
    );
    render();

    await waitFor(() => expect(screen.getByText("Runtime 1h 0m")).toBeTruthy());
    expect(screen.getByText(/TTL 0s · expires/)).toBeTruthy();
  });

  it("falls back to the probe uptime and shows No TTL when the session has neither", async () => {
    getAgentShell.mockResolvedValue(
      shellData({ created_at: null, uptime_seconds: 45, expires_at: null }),
    );
    render();

    await waitFor(() => expect(screen.getByText("Runtime 45s")).toBeTruthy());
    expect(screen.getByText("No TTL")).toBeTruthy();
  });

  it("renders terminal output lines", async () => {
    getAgentShell.mockResolvedValue(shellData());
    render();

    await waitFor(() => expect(screen.getByText(/\$ npm run dev/)).toBeTruthy());
    expect(screen.getByText(/> ready on :3000/)).toBeTruthy();
  });

  it("shows (no output) when lines array is empty", async () => {
    getAgentShell.mockResolvedValue(shellData({ lines: [] }));
    render();

    await waitFor(() => expect(screen.getByText("(no output)")).toBeTruthy());
  });

  it("shows an error message when the fetch fails", async () => {
    getAgentShell.mockRejectedValue(new Error("connection refused"));
    render();

    await waitFor(() =>
      expect(screen.getByText("connection refused")).toBeTruthy(),
    );
  });

  it("calls getAgentShell with default lines=200 on first load", async () => {
    getAgentShell.mockResolvedValue(shellData());
    render();

    await waitFor(() => expect(getAgentShell).toHaveBeenCalled());
    // First call should have lines=200 (default).
    expect(getAgentShell).toHaveBeenCalledWith(5, 12, 200);
  });

  it("refetchOnMount 'always': entering with a warm, fresh cache still fetches immediately", async () => {
    // Prod's global 5min staleTime would otherwise treat a warm cache as fresh
    // and skip the mount fetch — you'd stare at stale output until the 3s poll.
    // Pin that entering the page forces a fetch even against a fresh cache
    // (the cached output paints first, so there's no blank flash).
    const qc = new QueryClient({
      defaultOptions: {
        queries: { retry: false, staleTime: 5 * 60_000, gcTime: 30 * 60_000 },
      },
    });
    qc.setQueryData(["agent-shell", 5, 12, 200], shellData({ lines: ["cached line"] }));
    getAgentShell.mockResolvedValue(shellData({ lines: ["fresh line"] }));

    rtlRender(
      <QueryClientProvider client={qc}>
        <ShellMonitorPage params={Promise.resolve({ agentId: "5", sessionId: "12" })} />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(getAgentShell).toHaveBeenCalledWith(5, 12, 200));
    await waitFor(() => expect(screen.getByText(/fresh line/)).toBeTruthy());
  });

  it("updates lines on Enter and triggers a new fetch with the new value", async () => {
    getAgentShell.mockResolvedValue(shellData());
    render();

    await waitFor(() => expect(getAgentShell).toHaveBeenCalledWith(5, 12, 200));

    const input = screen.getByLabelText("Lines");
    fireEvent.change(input, { target: { value: "500" } });
    fireEvent.keyDown(input, { key: "Enter" });

    // After Enter, the query should re-fetch with lines=500.
    await waitFor(() =>
      expect(getAgentShell).toHaveBeenCalledWith(5, 12, 500),
    );
  });

  it("updates lines on blur", async () => {
    getAgentShell.mockResolvedValue(shellData());
    render();

    await waitFor(() => expect(getAgentShell).toHaveBeenCalledWith(5, 12, 200));

    const input = screen.getByLabelText("Lines");
    fireEvent.change(input, { target: { value: "100" } });
    fireEvent.blur(input);

    await waitFor(() =>
      expect(getAgentShell).toHaveBeenCalledWith(5, 12, 100),
    );
  });

  it("reverts invalid input on blur", async () => {
    getAgentShell.mockResolvedValue(shellData());
    render();

    await waitFor(() => expect(getAgentShell).toHaveBeenCalledWith(5, 12, 200));

    const input = screen.getByLabelText("Lines");
    fireEvent.change(input, { target: { value: "99999" } });
    fireEvent.blur(input);

    // Should revert to the previous valid value (200).
    await waitFor(() => expect((input as HTMLInputElement).value).toBe("200"));
    expect(getAgentShell).not.toHaveBeenCalledWith(5, 12, 99999);
  });

  it("has a manual refresh button that triggers refetch", async () => {
    getAgentShell.mockResolvedValue(shellData());
    render();

    // Wait for first fetch to complete (data loaded).
    await waitFor(() => expect(screen.getByText(/\$ npm run dev/)).toBeTruthy());

    const refreshBtn = screen.getByLabelText("Refresh shell output");
    fireEvent.click(refreshBtn);

    await waitFor(() => expect(getAgentShell).toHaveBeenCalledTimes(2));
  });

  it("has a back link to dashboard", () => {
    getAgentShell.mockResolvedValue(shellData());
    render();

    const backLink = screen.getByLabelText("Back to dashboard");
    expect(backLink.getAttribute("href")).toBe("/");
  });

  it("shows live indicator", () => {
    getAgentShell.mockResolvedValue(shellData());
    render();

    expect(screen.getByText("Live")).toBeTruthy();
  });

  it("stale-while-error: a failed poll keeps the last output and flips the dot to 'stale'", async () => {
    getAgentShell.mockResolvedValueOnce(shellData());
    render();

    await waitFor(() => expect(screen.getByText(/\$ npm run dev/)).toBeTruthy());
    expect(screen.getByText("Live")).toBeTruthy();

    // The next poll fails — but there is already output on screen.
    getAgentShell.mockRejectedValue(new Error("connection refused"));
    fireEvent.click(screen.getByLabelText("Refresh shell output"));

    // The health dot follows the poll: "live" → "stale"…
    await waitFor(() => expect(screen.getByText("Stale")).toBeTruthy());
    // …and the last-good output is still shown, NOT replaced by the error text.
    expect(screen.getByText(/\$ npm run dev/)).toBeTruthy();
    expect(screen.queryByText("connection refused")).toBeNull();
  });
});

describe("ShellMonitorPage terminal theme", () => {
  it("renders the theme toggle button with system icon by default", () => {
    getAgentShell.mockResolvedValue(shellData());
    render();

    const btn = screen.getByLabelText("Terminal theme: system");
    expect(btn).toBeTruthy();
  });

  it("cycles theme system → light → dark → system on click", async () => {
    getAgentShell.mockResolvedValue(shellData());
    render();

    await waitFor(() => expect(screen.getByText(/\$ npm run dev/)).toBeTruthy());

    const btn = screen.getByLabelText("Terminal theme: system");
    expect(btn).toBeTruthy();

    // system → light
    fireEvent.click(btn);
    expect(screen.getByLabelText("Terminal theme: light")).toBeTruthy();

    // light → dark
    fireEvent.click(btn);
    expect(screen.getByLabelText("Terminal theme: dark")).toBeTruthy();

    // dark → system
    fireEvent.click(btn);
    expect(screen.getByLabelText("Terminal theme: system")).toBeTruthy();
  });

  it("shows invalid-params message when agentId or sessionId is NaN", () => {
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    // In Next.js 16, params is a Promise.  Passing NaN strings inside
    // the Promise exercises the same validParams check.
    const params = Promise.resolve({ agentId: "NaN", sessionId: "NaN" });
    rtlRender(
      <QueryClientProvider client={qc}>
        <ShellMonitorPage params={params} />
      </QueryClientProvider>,
    );

    expect(screen.getByText("Invalid agent or session id")).toBeTruthy();
    // The query should not have been called — enabled=false
    expect(getAgentShell).not.toHaveBeenCalled();
  });
});
