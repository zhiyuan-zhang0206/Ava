// TaskGraph tests — the free force-directed graph + Kanban column view.
//
// Like the Graph View tests, the d3-force layout is a real, time-driven
// simulation; these tests drive it just far enough to settle card positions,
// then assert the SVG / DOM the component renders.
//
// useTasks is mocked so the view is fed a fixed task list.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentRow, OpenNotice, TaskRow } from "@/lib/types";
import type { TasksResult } from "@/lib/use-tasks";
import { resetMockSettings } from "@/test-support/user-settings-mock";

import { FORCE_DEFAULTS, FORCE_GROUPS, TASK_FORCE_GROUPS, type ForceGroup } from "./force-controls";
import { TASK_FORCE_KEY } from "./task-graph";
import { TaskGraph } from "./task-graph";

vi.mock("@/lib/use-user-settings", () => import("@/test-support/user-settings-mock"));

// Default agent roster for renders — empty: no needs-you signal anywhere.
function agents(): AgentRow[] {
  return [];
}

function openNotice(id: number, priority: OpenNotice["priority"]): OpenNotice {
  return {
    id,
    title: `Notice ${id}`,
    content: null,
    priority,
    require_response: true,
    blocking: false,
    created_at: "2026-01-01T00:00:00Z",
  };
}

function fleetAgent(agentId: number, notices: OpenNotice[]): AgentRow {
  return {
    agent_id: agentId,
    spawner: "user",
    fork_source_agent_id: null,
    fork_source_checkpoint_id: null,
    status: "running",
    pid: null,
    spawned_at: "2026-01-01T00:00:00Z",
    started_at: null,
    last_active_at: "2026-01-01T00:00:00Z", last_inbound_at: "2026-01-01T00:00:00Z",
    label: null,
    machine: "test",
    supports_vision: true,
    notices_awaiting_response: notices,
    unread_notice_count: 0,
    heartbeat_paused_until: null,
    liveness_state: "online",
    last_probe_at: null,
  };
}

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

