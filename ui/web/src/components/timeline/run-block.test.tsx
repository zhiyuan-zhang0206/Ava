import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { findClosestStuckHeaderId, TurnBlock } from "./run-block";
import type { TurnSummary } from "./runs";

const sampleSummary: TurnSummary = {
  total: 3,
  thinking: 1,
  code: 1,
  output: 1,
  systemPrompts: 0,
  compactSummaries: 0,
  memories: 0,
  agentMessages: 0,
  systemNotes: 0,
  workedMs: 12000,
  thinkingMs: 5000,
  codeMs: 4000,
  execMs: 3000,
  sdkCalls: [{ method: "files.read", count: 2 }],
  lastLiveKind: null,
  lastLiveStartedAt: 0,
};

function mockRect(top: number, bottom: number, height: number): DOMRect {
  return {
    top,
    bottom,
    height,
    left: 0,
    right: 400,
    width: 400,
    x: 0,
    y: top,
    toJSON: () => undefined,
  };
}

describe("findClosestStuckHeaderId", () => {
  it("returns null when no expanded turn block is in the container", () => {
    const container = document.createElement("div");
    container.innerHTML = `
      <div data-item-id="turn-1" data-turn-id="turn-1" data-turn-expanded="false"></div>
    `;
    expect(findClosestStuckHeaderId(container, 44)).toEqual({ topId: null, childId: null });
  });

  it("returns null when expanded block has not reached the sticky line", () => {
    const container = document.createElement("div");
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue(mockRect(0, 800, 800));

    const block = document.createElement("div");
    block.setAttribute("data-turn-id", "turn-1");
    block.setAttribute("data-turn-expanded", "true");
    // Block top is at 100, which is below sticky line 44
    vi.spyOn(block, "getBoundingClientRect").mockReturnValue(mockRect(100, 900, 800));
    container.appendChild(block);

    expect(findClosestStuckHeaderId(container, 44)).toEqual({ topId: null, childId: null });
  });

  it("returns turnId when expanded block has scrolled past the sticky line", () => {
    const container = document.createElement("div");
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue(mockRect(0, 800, 800));

    const block = document.createElement("div");
    block.setAttribute("data-turn-id", "turn-1");
    block.setAttribute("data-turn-expanded", "true");
    // Block top is at 20 (crossed sticky line 44), bottom is at 700 (still in view)
    vi.spyOn(block, "getBoundingClientRect").mockReturnValue(mockRect(20, 700, 680));
    container.appendChild(block);

    expect(findClosestStuckHeaderId(container, 44)).toEqual({ topId: "turn-1", childId: null });
  });

  it("returns null when block has completely scrolled past the sticky line", () => {
    const container = document.createElement("div");
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue(mockRect(0, 800, 800));

    const block = document.createElement("div");
    block.setAttribute("data-turn-id", "turn-1");
    block.setAttribute("data-turn-expanded", "true");
    // Block top is at -900, bottom is at 40 (past sticky threshold 44 + 20 = 64)
    vi.spyOn(block, "getBoundingClientRect").mockReturnValue(mockRect(-900, 40, 940));
    container.appendChild(block);

    expect(findClosestStuckHeaderId(container, 44)).toEqual({ topId: null, childId: null });
  });

  it("selects the closest/latest expanded block when multiple blocks cross the top", () => {
    const container = document.createElement("div");
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue(mockRect(0, 800, 800));

    // Block 1: earlier in document, scrolled higher (top = -400, bottom = 300)
    const block1 = document.createElement("div");
    block1.setAttribute("data-turn-id", "turn-1");
    block1.setAttribute("data-turn-expanded", "true");
    vi.spyOn(block1, "getBoundingClientRect").mockReturnValue(mockRect(-400, 300, 700));

    // Block 2: later in document, closer to sticky line (top = 10, bottom = 800)
    const block2 = document.createElement("div");
    block2.setAttribute("data-turn-id", "turn-2");
    block2.setAttribute("data-turn-expanded", "true");
    vi.spyOn(block2, "getBoundingClientRect").mockReturnValue(mockRect(10, 800, 790));

    container.appendChild(block1);
    container.appendChild(block2);

    // Block 2 has top = 10 >= Block 1's top = -400, so Block 2 is the closest stuck block
    expect(findClosestStuckHeaderId(container, 44)).toEqual({ topId: "turn-2", childId: null });
  });

  it("ignores collapsed blocks even if their top is above sticky line", () => {
    const container = document.createElement("div");
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue(mockRect(0, 800, 800));

    const block = document.createElement("div");
    block.setAttribute("data-turn-id", "turn-1");
    block.setAttribute("data-turn-expanded", "false");
    vi.spyOn(block, "getBoundingClientRect").mockReturnValue(mockRect(-100, 200, 300));
    container.appendChild(block);

    expect(findClosestStuckHeaderId(container, 44)).toEqual({ topId: null, childId: null });
  });

  it("returns the row id when a marked message-card row has crossed the sticky line", () => {
    const container = document.createElement("div");
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue(mockRect(0, 800, 800));

    const row = document.createElement("div");
    row.setAttribute("data-item-id", "2.0");
    row.setAttribute("data-card-sticky", "true");
    // Row top is at 20 (crossed sticky line 44), bottom at 900 (still in view).
    vi.spyOn(row, "getBoundingClientRect").mockReturnValue(mockRect(20, 900, 880));
    container.appendChild(row);

    expect(findClosestStuckHeaderId(container, 44)).toEqual({ topId: "2.0", childId: null });
  });

  it("ignores message-card rows that are not marked (collapsed or work-block children)", () => {
    const container = document.createElement("div");
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue(mockRect(0, 800, 800));

    const row = document.createElement("div");
    row.setAttribute("data-item-id", "2.0");
    row.setAttribute("data-card-sticky", "false");
    vi.spyOn(row, "getBoundingClientRect").mockReturnValue(mockRect(-100, 700, 800));
    container.appendChild(row);

    expect(findClosestStuckHeaderId(container, 44)).toEqual({ topId: null, childId: null });
  });

  it("picks the closest candidate across both surfaces when a card and a turn cross the line", () => {
    const container = document.createElement("div");
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue(mockRect(0, 800, 800));

    // An earlier turn scrolled higher (top = -300).
    const turn = document.createElement("div");
    turn.setAttribute("data-turn-id", "turn-1");
    turn.setAttribute("data-turn-expanded", "true");
    vi.spyOn(turn, "getBoundingClientRect").mockReturnValue(mockRect(-300, 200, 500));

    // A later message card closer to the sticky line (top = 10).
    const row = document.createElement("div");
    row.setAttribute("data-item-id", "2.0");
    row.setAttribute("data-card-sticky", "true");
    vi.spyOn(row, "getBoundingClientRect").mockReturnValue(mockRect(10, 800, 790));

    container.appendChild(turn);
    container.appendChild(row);

    expect(findClosestStuckHeaderId(container, 44)).toEqual({ topId: "2.0", childId: null });
  });

  it("reports the pinned child under its work block's header as childId, not topId", () => {
    const container = document.createElement("div");
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue(mockRect(0, 800, 800));

    const turn = document.createElement("div");
    turn.setAttribute("data-turn-id", "turn-1");
    turn.setAttribute("data-turn-expanded", "true");
    vi.spyOn(turn, "getBoundingClientRect").mockReturnValue(mockRect(10, 700, 690));

    const header = document.createElement("button");
    header.setAttribute("data-testid", "turn-toggle");
    // Pinned block header: top 44, bottom 72 ⇒ the child line is 72.
    vi.spyOn(header, "getBoundingClientRect").mockReturnValue(mockRect(44, 72, 28));

    const child = document.createElement("div");
    child.setAttribute("data-item-id", "2.0");
    child.setAttribute("data-card-sticky", "true");
    child.setAttribute("data-turn-child", "true");
    // Child top 30 crossed both lines; bottom 500 still in view. It must NOT
    // win the level-1 pass even though it is the topmost candidate there.
    vi.spyOn(child, "getBoundingClientRect").mockReturnValue(mockRect(30, 500, 470));

    turn.appendChild(header);
    turn.appendChild(child);
    container.appendChild(turn);

    expect(findClosestStuckHeaderId(container, 44)).toEqual({ topId: "turn-1", childId: "2.0" });
  });

  it("prefers the closest child when two children in one block crossed the child line", () => {
    const container = document.createElement("div");
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue(mockRect(0, 800, 800));

    const turn = document.createElement("div");
    turn.setAttribute("data-turn-id", "turn-1");
    turn.setAttribute("data-turn-expanded", "true");
    vi.spyOn(turn, "getBoundingClientRect").mockReturnValue(mockRect(10, 700, 690));

    const header = document.createElement("button");
    header.setAttribute("data-testid", "turn-toggle");
    // Pinned block header: top 44, bottom 72 ⇒ the child line is 72.
    vi.spyOn(header, "getBoundingClientRect").mockReturnValue(mockRect(44, 72, 28));

    const older = document.createElement("div");
    older.setAttribute("data-item-id", "2.0");
    older.setAttribute("data-card-sticky", "true");
    older.setAttribute("data-turn-child", "true");
    // Crossed the child line first — top 10 is the farthest past it.
    vi.spyOn(older, "getBoundingClientRect").mockReturnValue(mockRect(10, 400, 390));

    const closer = document.createElement("div");
    closer.setAttribute("data-item-id", "2.1");
    closer.setAttribute("data-card-sticky", "true");
    closer.setAttribute("data-turn-child", "true");
    // Crossed later — top 50 is the closest to the line, so it must win.
    vi.spyOn(closer, "getBoundingClientRect").mockReturnValue(mockRect(50, 400, 350));

    turn.appendChild(header);
    turn.appendChild(older);
    turn.appendChild(closer);
    container.appendChild(turn);

    expect(findClosestStuckHeaderId(container, 44)).toEqual({ topId: "turn-1", childId: "2.1" });
  });

  it("reports no child while its work block is still below the lines", () => {
    const container = document.createElement("div");
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue(mockRect(0, 800, 800));

    const turn = document.createElement("div");
    turn.setAttribute("data-turn-id", "turn-1");
    turn.setAttribute("data-turn-expanded", "true");
    vi.spyOn(turn, "getBoundingClientRect").mockReturnValue(mockRect(200, 700, 500));

    const header = document.createElement("button");
    header.setAttribute("data-testid", "turn-toggle");
    vi.spyOn(header, "getBoundingClientRect").mockReturnValue(mockRect(200, 228, 28));

    const child = document.createElement("div");
    child.setAttribute("data-item-id", "2.0");
    child.setAttribute("data-card-sticky", "true");
    child.setAttribute("data-turn-child", "true");
    vi.spyOn(child, "getBoundingClientRect").mockReturnValue(mockRect(260, 700, 440));

    turn.appendChild(header);
    turn.appendChild(child);
    container.appendChild(turn);

    expect(findClosestStuckHeaderId(container, 44)).toEqual({ topId: null, childId: null });
  });
});

