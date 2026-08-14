// R4 layer 1 — the fold's single entry point (Task #1024, design §5.2).
//
// applyEvent is the ONLY place the global stream's events reconcile into the
// query cache. The owner (EventStreamProvider) feeds every system event here
// and applies the returned outcome; hooks only read their keys.
//
// Domain dispatch:
// - agents  (["agents"])            — fold (spawn/update upsert, label patch)
// - pages   (["agent-pages", id] + ["all-pages"]) — fold (opened/closed)
// - notices (["notices"], ["notices-resolved"])   — invalidate (no full row)
// - fleet-graph (["fleet-graph"])   — invalidate, debounced 2s (owner)
// - tasks   (["tasks"])             — invalidate, debounced 2s (owner)
//
// Reconnect: the owner invalidates every query once on (re)open — the
// disconnect window's missed events are repaired wholesale (this was
// useAgentsCacheSync's central reconcile; it moves into the fold owner).

import { AGENTS_QUERY_KEY } from "./agents";
import type { AgentRow, PageRow, SystemEvent } from "../types";
import { foldAgents } from "./agents";
import { foldFleetGraph } from "./graph";
import { foldNotices } from "./notices";
import { foldPages } from "./pages";
import { foldTasks } from "./tasks";
import type { FoldInvalidation, FoldOutcome, FoldWrite } from "./types";
import { NO_FOLD } from "./types";

export const ALL_PAGES_QUERY_KEY = ["all-pages"] as const;
export { AGENTS_QUERY_KEY } from "./agents";
export { NOTICES_QUERY_KEY, NOTICES_RESOLVED_QUERY_KEY } from "./notices";
export { TASKS_QUERY_KEY } from "./tasks";
export { FLEET_GRAPH_KEY_PREFIX } from "./graph";

export interface FoldContext {
  /** Current cache value for a key (undefined = never fetched — never seed). */
  getQueryData: (key: readonly unknown[]) => unknown;
  /** Apply a cache write (a folded value). */
  setQueryData: (key: readonly unknown[], value: unknown) => void;
  /** Invalidate a query family (debounce policy lives in the owner). */
  invalidateQueries: (key: readonly unknown[]) => void;
}

/** Fold one event from the global broadcast into the query cache.
 *  Pure per-domain reducers produce the outcome; only the application through
 *  `ctx` is side-effecting. Returns the outcome (for tests / owners that want
 *  to observe). */
export function applyEvent(ctx: FoldContext, ev: SystemEvent): FoldOutcome {
  const outcome = foldAgainstCache(ctx, ev);
  for (const write of outcome.writes) {
    ctx.setQueryData(write.key, write.value);
  }
  for (const invalidation of outcome.invalidations) {
    ctx.invalidateQueries(invalidation.key);
  }
  return outcome;
}

/** Fold an event against the live cache — the pure dispatch, exported for
 *  tests. Reducers run only for domains whose events can touch them. */
export function foldAgainstCache(
  ctx: FoldContext,
  ev: SystemEvent,
): FoldOutcome {
  const writes: FoldWrite[] = [];
  const invalidations: FoldInvalidation[] = [];

  // agents — fold (upsert / label patch), guarded against un-seeded caches.
  const agentsPrev = ctx.getQueryData(AGENTS_QUERY_KEY) as AgentRow[] | undefined;
  const agentsNext = foldAgents(agentsPrev, ev);
  if (agentsNext !== undefined) writes.push({ key: AGENTS_QUERY_KEY, value: agentsNext });

  // pages — the event's per-agent cache (only if it exists — the guard inside
  // foldPages keeps an un-fetched key un-seeded) + the fleet-wide one.
  if (ev.role === "page_opened" || ev.role === "page_closed") {
    const agentKey = ["agent-pages", ev.agent_id] as const;
    const agentPrev = ctx.getQueryData(agentKey) as PageRow[] | undefined;
    const agentNext = foldPages(agentPrev, ev, ev.agent_id);
    if (agentNext !== undefined) writes.push({ key: agentKey, value: agentNext });

    const allPrev = ctx.getQueryData(ALL_PAGES_QUERY_KEY) as PageRow[] | undefined;
    const allNext = foldPages(allPrev, ev, null);
    if (allNext !== undefined) writes.push({ key: ALL_PAGES_QUERY_KEY, value: allNext });
  }

  // notices / fleet-graph / tasks — invalidation policies (the owner applies
  // its debounce for the graph/tasks families).
  invalidations.push(...foldNotices(ev).invalidations);
  invalidations.push(...foldFleetGraph(ev).invalidations);
  invalidations.push(...foldTasks(ev).invalidations);

  if (writes.length === 0 && invalidations.length === 0) return NO_FOLD;
  return { writes, invalidations };
}

export type { FoldOutcome };
export { NO_FOLD };
