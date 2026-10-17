import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { PendingStrip, userPending } from "./pending-strip";
import type { PendingInbound } from "@/lib/contracts/types";

afterEach(cleanup);

function item(over: Partial<PendingInbound> & { id: number }): PendingInbound {
  return {
    source: "user",
    sender: "user",
    content: "hello",
    images: null,
    created_at: "2026-05-28T00:00:00Z",
    ...over,
  };
}

function mixedQueue(): PendingInbound[] {
  return [
    item({ id: 1, content: "check the logs" }),
    item({ id: 2, source: "agent:3", sender: "agent", content: "peer asks for a status" }),
    item({ id: 3, source: "system", sender: "system", content: "[system] compact now" }),
  ];
}

describe("userPending", () => {
  it("keeps the rows the user caused, including a page callback", () => {
    const queued = userPending([
      item({ id: 1 }),
      item({ id: 2, source: "ui:page:compare", sender: "user" }),
    ]);
    expect(queued.map((p) => p.id)).toEqual([1, 2]);
  });

  it("drops agent, automation and system rows", () => {
    const queued = userPending([
      item({ id: 1, source: "agent:3", sender: "agent" }),
      item({ id: 2, source: "schedule:7", sender: "automation" }),
      item({ id: 3, source: "watcher:5", sender: "automation" }),
      item({ id: 4, source: "system", sender: "system" }),
    ]);
    expect(queued).toEqual([]);
  });
});

describe("PendingStrip", () => {
  it("renders nothing when empty", () => {
    const { container } = render(<PendingStrip items={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("shows only the user-originated pending message of a mixed queue", () => {
    render(<PendingStrip items={mixedQueue()} />);
    expect(screen.getByText("1 pending")).toBeTruthy();
    expect(screen.getByText("check the logs")).toBeTruthy();
    expect(screen.queryByText("peer asks for a status")).toBeNull();
    expect(screen.queryByText("[system] compact now")).toBeNull();
  });

  it("renders nothing when the queue holds no user message", () => {
    const { container } = render(
      <PendingStrip
        items={[
          item({ id: 1, source: "agent:3", sender: "agent", content: "peer asks for a status" }),
          item({ id: 2, source: "schedule:7", sender: "automation", content: "wake" }),
        ]}
      />,
    );
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
      <PendingStrip items={[item({ id: 1, content: "[image]", images: ["/api/agents/7/uploads/a.png"] })]} />,
    );
    expect(screen.queryByText("[image]")).toBeNull();
    // Without an image the same literal is the message's real text — keep it.
    rerender(<PendingStrip items={[item({ id: 1, content: "[image]" })]} />);
    expect(screen.getByText("[image]")).toBeTruthy();
  });
});
