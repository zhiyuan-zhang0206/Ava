import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";

const push = vi.fn();
const showToast = vi.fn();
const setActiveId = vi.fn();

vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("@/lib/store", () => ({
  useStore: <T,>(selector: (state: { showToast: typeof showToast; setActiveId: typeof setActiveId }) => T): T =>
    selector({ showToast, setActiveId }),
}));

import GuidePage from "./page";

describe("GuidePage", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    push.mockReset();
    showToast.mockReset();
    setActiveId.mockReset();
  });

  it("disables Ask until a non-blank request is entered", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <GuidePage />
      </QueryClientProvider>,
    );

    const ask = screen.getByRole("button", { name: "Ask Ava" });
    expect(ask.hasAttribute("disabled")).toBe(true);

    fireEvent.change(screen.getByPlaceholderText(/Describe an operations task/), {
      target: { value: "   " },
    });
    expect(ask.hasAttribute("disabled")).toBe(true);
  });

  it("drafts a trimmed request then selects the conversation and navigates home", async () => {
    const draftGuide = vi.spyOn(api, "draftGuide").mockResolvedValue({ agent_id: 42 });
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}>
        <GuidePage />
      </QueryClientProvider>,
    );

    fireEvent.change(screen.getByPlaceholderText(/Describe an operations task/), {
      target: { value: "  install the linear MCP server  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ask Ava" }));

    await waitFor(() => expect(draftGuide).toHaveBeenCalledWith("install the linear MCP server"));
    await waitFor(() => expect(setActiveId).toHaveBeenCalledWith(42));
    expect(showToast).toHaveBeenCalledWith("Ava Guide #42 started — continue in the conversation");
    expect(push).toHaveBeenCalledWith("/");
    expect((screen.getByPlaceholderText(/Describe an operations task/) as HTMLInputElement).value).toBe("");
  });

  it("reports a draft failure without navigating", async () => {
    vi.spyOn(api, "draftGuide").mockRejectedValue(new Error("gateway unavailable"));
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}>
        <GuidePage />
      </QueryClientProvider>,
    );

    fireEvent.change(screen.getByPlaceholderText(/Describe an operations task/), {
      target: { value: "install a plugin" },
    });
    fireEvent.keyDown(screen.getByPlaceholderText(/Describe an operations task/), { key: "Enter" });

    await waitFor(() => expect(showToast).toHaveBeenCalledWith("Guide failed: gateway unavailable"));
    expect(push).not.toHaveBeenCalled();
  });
});
