// Build a visible tree from cards and minimal ancestor links. Provenance stays
// unchanged; hiding a historical ancestor changes presentation only.
import type { AgentLineage, AgentRow } from "./types";

export interface AgentNode {
  readonly agent: AgentRow;
  readonly children: readonly AgentNode[];
  readonly isLast: boolean;
  readonly isFork: boolean;
}
export type TreeSortKey = "id" | "last_active" | "status";
export type TreeSortDir = "asc" | "desc";
export interface TreeSort { key: TreeSortKey; dir: TreeSortDir }
export interface BuildAgentTreeOptions {
  hideTerminated?: boolean;
  ancestors?: readonly AgentLineage[];
}

function parentOf(agent: AgentLineage, links: Map<number, AgentLineage>): number | null {
  if (agent.fork_source_agent_id != null && links.has(agent.fork_source_agent_id)) return agent.fork_source_agent_id;
  const match = /^agent:(\d+)$/.exec(agent.spawner);
  const spawner = match == null ? null : Number(match[1]);
  return spawner != null && links.has(spawner) ? spawner : null;
}

function compare(a: AgentRow, b: AgentRow, sort: TreeSort): number {
  const sign = sort.dir === "asc" ? 1 : -1;
  switch (sort.key) {
    case "id": return sign * (a.agent_id - b.agent_id);
    case "last_active": return sign * (Date.parse(a.last_active_at) - Date.parse(b.last_active_at));
    case "status": return sign * a.status.localeCompare(b.status);
  }
}

/** Fold current cards plus their ancestor closure in O(nodes + links), before sorting. */
export function buildAgentTree(agents: readonly AgentRow[], sort: TreeSort = { key: "id", dir: "asc" }, options: BuildAgentTreeOptions = {}): AgentNode[] {
  const links = new Map<number, AgentLineage>(options.ancestors?.map((a) => [a.agent_id, a]));
  for (const agent of agents) links.set(agent.agent_id, agent);
  const parents = new Map([...links].map(([id, agent]) => [id, parentOf(agent, links)]));
  // Validate once, iteratively: a corrupt chain must not hide live nodes or
  // recurse forever. Completed walks are memoized, including shared ancestry.
  const done = new Set<number>();
  for (const id of links.keys()) {
    const path = new Set<number>();
    let cursor: number | null = id;
    while (cursor != null && !done.has(cursor)) {
      if (path.has(cursor)) throw new Error(`Cyclic agent lineage at #${cursor}`);
      path.add(cursor);
      cursor = parents.get(cursor) ?? null;
    }
    for (const visited of path) done.add(visited);
  }
  const visible = agents.filter((a) => !options.hideTerminated || a.status !== "terminated");
  const visibleIds = new Set(visible.map((a) => a.agent_id));
  const nearest = new Map<number, number | null>();
  const buckets = new Map<number | null, AgentRow[]>();
  for (const agent of visible) {
    let parent = parents.get(agent.agent_id) ?? null;
    const traversed: number[] = [];
    while (parent != null && !visibleIds.has(parent)) {
      const cached = nearest.get(parent);
      if (cached !== undefined) { parent = cached; break; }
      traversed.push(parent);
      parent = parents.get(parent) ?? null;
    }
    for (const id of traversed) nearest.set(id, parent);
    const bucket = buckets.get(parent) ?? [];
    bucket.push(agent);
    buckets.set(parent, bucket);
  }
  const build = (parent: number | null): AgentNode[] => {
    const children = buckets.get(parent) ?? [];
    children.sort((a, b) => compare(a, b, sort));
    return children.map((agent, i) => ({ agent, children: build(agent.agent_id), isLast: i === children.length - 1, isFork: agent.fork_source_agent_id != null }));
  };
  return build(null);
}
