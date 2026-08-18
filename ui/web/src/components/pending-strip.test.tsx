import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { PendingStrip } from "./pending-strip";
import type { PendingInbound } from "@/lib/types";

afterEach(cleanup);

function item(over: Partial<PendingInbound> & { id: number }): PendingInbound {
  return { source: "user", content: "hello", created_at: "2026-05-28T00:00:00Z", ...over };
}

describe("PendingStrip", () => {
  it("renders nothing when empty", () => {
    const { container } = render(<PendingStrip items={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("shows count + each item's content", () => {
    render(
      <PendingStrip
        items={[
          item({ id: 1, content: "check the logs" }),
          item({ id: 2, content: "here is the result" }),
        ]}
      />,
    );
    expect(screen.getByText("2 pending")).toBeTruthy();
    expect(screen.getByText("check the logs")).toBeTruthy();
    expect(screen.getByText("here is the result")).toBeTruthy();
  });

  it("maps source tags to short labels", () => {
    render(
      <PendingStrip
        items={[
          item({ id: 1, source: "user" }),
          item({ id: 2, source: "agent:3" }),
          item({ id: 3, source: "schedule:7" }),
          item({ id: 4, source: "watcher:5" }),
          item({ id: 5, source: null }),
        ]}
      />,
    );
    expect(screen.getByText("· User")).toBeTruthy();
    expect(screen.getByText("· agent 3")).toBeTruthy();
    expect(screen.getByText("· scheduled")).toBeTruthy();
    // unrecognized tags fall through to the raw value
    expect(screen.getByText("· watcher:5")).toBeTruthy();
    expect(screen.getByText("· system")).toBeTruthy();
  });

  it("labels a page callback (ui:page:<name>) as User", () => {
    render(<PendingStrip items={[item({ id: 1, source: "ui:page:compare" })]} />);
    expect(screen.getByText("· User")).toBeTruthy();
  });
});
