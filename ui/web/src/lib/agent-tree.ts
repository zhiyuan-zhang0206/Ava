// Agent spawn-tree builder — folds AgentRow[] into nested AgentNode[]
// (terminated nodes included).
//
// Input: flat agents (each with spawner: "user" / "agent:N" / "<other>"
// plus an optional fork_source_agent_id).
// Output: a unified AgentNode[], with sub-agents indented under their parent.
//
// Parent resolution (see resolveParent): a fork nests under its
// fork-source (the agent whose checkpoint it branched from) — fork-source
// wins over spawner, because a fork inherits the source's conversation,
// so the source is its true lineage parent. Plain spawns nest under the
// spawner ("agent:N"). The edge carries an isFork flag the row UI renders
// distinctly (fork-source nesting is what reconnects UI forks, which
// carry spawner="user" but a fork_source_agent_id).
//
// Top-level (root) = neither fork-source nor spawner resolves to an agent
// present in the table (user / claude-code / any external spawner, or a
// broken-chain "agent:N"/fork-source whose target is absent).
//
// Example: agents = [
//   {id:1, spawner:"user"},
//   {id:2, spawner:"agent:1"},                       # alive
//   {id:3, spawner:"agent:2"},                       # terminated
//   {id:4, spawner:"claude-code"},
//   {id:5, spawner:"user", fork_source_agent_id:1},  # fork of 1
// ]
// Tree:
//   1
//     2
//       3   (terminated, still in tree; the row UI expresses it via dot
//            color + resurrect button)
//     5   (fork edge — branched from 1's state)
//   4
//
// No more grouping by spawner string (USER / CLAUDE-CODE etc. section
// headers) — UI prioritizes simplicity, and "who triggered it" remains
// in the spawner text itself when needed. Terminated agents **stay in
// the tree** — preserving spawn-lineage structure (a sub-agent's
// suicide doesn't remove it as the parent's child). Status is expressed
// per-row via dot color + buttons, not by filtering it out of the tree
// structure.
//
// When `options.hideTerminated` is true, terminated nodes are omitted from
// the tree and their children are re-parented to the nearest visible
// ancestor (walking up the spawn chain recursively). This keeps the tree
// connected when the user toggles "show terminated" off — a chain like
// 1(alive) → 2(terminated) → 3(alive) becomes 1 → 3 instead of surfacing
// 3 as a detached root.
//
// Sorting: within a level, strictly by id asc. Order is decoupled from
// status, so a row never jumps when its status changes (idling -> running
// -> idling, terminate, etc.). id is immutable, so a row holds its
// position for its whole lifetime — the only thing that moves a row is a
// new sibling being inserted. (last_active_at would be even more volatile
// than status — SSE updates would make same-status siblings swap places
// and flicker.)

import type { AgentRow } from "./types";

export interface AgentNode {
  readonly agent: AgentRow;
  readonly children: readonly AgentNode[];
  /** Whether this is the last child under its parent — used to render tree lines. */
  readonly isLast: boolean;
  /** Whether this agent was created as a fork (its state branched from a
   *  source checkpoint) rather than a plain spawn. Forks nest under their
   *  fork-source and the row badges them. True even when the source was
   *  pruned and the node falls back to a spawner-parent or to root — a fork
   *  stays recognizable. A normal root is false. */
  readonly isFork: boolean;
}

/** Parse the spawner string to extract parent agent id (when in "agent:N" form). */
function parentIdOf(spawner: string): number | null {
  if (!spawner.startsWith("agent:")) return null;
  const n = Number(spawner.slice("agent:".length));
  return Number.isFinite(n) ? n : null;
}

/** Resolve an agent's tree parent + whether that edge is a fork.
 *
 * Fork-source wins over spawner: a fork inherits its source agent's
 * conversation/checkpoint, so the natural lineage parent is the
 * fork-source (whose state it branched from), not whoever triggered the
 * spawn. This also reconnects UI-initiated forks — they carry
 * spawner="user" but fork_source_agent_id pointing at the source, and
 * would otherwise surface as detached roots.
 *
 * Falls back to the spawner ("agent:N") link for plain spawns. Returns
 * parentId=null (root) when neither link resolves to an agent present in
 * the table (broken chain — e.g. source terminated and purged).
 *
 * `isFork` reflects "this agent was created as a fork" (fork_source set),
 * independent of whether the source is still present. So a fork whose
 * source was pruned is kept in the tree (under its spawner, else as a
 * root) and STILL badged — rather than silently demoted to a plain node.
 */