const useTasks = vi.fn<() => TasksResult>();
vi.mock("@/lib/use-tasks", () => ({
  useTasks: () => useTasks(),
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
  it("explains task status colors and uniform sizing", () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph agents={agents()} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);

    const legend = screen.getByLabelText("Task graph legend");
    for (const label of ["In progress", "Done", "Canceled", "Root"]) {
      expect(legend.textContent).toContain(label);
    }
    expect(legend.textContent).toContain("Uniform node size");
  });

  it("renders only in-progress cards by default", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph agents={agents()} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);

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
    render(<TaskGraph agents={agents()} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
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
    render(<TaskGraph agents={agents()} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    expect(screen.getByText("Kanban")).toBeTruthy();
    expect(screen.getByText("Graph")).toBeTruthy();
  });

  it("switches to Kanban mode and shows only visible columns", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph agents={agents()} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });

    fireEvent.click(screen.getByText("Kanban"));

    // Kanban mode has columns: In Progress, Done, Canceled (the 'Open' lane
    // was dropped 2026-08-29 — tasks are born in_progress).
    // Done and Canceled columns are hidden when empty (by default Done/Canceled
    // tasks are toggled off so those lanes have zero cards).
    await waitFor(() => expect(screen.getAllByText("In progress").length).toBeGreaterThanOrEqual(1));
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
    render(<TaskGraph agents={agents()} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
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
    render(<TaskGraph agents={agents()} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);

    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    // #4 (done) is hidden.
    expect(screen.queryByText(/#4/)).toBeNull();

    // Click the Done toggle to reveal Done tasks.
    fireEvent.click(screen.getByRole("button", { name: /^Done/ }));
    await waitFor(() => expect(screen.getAllByText(/#4/).length).toBeGreaterThan(0), { timeout: 4000 });
  });

  it("shows Canceled tasks when the Canceled toggle is clicked (graph mode)", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph agents={agents()} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);

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
    render(<TaskGraph agents={agents()} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={onSelect} />);

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
    render(<TaskGraph agents={agents()} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={2} onSelectTask={onSelect} />);

    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const text = texts[0];
    fireEvent.click(text.closest("g")!);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("renders parent-child edges hanging under the root node", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(<TaskGraph agents={agents()} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText(/#1/).length).toBeGreaterThan(0), { timeout: 4000 });

    // The root task (#1) is a visible node now (ruling 2026-08-06), so its
    // visible children (#2, #3 in_progress) each render a parent→child
    // edge; #4/#5 are hidden by the status toggles.
    // Query only the main graph SVG, not icon SVGs in toolbar buttons.
    const mainSvg = container.querySelector("svg[role='img']");
    const edgeLines = mainSvg ? mainSvg.querySelectorAll("g > line") : [];
    expect(edgeLines.length).toBe(2);
  });

  it("empty task list (not loading, no error) shows the empty placeholder", () => {
    useTasks.mockReturnValue({ tasks: [], loading: false, error: false });
    render(<TaskGraph agents={agents()} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    expect(screen.getByText("No tasks yet.")).toBeTruthy();
  });

  it("cold error (no tasks loaded) shows the error placeholder", () => {
    useTasks.mockReturnValue({ tasks: [], loading: false, error: true });
    render(<TaskGraph agents={agents()} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    expect(screen.getByText("Failed to load tasks.")).toBeTruthy();
  });

  it("stale-while-error: error WITH tasks keeps the board (not the failure screen) and flags stale", async () => {
    // A poll failed but tasks are already loaded — the board must stay, with a
    // lightweight "stale" flag instead of being replaced by the error screen.
    useTasks.mockReturnValue({ tasks: sampleTasks(), loading: false, error: true });
    render(<TaskGraph agents={agents()} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);

    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    expect(screen.queryByText("Failed to load tasks.")).toBeNull();
    expect(screen.getByText("Stale")).toBeTruthy();
  });
});

describe("TaskGraph hover detail card", () => {
  // The instant hover card replaces the old native SVG <title> (whose
  // appearance the browser deferred — the perceived hover lag): it must show
  // the task's registry fields the moment the cursor enters the node.
  it("shows the detail card instantly on hover, with the registry fields", async () => {
    const tasks: TaskRow[] = [
      task(1, { title: "root", status: "ongoing" }),
      task(2, {
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
      }),
    ];
    useTasks.mockReturnValue(ok(tasks));
    const { container } = render(
      <TaskGraph agents={[]} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
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

  it("hides the card on mouseleave", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(
      <TaskGraph agents={[]} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });

    fireEvent.mouseEnter(texts[0].closest("g")!);
    expect(screen.getByRole("tooltip")).toBeTruthy();
    fireEvent.mouseLeave(texts[0].closest("g")!);
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("shows empty states for unset fields and a plain parent id for orphans", async () => {
    const tasks: TaskRow[] = [
      task(1, { title: "root", status: "ongoing" }),
      task(2, { title: "orphan", parent_id: 999 }), // parent not in the registry
    ];
    useTasks.mockReturnValue(ok(tasks));
    render(
      <TaskGraph agents={[]} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
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
      <TaskGraph agents={[]} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
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
      <TaskGraph agents={agents()} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
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
      <TaskGraph agents={agents()} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
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
      <TaskGraph agents={agents()} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
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
    render(<TaskGraph agents={agents()} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    const wrapper = screen.getByLabelText("Graph layout settings").parentElement!;
    expect([...wrapper.classList]).toEqual(expect.arrayContaining(["absolute", "left-3", "top-3"]));
  });
});

describe("TaskGraph needs-you (RCS cut 3)", () => {
  // Root #1 → #2 (owner 30, two pending P1/P3 notices) and #3 (owner 40, quiet).
  function needyFixture(): { tasks: TaskRow[]; roster: AgentRow[] } {
    return {
      tasks: [
        task(1, { title: "root" }),
        task(2, { title: "needy", parent_id: 1, owner: 30, status: "in_progress" }),
        task(3, { title: "quiet", parent_id: 1, owner: 40, status: "in_progress" }),
      ],
      roster: [
        fleetAgent(30, [openNotice(1, "P1"), openNotice(2, "P3")]),
        fleetAgent(40, []),
      ],
    };
  }

  it("shows a needs-you badge on the graph card of a task whose agent waits", async () => {
    const { tasks, roster } = needyFixture();
    useTasks.mockReturnValue(ok(tasks));
    const { container } = render(
      <TaskGraph agents={roster} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    const badge = container.querySelector('g[aria-label="2 waiting on you"]');
    expect(badge).not.toBeNull();
    // The quiet task carries no badge.
    expect(container.querySelectorAll('g[aria-label$="waiting on you"]').length).toBe(1);
  });

  it("shows the badge on the kanban card too", async () => {
    const { tasks, roster } = needyFixture();
    useTasks.mockReturnValue(ok(tasks));
    render(
      <TaskGraph agents={roster} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    fireEvent.click(screen.getByText("Kanban"));
    const badge = await screen.findByLabelText("2 waiting on you");
    expect(badge.textContent).toBe("2");
  });

  it("Needs you toggle filters the board to needy subtrees", async () => {
    const { tasks, roster } = needyFixture();
    useTasks.mockReturnValue(ok(tasks));
    render(
      <TaskGraph agents={roster} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    // The node renders the title as its own text line (like the Agent Graph's
    // label line), so the title tspans carry "needy" / "quiet".
    await waitFor(() => expect(screen.getAllByText("needy").length).toBeGreaterThan(0), { timeout: 4000 });
    expect(screen.getAllByText("quiet").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByText("Needs you"));
    // Quiet task #3 disappears; needy #2 stays.
    await waitFor(() => expect(screen.queryByText("quiet")).toBeNull(), { timeout: 4000 });
    expect(screen.getAllByText("needy").length).toBeGreaterThan(0);
    // No localStorage assertion: this suite's environment has no localStorage
    // (the component swallows that), so persistence is asserted elsewhere.
  });

  it("filter on with nothing pending keeps the toggle reachable", async () => {
    const { tasks } = needyFixture();
    useTasks.mockReturnValue(ok(tasks));
    render(
      <TaskGraph agents={[fleetAgent(30, []), fleetAgent(40, [])]} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    fireEvent.click(screen.getByText("Needs you"));

    expect(screen.getByText("Nothing needs you right now.")).toBeTruthy();
    // The toggle survives the empty state — turning it off restores the board.
    fireEvent.click(screen.getByText("Needs you"));
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
  });

  it("status-hidden needy task: empty state keeps Done toggle reachable and reveals it", async () => {
    // A done-but-needy orphan (#2, parent missing) plus a quiet live sibling
    // (#3). With Needs you on, #3 drops (not needy) and #2 drops (Done hidden)
    // -> the board is empty while decisions are pending behind status filters.
    const tasks = [
      task(2, { title: "fin", parent_id: 1, owner: 30, status: "done" }),
      task(3, { title: "alive", parent_id: 1, owner: 40, status: "in_progress" }),
    ];
    const roster = [fleetAgent(30, [openNotice(1, "P1")]), fleetAgent(40, [])];
    useTasks.mockReturnValue(ok(tasks));
    render(
      <TaskGraph agents={roster} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    await waitFor(() => expect(screen.getAllByText("alive").length).toBeGreaterThan(0), { timeout: 4000 });

    fireEvent.click(screen.getByText("Needs you"));
    // Honest empty state: pending work exists but is status-hidden.
    expect(screen.getByText(/Pending decisions sit on Done\/Canceled tasks/)).toBeTruthy();
    // The full filter toolbar survives: reveal the hidden needy task via Done.
    fireEvent.click(screen.getByRole("button", { name: /^Done/ }));
    await waitFor(() => expect(screen.getAllByText("fin").length).toBeGreaterThan(0), { timeout: 4000 });
  });

  it("inactive toggle shows the board-wide pending total", async () => {
    const { tasks, roster } = needyFixture();
    useTasks.mockReturnValue(ok(tasks));
    render(
      <TaskGraph agents={roster} selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    const toggle = screen.getByText("Needs you").closest("button")!;
    expect(toggle.textContent).toContain("2");
  });
});
