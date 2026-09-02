import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/fleet/fleet-view", () => ({
  FleetView: () => <section aria-label="Fleet supervision surface" />,
}));

import FleetPage from "./page";

describe("FleetPage", () => {
  it("provides the primary landmark and mounts the fleet surface", () => {
    render(<FleetPage />);

    const main = screen.getByRole("main");
    expect(main.id).toBe("main-content");
    expect(main.classList.contains("flex-1")).toBe(true);
    expect(screen.getByRole("region", { name: "Fleet supervision surface" })).toBeTruthy();
  });
});
