// data-testid hooks on the marker render paths (Task #1018) — the stable
// selectors the panoramic e2e cases assert against:
//   marker-unrecognized — the #1017 red alarm chip (must never render)
//   marker-error       — the SSE-error [error] chip (the legit error path)
// Rendering EphemeralSystemMarker directly (no TimelineView) keeps this file
// free of the timeline mock harness; next-intl is mocked globally in
// vitest.setup.ts.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { EphemeralSystemMarker, MarkerBody } from "./markers";

// This repo's convention: explicit cleanup (no vitest globals auto-cleanup).
afterEach(() => {
  cleanup();
});

describe("marker data-testid hooks (Task #1018)", () => {
  it("marker payload bodies use the 13px readability floor", () => {
    const { container } = render(<MarkerBody payload="detail" />);
    expect(container.querySelector("pre")?.className).toContain("text-[13px]");
  });

  it("unrecognized alarm renders data-testid=marker-unrecognized", () => {
    render(<EphemeralSystemMarker source={null} payload="future_kind_not_adapted" />);
    expect(screen.getByTestId("marker-unrecognized")).toBeTruthy();
  });

  it("error marker renders data-testid=marker-error", () => {
    render(<EphemeralSystemMarker source={null} payload="error:redis disconnected" />);
    expect(screen.getByTestId("marker-error")).toBeTruthy();
    expect(screen.queryByTestId("marker-unrecognized")).toBeNull();
  });

  it("filtered payloads (compact_done / compact_request / cancelled) render neither", () => {
    for (const payload of ["compact_done", "cancelled", "compact_request:x"]) {
      const { unmount } = render(<EphemeralSystemMarker source={null} payload={payload} />);
      expect(screen.queryByTestId("marker-unrecognized")).toBeNull();
      expect(screen.queryByTestId("marker-error")).toBeNull();
      unmount();
    }
  });
});