describe("TurnBlock component", () => {
  it("renders collapsed turn block with data attributes and no sticky classes", () => {
    render(
      <TurnBlock
        id="turn-1"
        memberIds={["1.0", "1.1"]}
        summary={sampleSummary}
        expanded={false}
        onToggle={vi.fn()}
      >
        <div data-testid="detail-content">detail rows</div>
      </TurnBlock>,
    );

    const toggle = screen.getByTestId("turn-toggle");
    expect(toggle.getAttribute("data-expanded")).toBe("false");
    expect(toggle.getAttribute("data-stuck")).toBe("false");
    expect(toggle.className).not.toContain("sticky");
    expect(toggle.className).not.toContain("shadow-xs");
  });

  it("renders expanded turn block with sticky classes", () => {
    render(
      <TurnBlock
        id="turn-1"
        memberIds={["1.0", "1.1"]}
        summary={sampleSummary}
        expanded={true}
        isStuck={false}
        onToggle={vi.fn()}
      >
        <div data-testid="detail-content">detail rows</div>
      </TurnBlock>,
    );

    const toggle = screen.getByTestId("turn-toggle");
    expect(toggle.getAttribute("data-expanded")).toBe("true");
    expect(toggle.getAttribute("data-stuck")).toBe("false");
    expect(toggle.className).toContain("sticky");
    expect(toggle.className).toContain("top-11");
    expect(toggle.className).toContain("z-10");
  });

  it("renders stuck active styling when expanded and isStuck is true", () => {
    render(
      <TurnBlock
        id="turn-1"
        memberIds={["1.0", "1.1"]}
        summary={sampleSummary}
        expanded={true}
        isStuck={true}
        onToggle={vi.fn()}
      >
        <div data-testid="detail-content">detail rows</div>
      </TurnBlock>,
    );

    const toggle = screen.getByTestId("turn-toggle");
    expect(toggle.getAttribute("data-expanded")).toBe("true");
    expect(toggle.getAttribute("data-stuck")).toBe("true");
    expect(toggle.className).toContain("backdrop-blur-md");
    expect(toggle.className).toContain("shadow-xs");
    expect(toggle.className).toContain("border-b");
    expect(toggle.className).toContain("motion-reduce:transition-none");
  });

  it("calls onToggle when clicked in stuck state", () => {
    const onToggle = vi.fn();
    render(
      <TurnBlock
        id="turn-1"
        memberIds={["1.0", "1.1"]}
        summary={sampleSummary}
        expanded={true}
        isStuck={true}
        onToggle={onToggle}
      >
        <div data-testid="detail-content">detail rows</div>
      </TurnBlock>,
    );

    const toggle = screen.getByTestId("turn-toggle");
    fireEvent.click(toggle);
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it("observes the header's border box so stuck-border changes re-measure", () => {
    const observe = vi.fn();
    class FakeResizeObserver {
      observe = observe;
      disconnect = vi.fn();
    }
    vi.stubGlobal("ResizeObserver", FakeResizeObserver);
    try {
      render(
        <TurnBlock
          id="turn-1"
          memberIds={["1.0", "1.1"]}
          summary={sampleSummary}
          expanded={true}
          isStuck={false}
          onToggle={vi.fn()}
        >
          <div data-testid="detail-content">detail rows</div>
        </TurnBlock>,
      );
      expect(observe).toHaveBeenCalledWith(
        screen.getByTestId("turn-toggle"),
        { box: "border-box" },
      );
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
