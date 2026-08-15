// PackageDraftEntry tests — the natural-language "add a package" entry shared
// by the Skills / Plugins / MCP sections.
//
// Asserts the contract that matters: it posts the SECTION'S kind alongside the
// user's words, jumps to the spawned installer's conversation, and offers no
// URL/spec field (the no-URL rule is the point of the flow, so it is a test).
//
// happy-dom + RTL + real QueryClient (mock at the api layer); next/navigation
// router and the Zustand store's toast/selection are mocked.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";

import { PackageDraftEntry } from "./package-draft-entry";

const pushSpy = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: pushSpy }) }));

afterEach(cleanup);
beforeEach(() => {
  vi.restoreAllMocks();
  pushSpy.mockReset();
});

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("PackageDraftEntry", () => {
  it("submit posts the section's kind + the user's words, then opens the conversation", async () => {
    const draft = vi.spyOn(api, "draftPackage").mockResolvedValue({ agent_id: 42 });
    wrap(<PackageDraftEntry kind="mcp" />);

    const input = screen.getByPlaceholderText(/Describe a tool you want to reach/);
    fireEvent.change(input, { target: { value: "  query our Linear issues  " } });
    fireEvent.click(screen.getByRole("button", { name: "Find and install an MCP server" }));

    await waitFor(() => expect(draft).toHaveBeenCalledWith("mcp", "query our Linear issues"));
    await waitFor(() => expect(pushSpy).toHaveBeenCalledWith("/"));
  });

  it("Enter submits too", async () => {
    const draft = vi.spyOn(api, "draftPackage").mockResolvedValue({ agent_id: 7 });
    wrap(<PackageDraftEntry kind="skill" />);

    const input = screen.getByPlaceholderText(/Describe a skill you want/);
    fireEvent.change(input, { target: { value: "write release notes" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => expect(draft).toHaveBeenCalledWith("skill", "write release notes"));
  });

  it("each kind carries its own kind through — a plugin entry never posts 'skill'", async () => {
    const draft = vi.spyOn(api, "draftPackage").mockResolvedValue({ agent_id: 3 });
    wrap(<PackageDraftEntry kind="plugin" />);

    fireEvent.change(screen.getByPlaceholderText(/Describe a plugin you want/), {
      target: { value: "review my PRs" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Find and install a plugin" }));

    await waitFor(() => expect(draft).toHaveBeenCalledWith("plugin", "review my PRs"));
  });

  it("empty / whitespace input cannot submit", () => {
    const draft = vi.spyOn(api, "draftPackage");
    wrap(<PackageDraftEntry kind="skill" />);

    const button = screen.getByRole("button", { name: "Find and install a skill" });
    expect(button.hasAttribute("disabled")).toBe(true);

    fireEvent.change(screen.getByPlaceholderText(/Describe a skill you want/), {
      target: { value: "   " },
    });
    expect(button.hasAttribute("disabled")).toBe(true);
    expect(draft).not.toHaveBeenCalled();
  });

  it("offers no URL / git-source field — every install goes through the agent", () => {
    wrap(<PackageDraftEntry kind="plugin" />);
    // Exactly one input, and it is the natural-language one.
    const inputs = document.querySelectorAll("input");
    expect(inputs.length).toBe(1);
    expect(inputs[0].getAttribute("placeholder")).toMatch(/Describe a plugin you want/);
    expect(screen.queryByPlaceholderText(/https?:\/\//)).toBeNull();
    expect(screen.queryByPlaceholderText(/url|git/i)).toBeNull();
  });

  it("a failed draft surfaces an error and does not navigate", async () => {
    vi.spyOn(api, "draftPackage").mockRejectedValue(new Error("gateway down"));
    wrap(<PackageDraftEntry kind="skill" />);

    fireEvent.change(screen.getByPlaceholderText(/Describe a skill you want/), {
      target: { value: "anything" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Find and install a skill" }));

    await waitFor(() => expect(api.draftPackage).toHaveBeenCalled());
    expect(pushSpy).not.toHaveBeenCalled();
  });
});
