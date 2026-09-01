// TaskGraph tests — the free force-directed graph + Kanban column view.
//
// Like the Graph View tests, the d3-force layout is a real, time-driven
// simulation; these tests drive it just far enough to settle card positions,
// then assert the SVG / DOM the component renders.
//
// useTasks is mocked so the view is fed a fixed task list.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TaskRow } from "@/lib/types";
import type { TasksResult } from "@/lib/use-tasks";
import { mockSetSettingCalls, resetMockSettings } from "@/test-support/user-settings-mock";

import { FORCE_DEFAULTS, FORCE_GROUPS, TASK_FORCE_GROUPS, type ForceGroup } from "./force-controls";
import { TASK_FORCE_KEY } from "./task-graph";
import { TaskGraph } from "./task-graph";

vi.mock("@/lib/use-user-settings", () => import("@/test-support/user-settings-mock"));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

const useTasks = vi.fn<(...args: string[]) => TasksResult>();
vi.mock("@/lib/use-tasks", () => ({
  useTasks: (...args: string[]) => useTasks(...args),
}));

function task(id: number, over: Partial<TaskRow> = {}): TaskRow {
  return {
    id,
    parent_id: null,
    title: `Task ${id}`,
    description: `Description for task ${id}`,
    results: null,
    status: "in_progress",
    priority: "P2",
    owner: null,
    created_by: "user",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    reminder_count: 0,
    ...over,
  };
}

function sampleTasks(): TaskRow[] {
  return [
    task(1, { title: "root", status: "ongoing" }),
    task(2, { title: "subtask-active", status: "in_progress", parent_id: 1 }),
    task(3, { title: "subtask-ip", status: "in_progress", parent_id: 1 }),
    task(4, { title: "subtask-done", status: "done", parent_id: 1 }),
    task(5, { title: "subtask-cancelled", status: "cancelled", parent_id: 1 }),
  ];
}

function ok(tasks: TaskRow[]): TasksResult {
  return { tasks, loading: false, error: false };
}

beforeEach(() => {
  useTasks.mockReset();
  // Reset persisted graph/kanban mode + toggle state (now DB-backed user
  // settings) so a previous test's mode switch or toggle doesn't leak.
  resetMockSettings();
});

afterEach(cleanup);

