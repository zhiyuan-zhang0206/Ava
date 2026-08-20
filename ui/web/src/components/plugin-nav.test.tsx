// Plugin nav entries — placement, attribution, and the link they produce.
//
// The assertions read the rendered anchors: a declaration for one location must
// not leak onto another surface, the console route must carry the plugin and
// the declared page path, and an entry must say which plugin put it there.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PluginNavIcons, PluginNavList } from "@/components/plugin-nav";
import { api } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  api: { getUiContributions: vi.fn() },
  API_BASE: "http://gateway.test:8000",
}));

const BOARD = {
  plugin: "board",
  location: "sidebar",
  label: "Task board",
  icon: "kanban",
  page: "board/",
};
const LEDGER = {
  plugin: "ledger",
  location: "settings",
  label: "Balance",
  icon: "coins",
  page: "balance/index.html",
};
const FLEET = {
  plugin: "board",
  location: "fleet-toolbar",
  label: "Board",
  icon: "kanban",
  page: "board/",
};

function renderNav(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getUiContributions).mockResolvedValue({
    themes: [],
    nav: [BOARD, LEDGER, FLEET],
  });
});

afterEach(cleanup);

describe("plugin nav", () => {
  it("renders only the entries declared for its location", async () => {
    renderNav(<PluginNavList location="settings" />);

    await waitFor(() => expect(screen.getByText("Balance")).toBeTruthy());
    // The sidebar and fleet-toolbar declarations are not this surface's.
    expect(screen.queryByText("Task board")).toBeNull();
    expect(screen.queryByText("Board")).toBeNull();
  });

  it("links to the console route carrying the plugin and the declared page", async () => {
    renderNav(<PluginNavList location="settings" />);

    await waitFor(() => expect(screen.getByText("Balance")).toBeTruthy());
    const link = screen.getByText("Balance").closest("a");
    expect(link?.getAttribute("href")).toBe("/plugin/ledger/balance/index.html");
  });

  it("attributes an entry to the plugin that declared it", async () => {
    renderNav(<PluginNavList location="settings" />);

    await waitFor(() => expect(screen.getByText("ledger")).toBeTruthy());
  });

  it("renders an icon entry as an attributed link to its page", async () => {
    renderNav(<PluginNavIcons location="fleet-toolbar" />);

    await waitFor(() => expect(screen.getByLabelText("Board (board)")).toBeTruthy());
    // next/link normalizes the trailing slash off the CONSOLE route; the
    // gateway mount URL keeps it (plugin-page-frame.test.tsx), and a directory
    // reached without one redirects to it.
    expect(screen.getByLabelText("Board (board)").getAttribute("href")).toBe(
      "/plugin/board/board",
    );
  });

  it("renders nothing when no plugin declares an entry for the location", async () => {
    vi.mocked(api.getUiContributions).mockResolvedValue({ themes: [], nav: [] });

    const { container } = renderNav(<PluginNavIcons location="fleet-toolbar" />);

    await waitFor(() => expect(api.getUiContributions).toHaveBeenCalled());
    expect(container.innerHTML).toBe("");
  });
});
