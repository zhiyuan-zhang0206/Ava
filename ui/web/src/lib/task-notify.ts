// Task-subtree join for the notify queue + task board (RCS cut 3).
//
// The supervisor pulls work through the task board; the notify queue is the
// linear fallback. Both need the same join, done purely client-side: a queue
// entry hangs off its agent, the agent hangs off the first real task it owns,
// and the task's parent chain rolls up to the top-level subtree (the child of
// the system root). Agents with no task stay loose — they are rendered flat,
// never forced into a pseudo-group.

import type { TaskRow } from "./types";

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
    // owner-join (the notice's task_id, or its owner agent's first task).
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
