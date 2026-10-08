// /control/presets page tests — loading/error/empty/data states, natural-
// language draft create (spawns writer + navigates), label/description edit,
// and delete-with-confirm.
//
// happy-dom + RTL + real QueryClient (mock at the api layer); next/navigation
// router is mocked.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/transport/api";
import type { PresetView } from "@/lib/contracts/types";
import { useStore } from "@/lib/state/store";

import PresetsPage from "./page";

const PRESET: PresetView = {
  id: 1,
  name: "coder",
  label: "Coding agent",
  description: "writes code",
  config: { llm_model: "m1" },
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-02T00:00:00Z",
};

const pushSpy = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: pushSpy }) }));

afterEach(cleanup);
beforeEach(() => {
  vi.restoreAllMocks();
  pushSpy.mockReset();
  useStore.setState({ activeId: null });
});

function makeQc() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function wrap(ui: React.ReactElement) {
  return render(<QueryClientProvider client={makeQc()}>{ui}</QueryClientProvider>);
}

describe("PresetsPage", () => {
  it("shows loading spinner", () => {
    vi.spyOn(api, "listPresets").mockReturnValue(new Promise<PresetView[]>(() => undefined));
    wrap(<PresetsPage />);
    expect(document.querySelector(".animate-spin")).toBeTruthy();
  });

  it("shows error state", async () => {
    vi.spyOn(api, "listPresets").mockRejectedValue(new Error("fail"));
    wrap(<PresetsPage />);
    await waitFor(() => expect(screen.getByText(/Couldn't load presets/)).toBeTruthy());
  });

  it("shows empty state", async () => {
    vi.spyOn(api, "listPresets").mockResolvedValue([]);
    wrap(<PresetsPage />);
    await waitFor(() => expect(screen.getByText(/No presets defined/)).toBeTruthy());
  });

  it("renders a preset card with label, name, pretty JSON, and its nav anchor", async () => {
    vi.spyOn(api, "listPresets").mockResolvedValue([PRESET]);
    wrap(<PresetsPage />);
    await waitFor(() => expect(screen.getByText("Coding agent")).toBeTruthy());
    expect(screen.getByText("coder")).toBeTruthy();
    // Config renders pretty-printed (multi-line, wrapped), not the one-line preview.
    expect(screen.getByText(/"llm_model": "m1"/)).toBeTruthy();
    // The card carries the anchor id the nav's dynamic Presets sub-links jump to.
    expect(document.getElementById("preset-coder")).toBeTruthy();
  });

  it("has one maker entry point that can open before the user writes a request", async () => {
    vi.spyOn(api, "listPresets").mockResolvedValue([]);
    wrap(<PresetsPage />);
    await waitFor(() => expect(screen.getByText(/No presets defined/)).toBeTruthy());

    expect(screen.getByPlaceholderText(/Describe a role/)).toBeTruthy();
    const buttons = screen.getAllByRole("button", { name: "Open Preset Maker" });
    expect(buttons).toHaveLength(1);
    expect((buttons[0] as HTMLButtonElement).disabled).toBe(false);
    // No raw-JSON creation path left anywhere on the page.
    expect(screen.queryByRole("button", { name: "New" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Add preset/ })).toBeNull();
    expect(screen.queryByText("Create preset")).toBeNull();
  });

  it("draft: spawns a writer via the plain spawn endpoint and navigates to the conversation", async () => {
    vi.spyOn(api, "listPresets").mockResolvedValue([]);
    const spawn = vi.spyOn(api, "spawnAgent").mockResolvedValue({ id: 42 });
    wrap(<PresetsPage />);
    await waitFor(() => expect(screen.getByText(/No presets defined/)).toBeTruthy());

    const input = screen.getByPlaceholderText(/Describe a role/);
    fireEvent.change(input, { target: { value: "  a researcher preset  " } });
    fireEvent.click(screen.getByRole("button", { name: "Open Preset Maker" }));

    await waitFor(() => expect(spawn).toHaveBeenCalled());
    const call = spawn.mock.calls[0]?.[0];
    expect(call?.prompt).toContain("ava.skills.ava_guide.presets");
    expect(call?.prompt).toContain("a researcher preset");
    expect(call?.prompt_source).toBe("user");
    expect(call?.label).toBe("ava-preset-maker");
    expect(call?.config).toEqual({ skills_to_expand_at_start: ["ava-guide:presets"] });
    await waitFor(() => expect(pushSpy).toHaveBeenCalledWith("/"));
    expect(useStore.getState().activeId).toBe(42);
    expect((input as HTMLInputElement).value).toBe("");
  });

  it("opens the maker with a skill and starter request when the input is blank", async () => {
    vi.spyOn(api, "listPresets").mockResolvedValue([]);
    const spawn = vi.spyOn(api, "spawnAgent").mockResolvedValue({ id: 43 });
    wrap(<PresetsPage />);
    await waitFor(() => expect(screen.getByText(/No presets defined/)).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "Open Preset Maker" }));
    await waitFor(() => expect(pushSpy).toHaveBeenCalledWith("/"));
    expect(spawn).toHaveBeenCalledTimes(1);
    const call = spawn.mock.calls[0]?.[0];
    expect(call?.prompt).toContain("Help me design a reusable agent preset.");
    expect(call).toMatchObject({
      prompt_source: "user",
      label: "ava-preset-maker",
      config: { skills_to_expand_at_start: ["ava-guide:presets"] },
    });
    expect(useStore.getState().activeId).toBe(43);
  });

  it("does not spawn another maker on Enter while a launch is pending", async () => {
    vi.spyOn(api, "listPresets").mockResolvedValue([]);
    const spawn = vi.spyOn(api, "spawnAgent").mockReturnValue(new Promise(() => undefined));
    wrap(<PresetsPage />);
    await waitFor(() => expect(screen.getByText(/No presets defined/)).toBeTruthy());

    const input = screen.getByPlaceholderText(/Describe a role/);
    fireEvent.change(input, { target: { value: "a research role" } });
    fireEvent.keyDown(input, { key: "Enter" });
    const button = screen.getByRole("button", { name: "Open Preset Maker" });
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(true));
    fireEvent.keyDown(input, { key: "Enter" });
    expect(spawn).toHaveBeenCalledTimes(1);
    expect(pushSpy).not.toHaveBeenCalled();
  });

  it("delete: confirms then deletes", async () => {
    vi.spyOn(api, "listPresets").mockResolvedValue([PRESET]);
    const del = vi.spyOn(api, "deletePreset").mockResolvedValue({ status: "deleted" });
    window.confirm = vi.fn(() => true);
    wrap(<PresetsPage />);
    await waitFor(() => expect(screen.getByText("Coding agent")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith(1));
  });

  it("edit: opens the editor pre-filled with label/description (no config field) and saves", async () => {
    vi.spyOn(api, "listPresets").mockResolvedValue([PRESET]);
    const update = vi.spyOn(api, "updatePreset").mockResolvedValue(PRESET);
    wrap(<PresetsPage />);
    await waitFor(() => expect(screen.getByText("Coding agent")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));

    const labelInput = await screen.findByDisplayValue("Coding agent");
    const descriptionInput = screen.getByDisplayValue("writes code");
    // No raw config textarea in the edit form.
    expect(document.querySelector("textarea")).toBeNull();

    fireEvent.change(labelInput, { target: { value: "Renamed" } });
    fireEvent.change(descriptionInput, { target: { value: "still writes code" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(update).toHaveBeenCalledWith(1, {
        label: "Renamed",
        description: "still writes code",
      }),
    );
  });

  it("edit: cancel closes the editor without saving", async () => {
    vi.spyOn(api, "listPresets").mockResolvedValue([PRESET]);
    const update = vi.spyOn(api, "updatePreset").mockResolvedValue(PRESET);
    wrap(<PresetsPage />);
    await waitFor(() => expect(screen.getByText("Coding agent")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    await screen.findByDisplayValue("Coding agent");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByDisplayValue("Coding agent")).toBeNull();
    expect(update).not.toHaveBeenCalled();
  });
});
