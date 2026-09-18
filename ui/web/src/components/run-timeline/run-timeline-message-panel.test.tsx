import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { runTimelineLabels } from "@/test-support/run-timeline-labels";
import type { RunTimelineMessage, RunTimelineMessageDetails, RunTimelineResponse } from "@/lib/types";

const { getRunTimelineMessage } = vi.hoisted(() => ({
  getRunTimelineMessage: vi.fn<
    (agentId: number, key: string, options?: { full?: boolean }) => Promise<RunTimelineMessageDetails>
  >(),
}));

vi.mock("@/lib/api", () => ({ api: { getRunTimelineMessage } }));

import { MessageDetailPanel } from "./run-timeline-message-panel";

const labels = runTimelineLabels();

type LayerNode = NonNullable<RunTimelineResponse["layers"]>[number];

const message: RunTimelineMessage = {
  key: "c.7",
  idx: 7,
  ts: "2026-09-19T00:10:00Z",
  kind: "ai",
  source: null,
  chars: 400,
  parts: [
    { kind: "think", chars: 100 },
    { kind: "text", chars: 300 },
  ],
};

const chain: { index: number; node: LayerNode }[] = [
  {
    index: 0,
    node: {
      id: "overview",
      depth: 0,
      parent: null,
      start: "2026-09-19T00:00:00Z",
      end: "2026-09-19T01:00:00Z",
      summary: "overview",
    },
  },
  {
    index: 2,
    node: {
      id: "block-1",
      depth: 2,
      parent: "overview",
      start: "2026-09-19T00:05:00Z",
      end: "2026-09-19T00:15:00Z",
      summary: "block one",
    },
  },
];

function details(overrides: Partial<RunTimelineMessageDetails>): RunTimelineMessageDetails {
  return {
    key: "c.7",
    kind: "ai",
    ts: "2026-09-19T00:10:00Z",
    source: null,
    chars: 400,
    parts: [
      { kind: "think", chars: 100, text: "secret reasoning", text_truncated: false },
      { kind: "text", chars: 300, text: "hello world", text_truncated: false },
    ],
    content_truncated: false,
    ...overrides,
  };
}

function wrap(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

function panelProps() {
  return {
    agentId: 42,
    message,
    chain,
    labels,
    focusTarget: {
      kind: "time" as const,
      window: { from: "2026-09-19T00:09:00Z", to: "2026-09-19T00:11:00Z" },
    },
    onFocus: vi.fn(),
    onClose: vi.fn(),
    onSelectLayer: vi.fn(),
  };
}

beforeEach(() => {
  getRunTimelineMessage.mockReset();
});

describe("MessageDetailPanel", () => {
  it("loads the message text and renders parts with folded thinking", async () => {
    getRunTimelineMessage.mockResolvedValue(details({}));
    wrap(<MessageDetailPanel {...panelProps()} />);

    expect(await screen.findByText("hello world")).not.toBeNull();
    expect(getRunTimelineMessage).toHaveBeenCalledWith(42, "c.7", { full: false });
    // Thinking folds into a summary line (the chip row echoes the label).
    expect(screen.getAllByText("agent thinking · 100 chars").length).toBeGreaterThan(0);
    expect(screen.getByText("Message 7 · agent thinking")).not.toBeNull();
    expect(screen.getByText("Chars")).not.toBeNull();
    expect(screen.getByText("400 chars")).not.toBeNull();
  });

  it("shows the error state with a working retry", async () => {
    getRunTimelineMessage.mockRejectedValueOnce(new Error("boom"));
    wrap(<MessageDetailPanel {...panelProps()} />);
    expect(await screen.findByText("Could not load the message.")).not.toBeNull();

    getRunTimelineMessage.mockResolvedValueOnce(details({}));
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("hello world")).not.toBeNull();
  });

  it("offers a full-text refetch for clipped content", async () => {
    getRunTimelineMessage.mockResolvedValueOnce(
      details({
        content_truncated: true,
        parts: [
          { kind: "think", chars: 100, text: "secret", text_truncated: false },
          { kind: "text", chars: 300, text: "hello wor…", text_truncated: true },
        ],
      }),
    );
    getRunTimelineMessage.mockResolvedValueOnce(details({}));
    wrap(<MessageDetailPanel {...panelProps()} />);

    const expand = await screen.findByRole("button", { name: "Show full text" });
    fireEvent.click(expand);
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Show full text" })).toBeNull();
    });
    expect(getRunTimelineMessage).toHaveBeenLastCalledWith(42, "c.7", { full: true });
    expect(await screen.findByText("hello world")).not.toBeNull();
  });

  it("routes the chain chips, the summary action, and the focus action", async () => {
    getRunTimelineMessage.mockResolvedValue(details({}));
    const props = panelProps();
    wrap(<MessageDetailPanel {...props} />);
    await screen.findByText("hello world");

    fireEvent.click(screen.getByRole("button", { name: "L2#block-1" }));
    expect(props.onSelectLayer).toHaveBeenCalledWith(2);

    fireEvent.click(screen.getByRole("button", { name: "View summary block" }));
    expect(props.onSelectLayer).toHaveBeenLastCalledWith(2);

    fireEvent.click(screen.getByRole("button", { name: "Zoom to this message" }));
    expect(props.onFocus).toHaveBeenCalledWith(
      { kind: "time", window: { from: "2026-09-19T00:09:00Z", to: "2026-09-19T00:11:00Z" } },
      "Message 7",
    );
  });

  it("routes a context-axis focus target unchanged (P4-2b)", async () => {
    getRunTimelineMessage.mockResolvedValue(details({}));
    const props = panelProps();
    wrap(
      <MessageDetailPanel
        {...props}
        focusTarget={{ kind: "context", view: { from: 100, to: 400 } }}
      />,
    );
    await screen.findByText("hello world");

    fireEvent.click(screen.getByRole("button", { name: "Zoom to this message" }));
    expect(props.onFocus).toHaveBeenCalledWith(
      { kind: "context", view: { from: 100, to: 400 } },
      "Message 7",
    );
  });
});
