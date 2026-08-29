// task-notify — the pure client-side join between the notify queue / task
// board and the task registry. No network, no DOM.

import { describe, expect, it } from "vitest";

import {
  groupByTaskSubtree,
  needsYouByTask,
  rollupNeedsYou,
  subtreeRootOf,
  taskForAgent,
} from "./task-notify";
import type { AgentRow, OpenNotice, TaskRow } from "./types";

function task(id: number, over: Partial<TaskRow> = {}): TaskRow {
  return {
    id,
    parent_id: null,
    title: `Task ${id}`,
    description: "",
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

function notice(id: number, over: Partial<OpenNotice> = {}): OpenNotice {
  return {
    id,
    title: `Notice ${id}`,
    content: null,
    priority: "P2",
    require_response: true,
    blocking: false,
    created_at: "2026-01-01T00:00:00Z",
    ...over,
  };
}

function agent(agentId: number, notices: OpenNotice[]): AgentRow {
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

// Registry: root #1 → manager #2 → workers #3, #4; second subtree #5 → #6.
function registry(): TaskRow[] {
  return [
    task(1, { title: "root" }),
    task(2, { parent_id: 1, title: "manager-a", owner: 20 }),
    task(3, { parent_id: 2, title: "worker-a1", owner: 30 }),
    task(4, { parent_id: 2, title: "worker-a2", owner: 40 }),
    task(5, { parent_id: 1, title: "manager-b", owner: 50 }),
    task(6, { parent_id: 5, title: "worker-b1", owner: 60 }),
  ];
}

describe("taskForAgent", () => {
  it("finds the first real task owned by the agent", () => {
    expect(taskForAgent(registry(), 30)?.id).toBe(3);
  });

  it("never picks the system root even when the agent owns it", () => {
    const tasks = [task(1, { owner: 7 })];
    expect(taskForAgent(tasks, 7)).toBeNull();
  });

  it("returns null for an agent with no task", () => {
    expect(taskForAgent(registry(), 999)).toBeNull();
  });
});

describe("subtreeRootOf", () => {
  const byId = new Map(registry().map((t) => [t.id, t]));

  it("walks a worker up to the top-level subtree (child of root)", () => {
    expect(subtreeRootOf(byId, byId.get(3)!).id).toBe(2);
    expect(subtreeRootOf(byId, byId.get(6)!).id).toBe(5);
  });

  it("returns a top-level task unchanged", () => {
    expect(subtreeRootOf(byId, byId.get(2)!).id).toBe(2);
  });

  it("stops at a missing parent", () => {
    const orphan = task(9, { parent_id: 999 });
    const m = new Map([[9, orphan]]);
    expect(subtreeRootOf(m, orphan).id).toBe(9);
  });

  it("survives a parent cycle", () => {
    const a = task(11, { parent_id: 12 });
    const b = task(12, { parent_id: 11 });
    const m = new Map([[11, a], [12, b]]);
    expect([11, 12]).toContain(subtreeRootOf(m, a).id);
  });
});

describe("groupByTaskSubtree", () => {
  interface Item {
    agentId: number;
    tag: string;
  }
  const it3 = { agentId: 30, tag: "a1" };
  const it4 = { agentId: 40, tag: "a2" };
  const it6 = { agentId: 60, tag: "b1" };
  const loose = { agentId: 999, tag: "loose" };

  it("pulls same-subtree entries together at the first member's rank", () => {
    // Sorted order interleaves the two subtrees + a loose agent.
    const units = groupByTaskSubtree<Item>(
      [it3, it6, loose, it4],
      (i) => i.agentId,
      registry(),
    );
    expect(units.map((u) => u.root?.id ?? null)).toEqual([2, 5, null]);
    expect(units[0].items.map((i) => i.tag)).toEqual(["a1", "a2"]);
    expect(units[1].items.map((i) => i.tag)).toEqual(["b1"]);
    expect(units[2].items.map((i) => i.tag)).toEqual(["loose"]);
  });

  it("keeps loose entries flat as singleton units in place", () => {
    const units = groupByTaskSubtree<Item>(
      [loose, it3, { agentId: 998, tag: "loose2" }],
      (i) => i.agentId,
      registry(),
    );
    expect(units.map((u) => u.root?.id ?? null)).toEqual([null, 2, null]);
  });

  it("with an empty registry everything stays flat", () => {
    const units = groupByTaskSubtree<Item>([it3, it6], (i) => i.agentId, []);
    expect(units.map((u) => u.root)).toEqual([null, null]);
  });

  // taskIdOf — the item's own task link (a notice's task_id) wins over the owner-join.
  interface TItem {
    agentId: number;
    tag: string;
    taskId: number | null;
  }
  const idOf = (i: TItem) => i.taskId;

  it("groups by the item's task_id, overriding the owner-join", () => {
    // agent 30's owner-join is task 3 (subtree root 2); the item names task 6 (root 5).
    const units = groupByTaskSubtree<TItem>(
      [{ agentId: 30, tag: "x", taskId: 6 }],
      (i) => i.agentId,
      registry(),
      idOf,
    );
    expect(units.map((u) => u.root?.id ?? null)).toEqual([5]);
  });

  it("falls back to the owner-join when task_id is null or unknown", () => {
    const units = groupByTaskSubtree<TItem>(
      [
        { agentId: 30, tag: "null", taskId: null },
        { agentId: 30, tag: "unknown", taskId: 9999 },
      ],
      (i) => i.agentId,
      registry(),
      idOf,
    );
    // both fall back to agent 30's owned task 3 → subtree root 2, one group
    expect(units.map((u) => u.root?.id ?? null)).toEqual([2]);
    expect(units[0].items.map((i) => i.tag)).toEqual(["null", "unknown"]);
  });

  it("groups a loose agent's entry by a valid task_id", () => {
    // agent 999 owns no task, but the item names task 6 → subtree root 5 (not loose).
    const units = groupByTaskSubtree<TItem>(
      [{ agentId: 999, tag: "loose-but-named", taskId: 6 }],
      (i) => i.agentId,
      registry(),
      idOf,
    );
    expect(units.map((u) => u.root?.id ?? null)).toEqual([5]);
  });
});

describe("needsYouByTask", () => {
  it("counts each agent's open require_response notices on its task", () => {
    const agents = [
      agent(30, [notice(1, { priority: "P1" }), notice(2, { priority: "P3" })]),
      agent(60, [notice(3)]),
      agent(999, [notice(4)]), // no task — not attributed anywhere
      agent(40, []), // nothing pending — no entry
    ];
    const m = needsYouByTask(registry(), agents);
    expect(m.get(3)).toEqual({ count: 2, top: "P1" });
    expect(m.get(6)).toEqual({ count: 1, top: "P2" });
    expect(m.has(4)).toBe(false);
    expect(m.size).toBe(2);
  });

  it("attributes an agent's notices to only its FIRST owned task (no double count)", () => {
    const tasks = [
      task(1),
      task(2, { parent_id: 1, owner: 10 }),
      task(3, { parent_id: 1, owner: 10 }),
    ];
    const m = needsYouByTask(tasks, [agent(10, [notice(1, { priority: "P0" })])]);
    expect(m.get(2)).toEqual({ count: 1, top: "P0" });
    expect(m.has(3)).toBe(false);
  });

  it("attributes a notice to the task it names (task_id wins over the owner-join)", () => {
    // Agent 30's owner-joined task is #3, but its notice names #4 — the notice
    // must land on #4, not #3.
    const m = needsYouByTask(registry(), [
      agent(30, [notice(1, { priority: "P1", task_id: 4 })]),
    ]);
    expect(m.get(4)).toEqual({ count: 1, top: "P1" });
    expect(m.has(3)).toBe(false);
  });

  it("splits one agent's notices across the different tasks they name", () => {
    const m = needsYouByTask(registry(), [
      agent(30, [
        notice(1, { priority: "P0", task_id: 3 }),
        notice(2, { priority: "P2", task_id: 4 }),
      ]),
    ]);
    expect(m.get(3)).toEqual({ count: 1, top: "P0" });
    expect(m.get(4)).toEqual({ count: 1, top: "P2" });
  });

  it("attributes a loose agent's notice by task_id even with no owned task", () => {
    // Agent 999 owns nothing (owner-join null), but its notice names task #6.
    const m = needsYouByTask(registry(), [agent(999, [notice(1, { task_id: 6 })])]);
    expect(m.get(6)).toEqual({ count: 1, top: "P2" });
  });

  it("falls back to the owner-join when task_id names an unknown task", () => {
    const m = needsYouByTask(registry(), [
      agent(30, [notice(1, { priority: "P1", task_id: 9999 })]),
    ]);
    expect(m.get(3)).toEqual({ count: 1, top: "P1" });
    expect(m.has(9999)).toBe(false);
  });
});

describe("rollupNeedsYou", () => {
  it("aggregates counts up the parent chain", () => {
    const own = needsYouByTask(registry(), [
      agent(30, [notice(1)]),
      agent(40, [notice(2), notice(3)]),
      agent(60, [notice(4)]),
    ]);
    const roll = rollupNeedsYou(registry(), own);
    expect(roll.get(3)).toBe(1);
    expect(roll.get(4)).toBe(2);
    expect(roll.get(2)).toBe(3); // manager-a subtree: 1 + 2
    expect(roll.get(5)).toBe(1); // manager-b subtree
    expect(roll.get(1)).toBe(4); // system root sees everything
    expect(roll.has(6)).toBe(true);
  });

  it("is empty when nothing needs the user", () => {
    expect(rollupNeedsYou(registry(), new Map()).size).toBe(0);
  });
});
