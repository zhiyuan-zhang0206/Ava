// Default-model control tests — read the stored value, PUT a new one.
//
// Renders <DefaultModelPanel /> directly; useSectionVisible defaults true so the
// queries enable. Mock at the api layer.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";

import { DefaultModelPanel } from "./_default_model";

vi.mock("@/lib/api", () => ({
  api: {
    getDefaultModel: vi.fn(),
    getModels: vi.fn(),
    putDefaultModel: vi.fn(),
  },
}));

function wrap() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <DefaultModelPanel id="config-default-model" />
    </QueryClientProvider>,
  );
}

const MODELS = {
  providers: {
    deepseek: ["deepseek-v4-pro", "deepseek-v4-flash"],
    claude: ["claude-sonnet-5"],
  },
  models: {},
  default: "deepseek-v4-pro",
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getModels).mockResolvedValue(MODELS);
  vi.mocked(api.getDefaultModel).mockResolvedValue({
    model: "deepseek-v4-flash",
    source: "cluster",
  });
  vi.mocked(api.putDefaultModel).mockResolvedValue({
    model: "claude-sonnet-5",
    source: "cluster",
  });
});

afterEach(cleanup);

describe("DefaultModelPanel", () => {
  it("selects the stored default and groups options by provider", async () => {
    wrap();
    const select = await screen.findByTestId<HTMLSelectElement>("select-default-model");
    await waitFor(() => expect(select.value).toBe("deepseek-v4-flash"));
    // optgroup labels are an attribute, not text — read them off the DOM.
    const groups = [...select.querySelectorAll("optgroup")].map((g) => g.label);
    expect(groups).toEqual(["DeepSeek", "Claude"]);
    expect([...select.options].map((o) => o.value)).toEqual([
      "deepseek-v4-pro",
      "deepseek-v4-flash",
      "claude-sonnet-5",
    ]);
  });

  it("omits superseded roster options while preserving a stored superseded default", async () => {
    vi.mocked(api.getModels).mockResolvedValue({
      providers: {
        deepseek: ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4-legacy"],
      },
      models: {
        "deepseek-v4-pro": {
          provider: "deepseek",
          context_window: 128_000,
          superseded_by: "deepseek-v4-flash",
        },
        "deepseek-v4-flash": {
          provider: "deepseek",
          context_window: 128_000,
          superseded_by: null,
        },
        "deepseek-v4-legacy": {
          provider: "deepseek",
          context_window: 128_000,
          superseded_by: "deepseek-v4-flash",
        },
      },
      default: "deepseek-v4-flash",
    });
    vi.mocked(api.getDefaultModel).mockResolvedValue({
      model: "deepseek-v4-pro",
      source: "cluster",
    });

    wrap();
    const select = await screen.findByTestId<HTMLSelectElement>("select-default-model");
    await waitFor(() => expect(select.value).toBe("deepseek-v4-pro"));

    expect([...select.options].map((option) => option.value)).toEqual([
      "deepseek-v4-pro",
      "deepseek-v4-flash",
    ]);
  });

  it("PUTs the picked model on save", async () => {
    wrap();
    const select = await screen.findByTestId<HTMLSelectElement>("select-default-model");
    await waitFor(() => expect(select.value).toBe("deepseek-v4-flash"));

    fireEvent.change(select, { target: { value: "claude-sonnet-5" } });
    fireEvent.click(screen.getByTestId("save-default-model"));

    await waitFor(() => expect(api.putDefaultModel).toHaveBeenCalledWith("claude-sonnet-5"));
  });

  it("keeps save disabled until the pick differs from the stored value", async () => {
    wrap();
    const select = await screen.findByTestId<HTMLSelectElement>("select-default-model");
    await waitFor(() => expect(select.value).toBe("deepseek-v4-flash"));
    const save = screen.getByTestId<HTMLButtonElement>("save-default-model");
    expect(save.disabled).toBe(true);

    fireEvent.change(select, { target: { value: "deepseek-v4-flash" } });
    expect(screen.getByTestId<HTMLButtonElement>("save-default-model").disabled).toBe(true);

    fireEvent.change(select, { target: { value: "deepseek-v4-pro" } });
    expect(screen.getByTestId<HTMLButtonElement>("save-default-model").disabled).toBe(false);
  });

  it("flags a value that is only the .env / code default showing through", async () => {
    vi.mocked(api.getDefaultModel).mockResolvedValue({
      model: "deepseek-v4-pro",
      source: "config",
    });
    wrap();
    expect(await screen.findByTestId("default-model-source")).toBeTruthy();
  });

  it("does not flag a value the cluster chose", async () => {
    wrap();
    await screen.findByTestId("select-default-model");
    await waitFor(() => expect(screen.queryByTestId("default-model-source")).toBeNull());
  });

  it("shows a quiet error instead of the raw failure", async () => {
    vi.mocked(api.getDefaultModel).mockRejectedValue(new Error("boom 500"));
    wrap();
    expect(await screen.findByText(/Couldn't load the default model/)).toBeTruthy();
    expect(screen.queryByText(/boom 500/)).toBeNull();
  });
});