function resolveParent(
  a: AgentRow,
  byId: Map<number, AgentRow>,
): { parentId: number | null; isFork: boolean } {
  const forkSource = a.fork_source_agent_id;
  const isFork = forkSource != null;
  if (forkSource != null && byId.has(forkSource)) {
    return { parentId: forkSource, isFork: true };
  }
  // Not a fork, or the fork-source was pruned — fall back to the spawner
  // link. Keep isFork so a pruned-source fork is still recognizable.
  const spawnerParent = parentIdOf(a.spawner);
  if (spawnerParent != null && byId.has(spawnerParent)) {
    return { parentId: spawnerParent, isFork };
  }
  return { parentId: null, isFork };
}

export type TreeSortKey = "id" | "last_active" | "status";
export type TreeSortDir = "asc" | "desc";
export interface TreeSort {
  key: TreeSortKey;
  dir: TreeSortDir;
}

function siblingOrder(a: AgentRow, b: AgentRow, sort: TreeSort): number {
  const sign = sort.dir === "asc" ? 1 : -1;
  switch (sort.key) {
    case "id":
      return sign * (a.agent_id - b.agent_id);
    case "last_active": {
      const ta = a.last_active_at ? new Date(a.last_active_at).getTime() : 0;
      const tb = b.last_active_at ? new Date(b.last_active_at).getTime() : 0;
      return sign * (ta - tb);
    }
    case "status":
      return sign * a.status.localeCompare(b.status);
  }
}

export interface BuildAgentTreeOptions {
  /** When true, terminated agents are omitted and their children are
   *  re-parented to the nearest visible ancestor (walking up the spawn
   *  chain recursively). Default false — terminated agents stay in the
   *  tree at their normal lineage position. */
  hideTerminated?: boolean;
}

/** Fold agents into a nested tree. Top level is all roots; within each
 *  root, nest by spawn lineage to any depth. */
export function buildAgentTree(
  agents: readonly AgentRow[],
  sort?: TreeSort,
  options?: BuildAgentTreeOptions,
): AgentNode[] {
  const s: TreeSort = sort ?? { key: "id", dir: "asc" };
  const hideTerminated = options?.hideTerminated === true;
  const byId = new Map(agents.map((a) => [a.agent_id, a]));

  // Single pass: resolve every agent's parent + fork flag, then group
  // by parent id. O(n) instead of the old O(n²) per-level filter.
  const kidsByParent = new Map<number | null, { agent: AgentRow; isFork: boolean }[]>();
  for (const a of agents) {
    const { parentId, isFork } = resolveParent(a, byId);
    const bucket = kidsByParent.get(parentId);
    if (bucket) bucket.push({ agent: a, isFork });
    else kidsByParent.set(parentId, [{ agent: a, isFork }]);
  }

  // Recursively build subtrees from the pre-grouped map.
  function buildSubtree(parentId: number | null): AgentNode[] {
    const bucket = kidsByParent.get(parentId);
    if (!bucket) return [];
    bucket.sort((x, y) => siblingOrder(x.agent, y.agent, s));

    if (!hideTerminated) {
      return bucket.map(({ agent, isFork }, i) => ({
        agent,
        children: buildSubtree(agent.agent_id),
        isLast: i === bucket.length - 1,
        isFork,
      }));
    }

    // When hiding terminated: recursively flatten terminated nodes by
    // re-parenting their children to the current level. A terminated node
    // whose children are also terminated triggers another iteration, so a
    // chain like 1(alive) → 2(term) → 3(term) → 4(alive) collapses to
    // 1 → 4 in one pass.
    let current = bucket;
    for (;;) {
      const next: { agent: AgentRow; isFork: boolean }[] = [];
      let hadTerminated = false;
      for (const item of current) {
        if (item.agent.status === "terminated") {
          hadTerminated = true;
          const grandkids = kidsByParent.get(item.agent.agent_id);
          if (grandkids) next.push(...grandkids);
          // If no grandkids, the terminated leaf is simply excluded from next.
        } else {
          next.push(item);
        }
      }
      if (!hadTerminated) break;
      current = next;
    }

    current.sort((x, y) => siblingOrder(x.agent, y.agent, s));

    return current.map(({ agent, isFork }, i) => ({
      agent,
      children: buildSubtree(agent.agent_id),
      isLast: i === current.length - 1,
      isFork,
    }));
  }

  return buildSubtree(null);
}
