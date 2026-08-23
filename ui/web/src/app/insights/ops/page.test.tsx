import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import OpsPage from "./page";

afterEach(cleanup);

function wrap(ui: React.ReactElement) {
  return render(ui);
}

describe("Ops tab (Grafana link)", () => {
  it("renders the Metrics section sub-anchor", () => {
    wrap(<OpsPage />);
    expect(document.getElementById("ops-metrics")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Metrics (Grafana)" })).toBeTruthy();
  });

  it("renders one link to the merged dashboard through the gateway proxy", () => {
    wrap(<OpsPage />);
    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(1);
    const link = screen.getByRole("link", { name: "Open the Ava ops dashboard" });
    expect(link.getAttribute("href")).toBe(
      "http://localhost:8000/grafana/d/ava-ops-main?from=now-6h&to=now",
    );
    expect(link.getAttribute("href")).toContain("/grafana/d/ava-ops-main");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noreferrer");
  });

  it("does not render an iframe", () => {
    wrap(<OpsPage />);
    expect(document.querySelector("iframe")).toBeNull();
  });
});
