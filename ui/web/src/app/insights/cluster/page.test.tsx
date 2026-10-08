import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/components/cluster-view/cluster-view", () => ({
  ClusterView: ({ root, window }: { root: number; window: { from: string; to: string } }) => (
    <div data-testid="cluster-view-stub">{`${root} ${window.from} ${window.to}`}</div>
  ),
}));

import ClusterPage from "./page";

beforeEach(() => {
  window.history.replaceState(null, "", "/insights/cluster");
});

afterEach(cleanup);

describe("ClusterPage", () => {
  it("shows the view of the selection in the URL", async () => {
    window.history.replaceState(
      null,
      "",
      "/insights/cluster?root=405&from=2026-10-03T00:00:00.000Z&to=2026-10-04T00:00:00.000Z",
    );
    render(<ClusterPage />);
    expect((await screen.findByTestId("cluster-view-stub")).textContent).toBe(
      "405 2026-10-03T00:00:00.000Z 2026-10-04T00:00:00.000Z",
    );
  });

  it("asks for a root and a window while the URL names none", async () => {
    render(<ClusterPage />);
    expect(await screen.findByText("Enter a root agent and a window to see its agents.")).toBeTruthy();
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Show" }).disabled).toBe(true);
  });

  it("applies the form to the view and to the URL", async () => {
    render(<ClusterPage />);
    await screen.findByText("Enter a root agent and a window to see its agents.");
    fireEvent.change(screen.getByLabelText("Root agent"), { target: { value: "7" } });
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-10-03T08:00" } });
    fireEvent.change(screen.getByLabelText("To"), { target: { value: "2026-10-03T09:30" } });
    fireEvent.click(screen.getByRole("button", { name: "Show" }));
    await waitFor(() => expect(screen.getByTestId("cluster-view-stub").textContent).toMatch(/^7 /));
    const params = new URLSearchParams(window.location.search);
    expect(params.get("root")).toBe("7");
    expect(Date.parse(params.get("to") ?? "") - Date.parse(params.get("from") ?? "")).toBe(90 * 60_000);
  });
});
