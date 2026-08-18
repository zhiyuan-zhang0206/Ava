// /insights/metrics redirect tests — the retired Metrics page (2026-08-04 user
// ruling): the route
// survives as a transition notice that auto-redirects to the Ops section (the
// embedded Grafana dashboard that replaced the Metrics page) after a short
// delay — but ONLY when the /grafana proxy answers a HEAD probe; otherwise it
// stays put and keeps the fallback note (CLI / /api/metrics) readable. Manual
// links (Ops section + direct Grafana) are always offered.

import { cleanup, render, screen, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { replaceSpy } = vi.hoisted(() => ({ replaceSpy: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: replaceSpy }) }));

import MetricsRedirectPage from "./page";

afterEach(cleanup);

const fetchMock = vi.fn();

beforeEach(() => {
  replaceSpy.mockClear();
  fetchMock.mockReset();
  fetchMock.mockResolvedValue({ ok: true });
  vi.stubGlobal("fetch", fetchMock);
  vi.useFakeTimers();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

async function flushProbe() {
  // Let the probe's promise resolution + state update settle before timers.
  await act(async () => {
    await Promise.resolve();
  });
}

describe("Metrics redirect page", () => {
  it("renders the transition notice with manual links", async () => {
    render(<MetricsRedirectPage />);
    await flushProbe();
    expect(screen.getByText("Metrics moved to Grafana")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open the Ops dashboard" })).toBeTruthy();
    const direct = screen.getByRole("link", { name: /open grafana/i });
    expect(direct.getAttribute("href")).toContain("/grafana/d/ava-ops-main?from=now-24h&to=now&kiosk");
    // Fallback note: CLI / API path survives the retirement.
    expect(screen.getByText(/scripts\/metrics\.py/)).toBeTruthy();
    // Probe: HEAD on the direct Grafana URL.
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/grafana/d/ava-ops-main"),
      { method: "HEAD" },
    );
  });

  it("auto-redirects to the Ops section after the delay when the proxy answers", async () => {
    render(<MetricsRedirectPage />);
    await flushProbe();
    expect(replaceSpy).not.toHaveBeenCalled();
    act(() => {
      vi.advanceTimersByTime(1200);
    });
    expect(replaceSpy).toHaveBeenCalledWith("/insights#ops");
  });

  it("does NOT redirect when the /grafana proxy is off — fallback stays", async () => {
    fetchMock.mockResolvedValue({ ok: false });
    render(<MetricsRedirectPage />);
    await flushProbe();
    expect(screen.getByText(/Grafana 未响应/)).toBeTruthy();
    act(() => {
      vi.advanceTimersByTime(1200);
    });
    expect(replaceSpy).not.toHaveBeenCalled();
  });

  it("does NOT redirect when the probe errors (Grafana down)", async () => {
    fetchMock.mockRejectedValue(new Error("network"));
    render(<MetricsRedirectPage />);
    await flushProbe();
    act(() => {
      vi.advanceTimersByTime(1200);
    });
    expect(replaceSpy).not.toHaveBeenCalled();
  });

  it("clears the pending redirect on unmount", async () => {
    const { unmount } = render(<MetricsRedirectPage />);
    await flushProbe();
    unmount();
    act(() => {
      vi.advanceTimersByTime(1200);
    });
    expect(replaceSpy).not.toHaveBeenCalled();
  });
});
