// OpenTasksNoticeHost / OpenTasksNoticeDialog — the terminate open-tasks
// notice (task #3374): store-slot gating, row rendering (count / #id | title |
// status | age / more line), dismissal, and the zh catalog copy.

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import zh from "../../messages/zh.json";
import { MIN_W_0 } from "@/lib/layout";
import { useStore } from "@/lib/store";
import type { OpenTasksHint } from "@/lib/types";
import { OpenTasksNoticeDialog } from "./open-tasks-notice-dialog";
import { OpenTasksNoticeHost } from "./open-tasks-notice";

afterEach(cleanup);

// Radix Dialog calls pointer-capture / scroll APIs happy-dom lacks.
beforeAll(() => {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.scrollIntoView = () => undefined;
});

beforeEach(() => {
  useStore.setState({ openTasksNotice: null });
});

// Rows are flex segments (id / title / tail): visual spacing comes from the
// flex gap, so normalize pipes before comparing against the receipt shape.
const rowText = (row: HTMLElement) => row.textContent.replace(/\s*\|\s*/g, " | ").trim();

function makeHint(overrides: Partial<OpenTasksHint> = {}): OpenTasksHint {
  return {
    count: 2,
    more: 0,
    tasks: [
      {
        id: 12,
        title: "Ship the hint",
        status: "in_progress",
        // 2.5h old: reads "2h ago" regardless of how long the suite runs.
        updated_at: new Date(Date.now() - 150 * 60 * 1000).toISOString(),
      },
      {
        id: 7,
        title: "Close the loop",
        status: "in_progress",
        updated_at: new Date(Date.now() - 65 * 60 * 1000).toISOString(),
      },
    ],
    ...overrides,
  };
}

describe("OpenTasksNoticeHost", () => {
  it("renders nothing while the notice slot is empty", () => {
    render(<OpenTasksNoticeHost />);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows the dialog once the slot is set, dismiss button clears it", async () => {
    render(<OpenTasksNoticeHost />);
    act(() => useStore.getState().showOpenTasksNotice(makeHint()));
    expect(await screen.findByRole("dialog")).toBeTruthy();
    expect(screen.getByText("This agent still has 2 open tasks")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Got it" }));
    expect(useStore.getState().openTasksNotice).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("OpenTasksNoticeDialog", () => {
  it("renders the count title and each row as #id | title | status | age", () => {
    render(<OpenTasksNoticeDialog notice={makeHint()} onClose={() => undefined} />);
    expect(screen.getByText("This agent still has 2 open tasks")).toBeTruthy();
    const rows = screen.getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(rowText(rows[0])).toBe("#12 | Ship the hint | In progress | 2h ago");
    expect(rowText(rows[1])).toBe("#7 | Close the loop | In progress | 1h ago");
  });

  it("keeps id, status and age visible when the title overflows", () => {
    const longTitle = { ...makeHint().tasks[0], id: 99, title: "x".repeat(120) };
    render(
      <OpenTasksNoticeDialog
        notice={{ count: 1, more: 0, tasks: [longTitle] }}
        onClose={() => undefined}
      />,
    );
    const [row] = screen.getAllByRole("listitem");
    expect(row.children).toHaveLength(3);
    expect(row.children[0].className).toContain("shrink-0");
    expect(row.children[1].className).toContain(MIN_W_0);
    expect(row.children[1].className).toContain("truncate");
    expect(row.children[2].className).toContain("shrink-0");
  });

  it("renders the server-truncated five rows as-is plus the more line", () => {
    const five = Array.from({ length: 5 }, (_, i) => ({
      id: 100 + i,
      title: `task ${i}`,
      status: "in_progress" as const,
      updated_at: new Date().toISOString(),
    }));
    render(
      <OpenTasksNoticeDialog
        notice={{ count: 8, more: 3, tasks: five }}
        onClose={() => undefined}
      />,
    );
    expect(screen.getByText("This agent still has 8 open tasks")).toBeTruthy();
    // No second truncation: five rows as-is, the remainder only counted.
    expect(screen.getAllByRole("listitem")).toHaveLength(5);
    expect(screen.getByText("and 3 more")).toBeTruthy();
  });

  it("hides the more line when nothing was truncated", () => {
    render(<OpenTasksNoticeDialog notice={makeHint()} onClose={() => undefined} />);
    expect(screen.queryByText(/and \d+ more/)).toBeNull();
  });
});

describe("openTasksNotice catalog", () => {
  it("carries the zh copy", () => {
    expect(zh.openTasksNotice.title).toBe("\u8be5 agent \u4ecd\u6709 {count} \u4e2a\u672a\u5173\u95ed\u4efb\u52a1");
    expect(zh.openTasksNotice.more).toBe("\u8fd8\u6709 {more} \u4e2a");
    expect(zh.openTasksNotice.dismiss).toBe("\u77e5\u9053\u4e86");
  });
});
