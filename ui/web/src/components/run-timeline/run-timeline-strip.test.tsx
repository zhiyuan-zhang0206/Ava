import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { runTimelineLabels } from "@/test-support/run-timeline-labels";
import type { RunTimelineMessage } from "@/lib/types";

import { StripTrackButtons, StripTrackGeometry, StripTruncatedHint } from "./run-timeline-strip";
import { buildStripLayout } from "./strip-layout";

// jsdom has no PointerEvent; the hover callback tests need pointer events,
// so give them a MouseEvent body (same polyfill as the chart tests).
if (typeof window !== "undefined" && typeof window.PointerEvent === "undefined") {
  Object.defineProperty(window, "PointerEvent", {
    value: class PointerEventPolyfill extends MouseEvent {},
    configurable: true,
  });
}

const labels = runTimelineLabels();
const WINDOW = { from: "2026-09-19T00:00:00Z", to: "2026-09-19T01:00:00Z" };
const PLOT = { left: 32, width: 968 };

function message(
  overrides: Partial<RunTimelineMessage> & Pick<RunTimelineMessage, "key">,
): RunTimelineMessage {
  return {
    idx: Number(overrides.key.split(".").at(-1) ?? 1),
    ts: "2026-09-19T00:10:00Z",
    kind: "ai",
    source: null,
    chars: 100,
    parts: [{ kind: "text", chars: 100 }],
    ...overrides,
  };
}

const messages: RunTimelineMessage[] = [
  message({
    key: "c.0",
    idx: 0,
    ts: null,
    kind: "prompt",
    chars: 400,
    parts: [{ kind: "prompt", chars: 400 }],
  }),
  message({
    key: "c.1",
    kind: "ai",
    chars: 300,
    parts: [
      { kind: "think", chars: 100 },
      { kind: "text", chars: 200 },
    ],
  }),
  message({
    key: "c.2",
    ts: "2026-09-19T00:20:00Z",
    kind: "inbound",
    source: "user",
    chars: 100,
    parts: [{ kind: "inbound", chars: 100 }],
  }),
  message({
    key: "c.3",
    ts: "2026-09-19T00:40:00Z",
    kind: "exec",
    chars: 200,
    parts: [{ kind: "out", chars: 200 }],
  }),
];

const strip = buildStripLayout(messages, WINDOW, PLOT);
const row = { top: 120, height: 26, messages: strip.messages };

describe("StripTrackGeometry", () => {
  it("renders one rect per part with the mapped color class", () => {
    render(
      <svg>
        <StripTrackGeometry
          plot={PLOT}
          row={row}
          messages={messages}
          selectedIndex={null}
          relatedIndexes={new Set()}
          activeCategory={null}
        />
      </svg>,
    );
    const parts = screen.getAllByTestId("strip-part");
    expect(parts.map((part) => part.getAttribute("data-strip-color"))).toEqual([
      "prompt",
      "think",
      "text",
      "ib-user",
      "out",
    ]);
    for (const part of parts) {
      expect(part.getAttribute("fill")).toBe(
        `var(--strip-${part.getAttribute("data-strip-color")})`,
      );
    }
  });

  it("dims every part that does not match the active category", () => {
    render(
      <svg>
        <StripTrackGeometry
          plot={PLOT}
          row={row}
          messages={messages}
          selectedIndex={null}
          relatedIndexes={new Set()}
          activeCategory="think"
        />
      </svg>,
    );
    const byClass = (colorClass: string) =>
      screen
        .getAllByTestId("strip-part")
        .filter((part) => part.getAttribute("data-strip-color") === colorClass);
    expect(byClass("think").every((part) => part.getAttribute("opacity") === "1")).toBe(true);
    expect(
      [...byClass("prompt"), ...byClass("text"), ...byClass("out")].every(
        (part) => part.getAttribute("opacity") === "0.12",
      ),
    ).toBe(true);
  });

  it("outlines the selected message and the related ones", () => {
    const { rerender } = render(
      <svg>
        <StripTrackGeometry
          plot={PLOT}
          row={row}
          messages={messages}
          selectedIndex={null}
          relatedIndexes={new Set()}
          activeCategory={null}
        />
      </svg>,
    );
    expect(screen.queryByTestId("strip-message-selected")).toBeNull();
    expect(screen.queryByTestId("strip-message-related")).toBeNull();

    rerender(
      <svg>
        <StripTrackGeometry
          plot={PLOT}
          row={row}
          messages={messages}
          selectedIndex={1}
          relatedIndexes={new Set([3])}
          activeCategory={null}
        />
      </svg>,
    );
    expect(screen.getByTestId("strip-message-selected").getAttribute("data-message-index")).toBe("1");
    const related = screen.getAllByTestId("strip-message-related");
    expect(related).toHaveLength(1);
    expect(related[0].getAttribute("data-message-index")).toBe("3");
  });

  it("keeps the selected outline independent of the related set", () => {
    render(
      <svg>
        <StripTrackGeometry
          plot={PLOT}
          row={row}
          messages={messages}
          selectedIndex={2}
          relatedIndexes={new Set([2])}
          activeCategory={null}
        />
      </svg>,
    );
    expect(screen.getByTestId("strip-message-selected")).not.toBeNull();
    expect(screen.queryByTestId("strip-message-related")).toBeNull();
  });
});

describe("StripTrackButtons", () => {
  it("labels every message button with idx, kind, time, and chars", () => {
    render(
      <StripTrackButtons
        row={row}
        messages={messages}
        labels={labels}
        onSelect={vi.fn()}
        onFocus={vi.fn()}
      />,
    );
    const buttons = screen.getAllByTestId("strip-message-button");
    expect(buttons).toHaveLength(4);
    expect(
      screen.getByRole("button", { name: "Message 0 · system prompt · None · 400 chars" }),
    ).not.toBeNull();
    expect(
      screen.getByRole("button", { name: /Message 2 · inbound · user · .* · 100 chars/ }),
    ).not.toBeNull();
  });

  it("reports select, focus, and hover to the chart", () => {
    const onSelect = vi.fn();
    const onFocus = vi.fn();
    const onHover = vi.fn();
    render(
      <StripTrackButtons
        row={row}
        messages={messages}
        labels={labels}
        onSelect={onSelect}
        onFocus={onFocus}
        onHover={onHover}
      />,
    );
    const buttons = screen.getAllByTestId("strip-message-button");
    fireEvent.click(buttons[2]);
    expect(onSelect).toHaveBeenCalledWith(2);
    fireEvent.doubleClick(buttons[1]);
    expect(onFocus).toHaveBeenCalledWith(1);
    fireEvent.pointerEnter(buttons[3]);
    expect(onHover).toHaveBeenCalledWith(3);
    fireEvent.pointerLeave(buttons[3]);
    expect(onHover).toHaveBeenCalledWith(null);
  });

  it("omits hover wiring when the readout is not rendered", () => {
    render(
      <StripTrackButtons
        row={row}
        messages={messages}
        labels={labels}
        onSelect={vi.fn()}
        onFocus={vi.fn()}
      />,
    );
    // No hover callbacks were passed; the button still renders and stays inert.
    expect(screen.getAllByTestId("strip-message-button")).toHaveLength(4);
  });
});

describe("StripTruncatedHint", () => {
  it("renders the truncation notice below the strip row", () => {
    render(
      <StripTruncatedHint plot={PLOT} row={row} labels={labels} />,
    );
    expect(screen.getByTestId("strip-truncated").textContent).toBe(
      "Earlier messages are not shown",
    );
  });
});
