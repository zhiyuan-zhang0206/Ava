// Task-subtree join for the notify queue + task board (RCS cut 3).
//
// The supervisor pulls work through the task board; the notify queue is the
// linear fallback. Both need the same join, done purely client-side: a queue
// entry hangs off its agent, the agent hangs off the first real task it owns,
// and the task's parent chain rolls up to the top-level subtree (the child of
// the system root). Agents with no task stay loose — they are rendered flat,
// never forced into a pseudo-group.

import { PRIORITY_RANK } from "./notices";
import type { AgentRow, OpenNotice, TaskRow } from "./types";

/** First real task (parent_id !== null — never the system root) owned by the
 *  agent, or null. Mirrors the board's bidirectional-sync convention
 *  (`tasks.find((t) => t.owner === agentId && t.parent_id !== null)`) so the
 *  queue and the board agree on which task an agent "is". */
export function taskForAgent(
  tasks: readonly TaskRow[],
  agentId: number,
): TaskRow | null {
  return tasks.find((t) => t.owner === agentId && t.parent_id !== null) ?? null;
}

/** Walk the parent chain up to the top-level subtree root: the ancestor whose
 *  parent is the system root (parent_id === null) or is missing from the
 *  registry. Cycle-guarded — a corrupt chain returns the last task reached. */
export function subtreeRootOf(
  byId: ReadonlyMap<number, TaskRow>,
  task: TaskRow,
): TaskRow {
  let cur = task;
  const seen = new Set<number>([cur.id]);
  while (cur.parent_id !== null) {
    const parent = byId.get(cur.parent_id);
    // Stop under the system root (parent_id null), at a missing parent, or on
    // a cycle.
    if (parent?.parent_id == null || seen.has(parent.id)) break;
    seen.add(parent.id);
    cur = parent;
  }
  return cur;
}

/** One render unit of the grouped queue: a task-subtree group (root non-null,
 *  all its entries pulled together) or a single loose entry (root null). */
export interface QueueUnit<T> {
  readonly root: TaskRow | null;
  readonly items: T[];
}

/** Fold a priority-sorted entry list into contiguous render units. Each entry
 *  hangs off a task — the one it names via `taskIdOf` (a notice's task_id) when
 *  that points at a real task, else the first real task its agent owns — and
 *  entries sharing a top-level subtree collapse into one unit, positioned where
 *  the subtree's best-ranked entry sat (so a P0 inside a group still surfaces
 *  the group first); relative order inside a group is the incoming order.
 *  Entries with no task (null/unknown id and no owned task) stay flat at their
 *  own rank. `taskIdOf` is optional — omit it for pure owner-join grouping. */
export function groupByTaskSubtree<T>(
  items: readonly T[],
  agentIdOf: (item: T) => number,
  tasks: readonly TaskRow[],
  taskIdOf?: (item: T) => number | null,
): QueueUnit<T>[] {
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const groups = new Map<number, QueueUnit<T>>();
  const units: QueueUnit<T>[] = [];
  for (const item of items) {
    // The named task wins when it exists; a null / unknown id falls back to the
    // owner-join (mirrors needsYouByTask's per-notice attribution).
    const named = taskIdOf?.(item) ?? null;
    const namedTask = named !== null ? byId.get(named) : undefined;
    const task = namedTask ?? taskForAgent(tasks, agentIdOf(item));
    if (task == null) {
      units.push({ root: null, items: [item] });
      continue;
    }
    const root = subtreeRootOf(byId, task);
    const existing = groups.get(root.id);
    if (existing) {
      existing.items.push(item);
    } else {
      const unit: QueueUnit<T> = { root, items: [item] };
      groups.set(root.id, unit);
      units.push(unit);
    }
  }
  return units;
}

/** Pending needs-you load on one task: how many open require_response notices
 *  its owner agents hold, and the highest priority among them. */
export interface TaskNeedsYou {
  count: number;
  top: OpenNotice["priority"];
}

/** Per-task needs-you: each open require_response notice attaches to the task it
 *  names (`notice.task_id`, the authoritative link), or — when it names none, or
 *  names one absent from the registry — falls back to the first real task its
 *  owner agent holds (`taskForAgent`). Attributed per notice, so an agent whose
 *  notices point at different tasks splits across them. Tasks with zero pending
 *  notices have no entry. */
export function needsYouByTask(
  tasks: readonly TaskRow[],
  agents: readonly AgentRow[],
): Map<number, TaskNeedsYou> {
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const result = new Map<number, TaskNeedsYou>();
  const bump = (taskId: number, priority: OpenNotice["priority"]) => {
    const cur = result.get(taskId);
    if (!cur) {
      result.set(taskId, { count: 1, top: priority });
    } else {
      cur.count += 1;
      if (PRIORITY_RANK[priority] < PRIORITY_RANK[cur.top]) cur.top = priority;
    }
  };
  for (const agent of agents) {
    // Fallback target for a notice that names no task: the first real task the
    // agent owns (the pre-task_id behavior).
    const ownerTaskId = taskForAgent(tasks, agent.agent_id)?.id ?? null;
    for (const notice of agent.notices_awaiting_response) {
      const taskId =
        notice.task_id != null && byId.has(notice.task_id) ? notice.task_id : ownerTaskId;
      if (taskId === null) continue;
      bump(taskId, notice.priority);
    }
  }
  return result;
}

/** Subtree rollup: task id → total needs-you count in its subtree (itself +
 *  every descendant). Drives the board's "Needs you" filter, which keeps a
 *  manager task visible while any of its descendants waits on the user. */
export function rollupNeedsYou(
  tasks: readonly TaskRow[],
  own: ReadonlyMap<number, TaskNeedsYou>,
): Map<number, number> {
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const result = new Map<number, number>();
  for (const [taskId, ny] of own) {
    let cur = byId.get(taskId);
    const seen = new Set<number>();
    while (cur && !seen.has(cur.id)) {
      seen.add(cur.id);
      result.set(cur.id, (result.get(cur.id) ?? 0) + ny.count);
      cur = cur.parent_id !== null ? byId.get(cur.parent_id) : undefined;
    }
  }
  return result;
}
