// Skills section tests — tri-state (loading / error / empty) + the table
// (name, source layer, enabled) + the natural-language install entry, which
// stays usable through every tri-state. Renders <SkillsPage /> directly;
// useSectionVisible defaults true so the query enables. Mock at the api layer.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { SkillsView } from "@/lib/types";

import SkillsPage from "./page";

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

const SKILLS: SkillsView = {
  skills: [
    { name: "alpha", layer: "core", enabled: true, modified_locally: false },
    { name: "beta", layer: "plugin", enabled: true, modified_locally: false },
    { name: "gamma", layer: "machine", enabled: false, modified_locally: false },
    { name: "stray", layer: "untracked", enabled: false, modified_locally: false },
  ],
};

describe("SkillsPage", () => {
  it("loading shows a spinner", () => {
    vi.spyOn(api, "getSkills").mockReturnValue(new Promise(() => undefined));
    const { container } = wrap(<SkillsPage />);
    expect(container.querySelector(".animate-spin")).toBeTruthy();
  });

  it("error shows a quiet line, not the raw error", async () => {
    vi.spyOn(api, "getSkills").mockRejectedValue(new Error("boom 500"));
    wrap(<SkillsPage />);
    await waitFor(() => screen.getByText(/Couldn't load skills/));
    expect(screen.queryByText(/boom 500/)).toBeNull();
  });

  it("empty → (no skills installed)", async () => {
    vi.spyOn(api, "getSkills").mockResolvedValue({ skills: [] });
    wrap(<SkillsPage />);
    await waitFor(() => screen.getByText(/no skills installed/));
  });

  it("renders each skill with its layer + enabled", async () => {
    vi.spyOn(api, "getSkills").mockResolvedValue(SKILLS);
    wrap(<SkillsPage />);
    await waitFor(() => screen.getByText("alpha"));
    // names
    for (const n of ["alpha", "beta", "gamma", "stray"]) {
      expect(screen.getByText(n)).toBeTruthy();
    }
    // layer badges
    expect(screen.getByText("Core")).toBeTruthy();
    expect(screen.getByText("Plugin")).toBeTruthy();
    expect(screen.getByText("Machine")).toBeTruthy();
    expect(screen.getByText("Untracked")).toBeTruthy();
    // enabled column: alpha/beta enabled (checked), gamma/stray disabled (unchecked)
    const toggles = screen.getAllByRole("switch");
    expect(toggles.length).toBe(4);
    // alpha should be checked (enabled)
    expect(toggles[0].getAttribute("data-state")).toBe("checked");
    // gamma should be unchecked (disabled)
    expect(toggles[2].getAttribute("data-state")).toBe("unchecked");
    // stray (untracked) should be disabled
    expect(toggles[3].hasAttribute("disabled")).toBe(true);
  });

  it("install entry posts kind=skill and opens the installer's conversation", async () => {
    vi.spyOn(api, "getSkills").mockResolvedValue({ skills: [] });
    const draft = vi.spyOn(api, "draftPackage").mockResolvedValue({ agent_id: 55 });
    wrap(<SkillsPage />);
    await waitFor(() => screen.getByText(/no skills installed/));

    fireEvent.change(screen.getByPlaceholderText(/Describe a skill you want/), {
      target: { value: "write release notes the way we do" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Find and install a skill" }));

    await waitFor(() =>
      expect(draft).toHaveBeenCalledWith("skill", "write release notes the way we do"),
    );
    await waitFor(() => expect(pushSpy).toHaveBeenCalledWith("/"));
  });

  it("install entry survives a failed list read", async () => {
    vi.spyOn(api, "getSkills").mockRejectedValue(new Error("boom 500"));
    wrap(<SkillsPage />);
    await waitFor(() => screen.getByText(/Couldn't load skills/));
    expect(screen.getByRole("button", { name: "Find and install a skill" })).toBeTruthy();
  });
});
