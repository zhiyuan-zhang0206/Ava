import { describe, expect, it, vi } from "vitest";

const { redirect } = vi.hoisted(() => ({ redirect: vi.fn() }));
vi.mock("next/navigation", () => ({ redirect }));

import AlertsRedirect from "./page";

describe("AlertsRedirect", () => {
  it("forwards the alert-history deep link to the Insights anchor", () => {
    AlertsRedirect();

    expect(redirect).toHaveBeenCalledWith("/insights#alerts");
  });
});
