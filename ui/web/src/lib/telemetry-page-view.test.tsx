import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { initWebVitals } from "./web-vitals";

let pathname = "/";

vi.mock("next/navigation", () => ({ usePathname: () => pathname }));
vi.mock("./telemetry", () => ({
  normalizePage: (path: string) => (path === "/" ? "home" : path.replace(/^\//, "")),
  setTelemetryPage: vi.fn(),
  track: vi.fn(),
}));
vi.mock("./web-vitals", () => ({ initWebVitals: vi.fn() }));

import { TelemetryPageView } from "./telemetry-page-view";

afterEach(() => {
  cleanup();
  pathname = "/";
  vi.clearAllMocks();
});

it("initializes Web Vitals once across authenticated route changes", () => {
  const view = render(<TelemetryPageView />);
  pathname = "/fleet";
  view.rerender(<TelemetryPageView />);

  expect(initWebVitals).toHaveBeenCalledTimes(1);
});