describe("TaskGraph (graph mode)", () => {
  it("uses full rows so graph and kanban retain task text", () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);

    expect(useTasks).toHaveBeenCalledWith("24h", "full");
  });

  it("explains task status colors", () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);

    const legend = screen.getByLabelText("Task graph legend");
    for (const label of ["In progress", "Done", "Canceled", "Root"]) {
      expect(legend.textContent).toContain(label);
    }
    // The "Uniform node size" legend line was removed (user ruling 2026-08-29):
    // the sizing itself is unchanged, the copy was clutter.
    expect(legend.textContent).not.toContain("Uniform node size");
  });

  it("renders only in-progress cards by default", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);

    // The root task (#1) is shown in graph mode (ruling 2026-08-06) so
    // top-level tasks hang under it; only subtasks used to appear.
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    expect(screen.getAllByText(/#3/).length).toBeGreaterThan(0);
    await waitFor(() => expect(screen.getAllByText(/#1/).length).toBeGreaterThan(0), { timeout: 4000 });
    // Done and Canceled cards are hidden by default.
    expect(screen.queryByText(/#4/)).toBeNull();
    expect(screen.queryByText(/#5/)).toBeNull();
  });

  it("has no swimlane band labels in graph mode", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });

    // The old swimlane labels should not appear as SVG text.
    for (const lane of ["Backlog", "In progress", "Done"]) {
      // queryByText finds text content anywhere; swimlane labels were <text>
      // elements rendered inside the SVG. After the rewrite they are gone.
      // The label may still match the status text inside a card
      // (e.g. "In progress" on task #2), so we only assert the label is
      // not rendered as an SVG <text> element.
      const svgTexts = document.querySelectorAll("svg text");
      const found = Array.from(svgTexts).some((el) => el.textContent.trim() === lane);
      expect(found).toBe(false);
    }
  });

  it("shows Graph/Kanban mode toggle", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    expect(screen.getByText("Kanban")).toBeTruthy();
    expect(screen.getByText("Graph")).toBeTruthy();
  });

  it("switches to Kanban mode and shows only visible columns", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });

    fireEvent.click(screen.getByText("Kanban"));

    // Kanban mode has columns: In Progress, Done, Canceled (the 'Open' lane
    // was dropped 2026-08-29 — tasks are born in_progress).
    // Done and Canceled columns are hidden when empty (by default Done/Canceled
    // tasks are toggled off so those lanes have zero cards).
    await waitFor(() => expect(screen.getAllByText("In progress").length).toBeGreaterThanOrEqual(1));
    expect(screen.getByText("Description for task 2")).toBeTruthy();
    // Done and Canceled lane headers do not render when empty.
  });

  it("in Kanban mode, Done column appears when Done toggle is on", async () => {
    // Use tasks where the done task has a parent_id so it shows in kanban
    // (kanban filters to parent_id !== null — subtasks only).
    const tasks: TaskRow[] = [
      task(1, { title: "root", status: "ongoing" }),
      task(2, { title: "child", status: "in_progress", parent_id: 1 }),
      task(3, { title: "done-child", status: "done", parent_id: 1 }),
    ];
    useTasks.mockReturnValue(ok(tasks));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });

    fireEvent.click(screen.getByText("Kanban"));
    // Initially Done column is hidden (no done tasks visible).
    expect(screen.getByText("Done")).toBeTruthy(); // toggle button still visible

    // Click Done toggle to show done tasks.
    fireEvent.click(screen.getByRole("button", { name: /^Done/ }));
    // Now the Done lane header and card should appear.
    await waitFor(() => expect(screen.getAllByText(/#3/).length).toBeGreaterThan(0), { timeout: 4000 });
  });

  it("shows Done tasks when the Done toggle is clicked (graph mode)", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);

    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    // #4 (done) is hidden.
    expect(screen.queryByText(/#4/)).toBeNull();

    // Click the Done toggle to reveal Done tasks.
    fireEvent.click(screen.getByRole("button", { name: /^Done/ }));
    await waitFor(() => expect(screen.getAllByText(/#4/).length).toBeGreaterThan(0), { timeout: 4000 });
  });

  it("shows Canceled tasks when the Canceled toggle is clicked (graph mode)", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);

    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    // #5 (cancelled) is hidden.
    expect(screen.queryByText(/#5/)).toBeNull();

    // Click the Canceled toggle to reveal Canceled tasks. The legend now
    // also spells it "Canceled", so target the toggle button by role.
    fireEvent.click(screen.getByRole("button", { name: /^Canceled/ }));
    await waitFor(() => expect(screen.getAllByText(/#5/).length).toBeGreaterThan(0), { timeout: 4000 });
  });

  it("selects a card on single click", async () => {
    const onSelect = vi.fn();
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={onSelect} />);

    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const text = texts[0];
    const card = text.closest("g")!;

    fireEvent.click(card);
    expect(onSelect).toHaveBeenCalledWith(2);
  });

  it("clicking an already-selected card is a no-op (does not deselect)", async () => {
    // Unified with the Agent Graph interaction: re-clicking the selected node
    // keeps the selection; the SVG background click deselects instead.
    const onSelect = vi.fn();
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={2} onSelectTask={onSelect} />);

    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const text = texts[0];
    fireEvent.click(text.closest("g")!);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("renders parent-child edges hanging under the root node", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText(/#1/).length).toBeGreaterThan(0), { timeout: 4000 });

    // The root task (#1) is a visible node now (ruling 2026-08-06), so its
    // visible children (#2, #3 in_progress) each render a parent→child
    // edge; #4/#5 are hidden by the status toggles.
    // Query only the main graph SVG, not icon SVGs in toolbar buttons.
    const mainSvg = container.querySelector("svg[role='img']");
    const edgeLines = mainSvg ? mainSvg.querySelectorAll("g > line") : [];
    expect(edgeLines.length).toBe(2);
  });

  it("renders a toggle-hidden done parent as a ghost node so its child is not a fake orphan (#1848)", async () => {
    // #1848 repro: child 1848 is in_progress, its parent 1815 exists in the
    // registry but is done — hidden while the Done toggle is off. Before the
    // ghost fix the child dangled with no visible parent ("orphan").
    const tasks: TaskRow[] = [
      task(1, { title: "root", status: "ongoing" }),
      task(1815, { title: "done-parent", status: "done", parent_id: 1 }),
      task(1848, { title: "child", status: "in_progress", parent_id: 1815 }),
    ];
    useTasks.mockReturnValue(ok(tasks));
    const { container } = render(
      <TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    await waitFor(() => expect(screen.getAllByText(/#1848/).length).toBeGreaterThan(0), { timeout: 4000 });

    // The hidden parent renders as a dimmed ghost node.
    const ghostTexts = screen.getAllByText(/#1815/);
    expect(ghostTexts.length).toBeGreaterThan(0);
    const ghostGroup = ghostTexts[0].closest("g")!;
    expect(ghostGroup.classList.contains("opacity-40")).toBe(true);
    // The real child node is not dimmed.
    const childGroup = screen.getAllByText(/#1848/)[0].closest("g")!;
    expect(childGroup.classList.contains("opacity-40")).toBe(false);

    // Both tree edges render: root → done-parent → child.
    const mainSvg = container.querySelector("svg[role='img']");
    const edgeLines = mainSvg ? mainSvg.querySelectorAll("g > line") : [];
    expect(edgeLines.length).toBe(2);
  });

  it("walks a chain of hidden ancestors so a deep subtree stays connected", async () => {
    const tasks: TaskRow[] = [
      task(1, { title: "root", status: "ongoing" }),
      task(10, { title: "g-cancelled", status: "cancelled", parent_id: 1 }),
      task(11, { title: "g-done", status: "done", parent_id: 10 }),
      task(12, { title: "leaf", status: "in_progress", parent_id: 11 }),
    ];
    useTasks.mockReturnValue(ok(tasks));
    const { container } = render(
      <TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    await waitFor(() => expect(screen.getAllByText(/#12/).length).toBeGreaterThan(0), { timeout: 4000 });
    // Both hidden ancestors render as ghosts (their id tspans).
    for (const id of [10, 11]) {
      const g = screen.getAllByText(new RegExp(`#${String(id)}`))[0].closest("g")!;
      expect(g.classList.contains("opacity-40")).toBe(true);
    }
    // Full chain edges: 1→10, 10→11, 11→12.
    const mainSvg = container.querySelector("svg[role='img']");
    expect(mainSvg!.querySelectorAll("g > line").length).toBe(3);
  });

  it("does not ghost a hidden done task with no visible children", async () => {
    useTasks.mockReturnValue(ok(sampleTasks())); // #4 done / #5 cancelled are leaves
    const { container } = render(
      <TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    // Hidden leaves stay hidden — only structural parents become ghosts.
    expect(screen.queryByText(/#4/)).toBeNull();
    expect(screen.queryByText(/#5/)).toBeNull();
    const mainSvg = container.querySelector("svg[role='img']");
    expect(mainSvg!.querySelectorAll("g.opacity-40").length).toBe(0);
  });

  it("empty task list (not loading, no error) shows the empty placeholder", () => {
    useTasks.mockReturnValue({ tasks: [], loading: false, error: false });
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    expect(screen.getByText("No tasks yet.")).toBeTruthy();
  });

  it("cold error (no tasks loaded) shows the error placeholder", () => {
    useTasks.mockReturnValue({ tasks: [], loading: false, error: true });
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    expect(screen.getByText("Failed to load tasks.")).toBeTruthy();
  });

  it("stale-while-error: error WITH tasks keeps the board (not the failure screen) and flags stale", async () => {
    // A poll failed but tasks are already loaded — the board must stay, with a
    // lightweight "stale" flag instead of being replaced by the error screen.
    useTasks.mockReturnValue({ tasks: sampleTasks(), loading: false, error: true });
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);

    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    expect(screen.queryByText("Failed to load tasks.")).toBeNull();
    expect(screen.getByText("Stale")).toBeTruthy();
  });
});

/** Mock the hover card's offsetWidth/offsetHeight (the role="tooltip"
 *  element) at the given size; every other element keeps its real values.
 *  Returns a restore function. */
function mockCardSize(width: number, height: number): () => void {
  const proto = HTMLElement.prototype;
  // Typed through a shim so the descriptor's getter returns number.
  const widthDesc = Object.getOwnPropertyDescriptor({ offsetWidth: 0 }, "offsetWidth");
  const heightDesc = Object.getOwnPropertyDescriptor({ offsetHeight: 0 }, "offsetHeight");
  const isCard = function (this: HTMLElement) {
    return this.getAttribute("role") === "tooltip";
  };
  Object.defineProperty(proto, "offsetWidth", {
    configurable: true,
    get(this: HTMLElement) {
      if (isCard.call(this)) return width;
      // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment -- DOM descriptor getters are untyped in lib.dom; narrowed immediately below
      const real = widthDesc?.get?.call(this);
      return typeof real === "number" ? real : 0;
    },
  });
  Object.defineProperty(proto, "offsetHeight", {
    configurable: true,
    get(this: HTMLElement) {
      if (isCard.call(this)) return height;
      // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment -- DOM descriptor getters are untyped in lib.dom; narrowed immediately below
      const real = heightDesc?.get?.call(this);
      return typeof real === "number" ? real : 0;
    },
  });
  return () => {
    if (widthDesc) Object.defineProperty(proto, "offsetWidth", widthDesc);
    if (heightDesc) Object.defineProperty(proto, "offsetHeight", heightDesc);
  };
}

describe("TaskGraph hover detail card", () => {
  // The instant hover card replaces the old native SVG <title> (whose
  // appearance the browser deferred — the perceived hover lag): it must show
  // the task's registry fields the moment the cursor enters the node.
  it("shows the detail card instantly on hover, with the registry fields", async () => {
    const fullTask = task(2, {
        title: "Build the widget",
        description: "A longer description\nspanning two lines",
        results: "Shipped in #1",
        status: "in_progress",
        priority: "P1",
        parent_id: 1,
        owner: 30,
        owner_label: "builder",
        created_by: "user",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-02T00:00:00Z",
        remind_interval_seconds: 7200,
        last_reminded_at: "2026-01-02T00:00:00Z",
        reminder_count: 3,
      });
    const tasks: TaskRow[] = [
      task(1, { title: "root", status: "ongoing" }),
      fullTask,
    ];
    useTasks.mockReturnValue(ok(tasks));
    const { container } = render(
      <TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );

    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const group = texts[0].closest("g")!;

    // No native <title> remains — the delayed browser tooltip is gone.
    expect(container.querySelectorAll("svg title").length).toBe(0);

    // The card appears synchronously with the mouseenter (no delay timer).
    fireEvent.mouseEnter(group);
    expect(screen.queryByRole("tooltip")).not.toBeNull();
    const card = screen.getByRole("tooltip");
    const text = card.textContent;
    expect(text).toContain("Build the widget");
    expect(text).toContain("Task #2");
    expect(text).toContain("In progress");
    expect(text).toContain("P1");
    expect(text).toContain("builder");
    expect(text).toContain("#30");
    expect(text).toContain("User"); // created_by
    expect(text).toContain("root (#1)"); // parent
    expect(text).toContain("every 2h"); // remind interval
    expect(text).toContain("3 reminders");
    expect(text).toContain("A longer description");
    expect(text).toContain("Shipped in #1");
    expect(text).toContain("Double-click the node to open the owner");
  });

  it("hides the card on mouseleave (non-capped card hides immediately)", async () => {
    // A roomy canvas puts the card in the beside-node state (not height
    // capped) — the non-interactive card must hide the moment the pointer
    // leaves the node (the capped card instead gets a grace window so its
    // scroll stays reachable; covered by its own test below).
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(
      <TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const group = texts[0].closest("g")!;
    const canvas = container.querySelector("svg[role='img']")!.parentElement!;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 0, width: 600, height: 400 }),
    );
    vi.spyOn(group, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 100, y: 100, width: 36, height: 36 }),
    );
    const restore = mockCardSize(200, 100);
    try {
      fireEvent.mouseEnter(group);
      expect(screen.getByRole("tooltip")).toBeTruthy();
      fireEvent.mouseLeave(group);
      expect(screen.queryByRole("tooltip")).toBeNull();
    } finally {
      restore();
    }
  });

  it("keeps the card anchored to the node while the cursor moves (no following)", async () => {
    // User ruling 2026-08-29: the detail card shows on mouseenter and hides
    // on mouseleave — it must not chase the cursor. The card is pinned to
    // the node's on-screen box; a mousemove over the node leaves it put.
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(
      <TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const group = texts[0].closest("g")!;

    fireEvent.mouseEnter(group);
    const card = screen.getByRole("tooltip");
    const leftAfterEnter = card.style.left;

    // A far-away mousemove must not move the card (jsdom boxes are all 0×0,
    // so the card sits at the clamped 4px anchor; cursor-following would
    // rewrite left to follow clientX).
    fireEvent.mouseMove(group, { clientX: 900, clientY: 900 });
    expect(screen.getByRole("tooltip")).toBeTruthy();
    expect(card.style.left).toBe(leftAfterEnter);
    expect(card.style.left).toBe("4px");
  });

  it("flips beside the node's LEFT edge so the card never covers the hovered node", async () => {
    // QA #990 BLOCK regression: the flip baseline must be the node's left
    // edge — the old formula anchored the flip at the right edge and parked
    // the card ON the hovered node (~70% overlap on right-edge nodes).
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(
      <TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const group = texts[0].closest("g")!;
    // Canvas 300×400; the node box [250, 286] × [100, 136] sits at the right
    // edge so the 200×100 card must flip.
    const canvas = container.querySelector("svg[role='img']")!.parentElement!;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 0, width: 300, height: 400 }),
    );
    vi.spyOn(group, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 250, y: 100, width: 36, height: 36 }),
    );
    const restore = mockCardSize(200, 100);
    try {
      fireEvent.mouseEnter(group);
      const card = screen.getByRole("tooltip");
      // Default side starts at 286+14=300 and clips (300+200 > 296), so the
      // card flips: right edge at the node's left edge minus the gap —
      // left = 250 − 14 − 200 = 36. The card [36, 236] lies fully left of
      // the node [250, 286] — zero overlap. (The old formula gave 72, which
      // covered the node's right two-thirds.)
      expect(card.style.left).toBe("36px");
      expect(card.style.top).toBe("114px"); // 100 + 14, no vertical flip
      // Not height-capped: the card stays pointer-events-none so it never
      // steals the cursor from the nodes beneath it.
      expect(card.className).toContain("pointer-events-none");
    } finally {
      restore();
    }
  });

  it("places the card vertically above a mid-band node when no horizontal side fits (QA #990: 320x390)", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(
      <TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const group = texts[0].closest("g")!;
    // 320x390 canvas; the node box [130,166] x [180,216] sits mid-band on
    // both axes. Card 200x100.
    const canvas = container.querySelector("svg[role='img']")!.parentElement!;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 0, width: 320, height: 390 }),
    );
    vi.spyOn(group, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 130, y: 180, width: 36, height: 36 }),
    );
    const restore = mockCardSize(200, 100);
    try {
      fireEvent.mouseEnter(group);
      const card = screen.getByRole("tooltip");
      // Neither side fits (right: 166+14+200 > 316; left: 130−14−200 < 4),
      // so the card goes ABOVE the node, horizontally centered:
      // top = 180 − 14 − 100 = 66; left = center(148) − 100 = 48.
      // Card [48,248] x [66,166] sits fully above node [130,166] x [180,216]
      // — zero overlap (the old code clamped to left=4 and covered it).
      expect(card.style.left).toBe("48px");
      expect(card.style.top).toBe("66px");
    } finally {
      restore();
    }
  });

  it("caps the card height when it is taller than both vertical sides (long card, short canvas)", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(
      <TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const group = texts[0].closest("g")!;
    const canvas = container.querySelector("svg[role='img']")!.parentElement!;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 0, width: 320, height: 390 }),
    );
    vi.spyOn(group, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 130, y: 180, width: 36, height: 36 }),
    );
    // Card 200x300 fits nowhere beside (320 canvas) and nowhere vertically
    // (above space 162, below space 156 — both under 300).
    const restore = mockCardSize(200, 300);
    try {
      fireEvent.mouseEnter(group);
      const card = screen.getByRole("tooltip");
      // Pinned to the roomier side (above: 162px) with the height capped so
      // the card still clears the node: top = 4, bottom = 166 = node top −
      // gap. The body scrolls instead of covering the node — and the cap
      // state flips the card to pointer-events-auto so the scroll is
      // actually reachable (QA #990 delta2).
      expect(card.style.top).toBe("4px");
      expect(card.style.maxHeight).toBe("162px");
      expect(card.style.overflowY).toBe("auto");
      expect(card.className).toContain("pointer-events-auto");
    } finally {
      restore();
    }
  });

  it("keeps the capped card open when the pointer moves from the node onto it", async () => {
    // QA #990 delta2: crossing the 14px gap between node and card must not
    // unmount the card before the pointer reaches it — the leave handler
    // checks relatedTarget and keeps the hover when the pointer lands inside
    // the card.
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(
      <TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const group = texts[0].closest("g")!;
    const canvas = container.querySelector("svg[role='img']")!.parentElement!;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 0, width: 320, height: 390 }),
    );
    vi.spyOn(group, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 130, y: 180, width: 36, height: 36 }),
    );
    const restore = mockCardSize(200, 300);
    try {
      fireEvent.mouseEnter(group);
      const card = screen.getByRole("tooltip");
      // The leave lands INSIDE the card (relatedTarget = a card child) — the
      // hover must survive so the user can scroll the clipped content.
      fireEvent.mouseLeave(group, { relatedTarget: card.firstElementChild });
      expect(screen.getByRole("tooltip")).toBeTruthy();
      // A plain leave from the card itself hides it.
      fireEvent.mouseLeave(card);
      expect(screen.queryByRole("tooltip")).toBeNull();
    } finally {
      restore();
    }
  });

  it("gives the pointer a grace window to reach the capped card, then hides", async () => {
    // When the leave lands mid-gap (not on the card), the card must not
    // vanish instantly — a 200ms grace window covers the crossing, and the
    // card's own mouseenter cancels the pending hide.
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(
      <TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const group = texts[0].closest("g")!;
    const canvas = container.querySelector("svg[role='img']")!.parentElement!;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 0, width: 320, height: 390 }),
    );
    vi.spyOn(group, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 130, y: 180, width: 36, height: 36 }),
    );
    const restore = mockCardSize(200, 300);
    vi.useFakeTimers();
    try {
      fireEvent.mouseEnter(group);
      const card = screen.getByRole("tooltip");
      fireEvent.mouseLeave(group); // relatedTarget null → grace timer armed
      expect(screen.getByRole("tooltip")).toBeTruthy(); // not hidden yet
      // Entering the card within the grace window cancels the hide.
      fireEvent.mouseEnter(card);
      act(() => {
        vi.advanceTimersByTime(300);
      });
      expect(screen.getByRole("tooltip")).toBeTruthy();
      // Leaving the card hides it immediately.
      fireEvent.mouseLeave(card);
      expect(screen.queryByRole("tooltip")).toBeNull();
    } finally {
      vi.useRealTimers();
      restore();
    }
  });

  it("hides the capped card when the grace window expires without reaching it", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(
      <TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const group = texts[0].closest("g")!;
    const canvas = container.querySelector("svg[role='img']")!.parentElement!;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 0, width: 320, height: 390 }),
    );
    vi.spyOn(group, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 130, y: 180, width: 36, height: 36 }),
    );
    const restore = mockCardSize(200, 300);
    vi.useFakeTimers();
    try {
      fireEvent.mouseEnter(group);
      fireEvent.mouseLeave(group); // grace timer armed
      expect(screen.getByRole("tooltip")).toBeTruthy();
      act(() => {
        vi.advanceTimersByTime(250);
      });
      expect(screen.queryByRole("tooltip")).toBeNull();
    } finally {
      vi.useRealTimers();
      restore();
    }
  });

  it("shows empty states for unset fields and a plain parent id for orphans", async () => {
    const tasks: TaskRow[] = [
      task(1, { title: "root", status: "ongoing" }),
      task(2, { title: "orphan", parent_id: 999 }), // parent not in the registry
    ];
    useTasks.mockReturnValue(ok(tasks));
    render(
      <TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    const texts = await waitFor(() => { const r = screen.getAllByText(/orphan/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });

    fireEvent.mouseEnter(texts[0].closest("g")!);
    const card = screen.getByRole("tooltip");
    const text = card.textContent;
    expect(text).toContain("Unowned"); // no owner
    expect(text).toContain("#999"); // orphan parent falls back to the id
    // No reminder configured: the cadence row shows the empty placeholder and
    // no reminder history line appears.
    expect(text).toContain("Reminder");
    expect(text).not.toContain("every");
  });

  it("shows agent-created tasks by agent id", async () => {
    const tasks: TaskRow[] = [
      task(1, { title: "root", status: "ongoing" }),
      task(2, { title: "agent-made", parent_id: 1, created_by: "405", owner: 405 }),
    ];
    useTasks.mockReturnValue(ok(tasks));
    render(
      <TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    // "agent-made" wraps at its hyphen into two tspans; match the first line.
    const texts = await waitFor(() => { const r = screen.getAllByText(/^agent-$/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });

    fireEvent.mouseEnter(texts[0].closest("g")!);
    const text = screen.getByRole("tooltip").textContent;
    expect(text).toContain("Agent #405"); // created_by: "405" → "Agent #405"
    expect(text).toContain("Agent #405"); // owner: no label → agent id
  });
});

describe("TaskGraph layout controls", () => {
  // The task graph renders through the SHARED ForceGraph, but it is an
  // INDEPENDENT UI from the Agent Graph (user ruling 2026-08-10 #1127):
  // - its own persisted tuning key (display.task_force_params.v2) — changing
  //   one graph never touches the other's settings;
  // - task nodes have ONE size (no min/max band) — the Node group is a single
  //   "Size" slider, and every node sits at it (score is always 0).
  const surface = (groups: ForceGroup[]) =>
    groups.map((g) => ({ label: g.label, sliders: g.sliders.map((s) => `${s.key}:${s.label}`) }));

  it("Node group is a single Size slider — no min/max band (ruling 2026-08-10 #1127)", () => {
    const nodeGroup = TASK_FORCE_GROUPS.find((g) => g.label === "Node")!;
    expect(nodeGroup.sliders).toEqual([{ key: "nodeSizeMin", label: "Size", min: 6, max: 48, step: 1 }]);
    // the other groups are identical to the Agent Graph's
    expect(surface(TASK_FORCE_GROUPS.slice(1))).toEqual(surface(FORCE_GROUPS.slice(1)));
    expect(TASK_FORCE_GROUPS.map((g) => g.label)).toEqual(["Node", "Edge", "Layout", "Zoom"]);
  });

  it("repulsion max is at least 10000 on BOTH graphs (user ruling 2026-08-10 #1140)", () => {
    const repulsion = (groups: ForceGroup[]) =>
      groups
        .find((g) => g.label === "Layout")!
        .sliders.find((s) => s.key === "repulsion")!;
    expect(repulsion(FORCE_GROUPS).max).toBeGreaterThanOrEqual(10000);
    expect(repulsion(TASK_FORCE_GROUPS).max).toBeGreaterThanOrEqual(10000);
  });

  it("persists to its OWN key — display.task_force_params.v2, not the agent graph's", () => {
    // The task graph must never write the Agent Graph's key (ruling
    // 2026-08-10 #1127 — two independent UIs, two independent tunings).
    expect(TASK_FORCE_KEY).toBe("display.task_force_params.v2");
    expect(TASK_FORCE_KEY).not.toBe("display.graph_force_params");
  });

  it("every default sits inside its slider's range", () => {
    for (const group of [...FORCE_GROUPS, ...TASK_FORCE_GROUPS]) {
      for (const s of group.sliders) {
        const v = FORCE_DEFAULTS[s.key];
        expect(`${s.key}=${v}`).toBe(`${s.key}=${Math.min(Math.max(v, s.min), s.max)}`);
      }
    }
  });

  it("square size follows the single Size setting (own key, ruling 2026-08-10 #1127)", async () => {
    // All task nodes sit at the one size: side = 2 * nodeSizeMin. Doubling it
    // doubles the rendered squares — and the setting lives under the task
    // graph's OWN key (display.task_force_params.v2), not the agent graph's.
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(
      <TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    const rect = container.querySelector("svg[role='img'] rect")!;
    expect(rect.getAttribute("width")).toBe("36");

    cleanup();
    resetMockSettings({
      "display.task_force_params.v2": { ...FORCE_DEFAULTS, nodeSizeMin: 36 },
    });
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container: c2 } = render(
      <TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    const rect2 = c2.querySelector("svg[role='img'] rect")!;
    expect(rect2.getAttribute("width")).toBe("72");
  });

  it("task nodes are uniform size regardless of subtree size (user ruling 2026-08-09 #1070)", async () => {
    // Regression: the node's size used to encode its descendant count, so a
    // root with children rendered bigger than its leaves. The ruling: every
    // task node must be the same size — all sit at the minimum radius.
    useTasks.mockReturnValue(ok(sampleTasks())); // root #1 has 4 children
    const { container } = render(
      <TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    const rects = [...container.querySelectorAll("svg[role='img'] rect")];
    // The graph renders in_progress visible tasks — root #1 plus its
    // children (done/cancelled are filtered out). Root included, so under the
    // old descendant-count sizing these widths would differ.
    expect(rects.length).toBeGreaterThanOrEqual(3);
    const widths = new Set(rects.map((r) => r.getAttribute("width")));
    expect(widths.size).toBe(1);
    expect([...widths][0]).toBe("36"); // 2 × nodeSizeMin(18) — every node at min
  });

  it("insets the layout gear from the canvas edge", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    const wrapper = screen.getByLabelText("Graph layout settings").parentElement!;
    expect([...wrapper.classList]).toEqual(expect.arrayContaining(["absolute", "left-3", "top-3"]));
  });
});
describe("TaskGraph time filter (Task #1969)", () => {
  // The window filter is applied BACKEND-side (GET /api/tasks?window=); the
  // board's job is to select the window (DB-backed, default 24h), pass it to
  // useTasks, and render server-delivered out-of-window ancestors (ghost
  // rows) dimmed in the graph — the kanban hides them (they are graph-only
  // scaffolding).

  it("defaults to the 24h window", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    const sel = screen.getByLabelText<HTMLSelectElement>("Time window");
    expect(sel.value).toBe("24h");
    expect([...sel.options].map((o) => o.value)).toEqual(["24h", "7d", "30d", "all"]);
  });

  it("persists the window choice as a user setting", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    fireEvent.change(screen.getByLabelText("Time window"), { target: { value: "30d" } });
    expect(mockSetSettingCalls()).toContainEqual({ key: "display.task_window", value: "30d" });
  });

  it("falls back to 24h when the stored window is garbage", async () => {
    resetMockSettings({ "display.task_window": "banana" });
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    expect(screen.getByLabelText<HTMLSelectElement>("Time window").value).toBe("24h");
  });

  it("renders server-delivered out-of-window ancestors (ghost rows) dimmed in the graph", async () => {
    const tasks: TaskRow[] = [
      task(1, { title: "root", status: "ongoing" }),
      task(1815, { title: "old-parent", status: "done", parent_id: 1, ghost: true }),
      task(1848, { title: "child", status: "in_progress", parent_id: 1815 }),
    ];
    useTasks.mockReturnValue(ok(tasks));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText(/#1848/).length).toBeGreaterThan(0), { timeout: 4000 });
    // The ghost ancestor renders dimmed even though it is not a
    // toggle-hidden parent (the server flagged it).
    const ghostG = screen.getAllByText(/#1815/)[0].closest("g")!;
    expect(ghostG.classList.contains("opacity-40")).toBe(true);
    // The child is a normal node.
    expect(screen.getAllByText(/#1848/)[0].closest("g")!.classList.contains("opacity-40")).toBe(false);
  });

  it("hides ghost ancestors in the kanban (they are graph-only scaffolding)", async () => {
    const tasks: TaskRow[] = [
      task(1, { title: "root", status: "ongoing" }),
      task(1815, { title: "old-parent", status: "done", parent_id: 1, ghost: true }),
      task(1848, { title: "child", status: "in_progress", parent_id: 1815 }),
    ];
    useTasks.mockReturnValue(ok(tasks));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText(/#1848/).length).toBeGreaterThan(0), { timeout: 4000 });
    fireEvent.click(screen.getByText("Kanban"));
    await screen.findByText(/child/);
    expect(screen.queryByText(/old-parent/)).toBeNull();
  });
});
