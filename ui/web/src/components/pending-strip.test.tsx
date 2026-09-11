import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { PendingStrip } from "./pending-strip";
import type { PendingInbound } from "@/lib/types";

afterEach(cleanup);

function item(over: Partial<PendingInbound> & { id: number }): PendingInbound {
  return {
    source: "user",
    content: "hello",
    images: null,
    created_at: "2026-05-28T00:00:00Z",
    ...over,
  };
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
    expect(screen.getByText("· Agent #3")).toBeTruthy();
    expect(screen.getByText("· scheduled")).toBeTruthy();
    // unrecognized tags fall through to the raw value
    expect(screen.getByText("· watcher:5")).toBeTruthy();
    expect(screen.getByText("· system")).toBeTruthy();
  });

  it("labels a page callback (ui:page:<name>) as User", () => {
    render(<PendingStrip items={[item({ id: 1, source: "ui:page:compare" })]} />);
    expect(screen.getByText("· User")).toBeTruthy();
  });

  it("renders a thumbnail per image on a queued multimodal message", () => {
    render(
      <PendingStrip
        items={[
          item({
            id: 1,
            content: "look at this",
            images: ["/api/agents/7/uploads/a.png", "/api/agents/7/uploads/b.png"],
          }),
        ]}
      />,
    );
    const thumbs = screen.getAllByRole("img");
    expect(thumbs).toHaveLength(2);
    expect(thumbs[0].getAttribute("src")).toContain("/api/agents/7/uploads/a.png");
    expect(thumbs[1].getAttribute("src")).toContain("/api/agents/7/uploads/b.png");
    expect(thumbs[0].getAttribute("alt")).toBe("pending image");
    // The message's text still shows next to its thumbnails.
    expect(screen.getByText("look at this")).toBeTruthy();
  });

  it("renders no thumbnail and keeps the text for a text-only message", () => {
    render(<PendingStrip items={[item({ id: 1, content: "just text" })]} />);
    expect(screen.queryAllByRole("img")).toHaveLength(0);
    expect(screen.getByText("just text")).toBeTruthy();
  });

  it("suppresses the [image] placeholder when thumbnails render", () => {
    const { rerender } = render(
      <PendingStrip
        items={[item({ id: 1, content: "[image]", images: ["/api/agents/7/uploads/a.png"] })]}
      />,
    );
    expect(screen.queryByText("[image]")).toBeNull();
    // Without an image the same literal is the message's real text — keep it.
    rerender(<PendingStrip items={[item({ id: 1, content: "[image]" })]} />);
    expect(screen.getByText("[image]")).toBeTruthy();
  });
});
