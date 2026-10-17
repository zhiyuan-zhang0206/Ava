
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { HostPermissionsPayload, PermissionItemRow } from "@/lib/contracts/types";

const row = (over: Partial<PermissionItemRow>): PermissionItemRow => ({
  id: "screen-recording",
  title: "Screen Recording",
  group: "desktop",
  state: "denied",
  state_label: "denied -- turn it on in System Settings",
  mode: "System Settings only",
  why: "Take screenshots and window captures.",
  jump: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
  verify: "Meiva is listed and ticked in Screen & System Audio Recording.",
  restart: "Restart the permissions helper after granting it.",
  required: true,
  blocking: true,
  detail: "",
  ...over,
});

const payload = (over: Partial<HostPermissionsPayload> = {}): HostPermissionsPayload => ({
  state: "action_required",
  headline: "Some permissions are still missing.",
  checked_at: "2026-10-17T10:00:00+00:00",
  diagnostic: "",
  helper_reachable: true,
  required_total: 10,
  required_missing: 1,
  items: [
    row({}),
    row({
      id: "automation-finder",
      title: "Control Finder",
      state: "unknown",
      state_label: "not readable here",
      mode: "granted per app, on first use",
      blocking: false,
      why: "Ask Finder to open a window.",
      verify: "Automation lists Meiva with Finder ticked.",
      restart: "",
    }),
  ],
  ...over,
});

const getHostPermissions = vi.fn<() => Promise<HostPermissionsPayload>>();
const recheckHostPermissions = vi.fn<() => Promise<HostPermissionsPayload>>();
const requestHostPermissions = vi.fn<() => Promise<HostPermissionsPayload>>();
const deferHostPermissions = vi.fn<() => Promise<HostPermissionsPayload>>();
const showToast = vi.fn();

vi.mock("@/lib/transport/api", () => ({
  api: {
    getHostPermissions: () => getHostPermissions(),
    recheckHostPermissions: () => recheckHostPermissions(),
    requestHostPermissions: () => requestHostPermissions(),
    deferHostPermissions: () => deferHostPermissions(),
  },
}));
vi.mock("@/lib/state/store", () => ({
  useStore: (selector: (state: { showToast: typeof showToast }) => unknown) =>
    selector({ showToast }),
}));

const { PermissionChecklist } = await import("./permission-checklist");

function wrap(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  vi.clearAllMocks();
  getHostPermissions.mockResolvedValue(payload());
  recheckHostPermissions.mockResolvedValue(
    payload({
      state: "complete",
      required_missing: 0,
      headline: "All desktop permissions are in place.",
    }),
  );
  requestHostPermissions.mockResolvedValue(payload());
  deferHostPermissions.mockResolvedValue(payload({ state: "deferred" }));
});

describe("PermissionChecklist", () => {
  it("renders every row with why, where to click, and how to confirm it", async () => {
    wrap(<PermissionChecklist />);
    expect(await screen.findByText("Screen Recording")).toBeTruthy();
    expect(screen.getByText("Take screenshots and window captures.")).toBeTruthy();
    expect(
      screen.getByText("Meiva is listed and ticked in Screen & System Audio Recording."),
    ).toBeTruthy();
    expect(screen.getByText("Restart the permissions helper after granting it.")).toBeTruthy();
    expect(screen.getByText("Open System Settings").closest("a")?.getAttribute("href")).toBe(
      "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    );
    expect(screen.getByText("Control Finder")).toBeTruthy();
  });

  it("sends a grant the helper has not read to the restart, not to System Settings", async () => {
    getHostPermissions.mockResolvedValue(
      payload({
        items: [
          row({
            state: "stale",
            state_label: "granted in macOS; the helper has not read it yet",
            restart: "Restart the permissions helper (`meiva permissions restart-helper`).",
          }),
        ],
      }),
    );
    wrap(<PermissionChecklist />);
    expect(
      await screen.findByText("granted in macOS; the helper has not read it yet"),
    ).toBeTruthy();
    expect(screen.getByText(/Restart the permissions helper/)).toBeTruthy();
    expect(screen.queryByText("Open System Settings")).toBeNull();
  });

  it("re-checks against the system instead of trusting what it rendered", async () => {
    wrap(<PermissionChecklist />);
    await screen.findByText("Screen Recording");
    fireEvent.click(screen.getByRole("button", { name: /re-check/i }));
    await waitFor(() => expect(recheckHostPermissions).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("All desktop permissions are in place.")).toBeTruthy();
  });

  it("records later without claiming anything was granted", async () => {
    wrap(<PermissionChecklist />);
    await screen.findByText("Screen Recording");
    fireEvent.click(screen.getByRole("button", { name: /do it later/i }));
    await waitFor(() => expect(deferHostPermissions).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith(
        "Recorded. Nothing was granted; the next start asks again.",
      ),
    );
  });
});
