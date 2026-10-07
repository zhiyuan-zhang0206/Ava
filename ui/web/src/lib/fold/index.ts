// R4 layer 1 — the fold's single entry point (Task #1024, design §5.2).
//
// applyEvent is the ONLY place the global stream's events reconcile into the
// query cache. The owner (EventStreamProvider) feeds every system event here
// and applies the returned outcome; hooks only read their keys.
//
// Lifecycle hints schedule authoritative roster/directory/detail reads.
// Pages, notices, fleet graph and tasks invalidate from event hints.
// The owner coalesces reads and guarantees trailing repair across event races.

import { AGENTS_QUERY_KEY, AGENT_DIRECTORY_QUERY_KEY, AGENT_DETAIL_QUERY_KEY, foldAgents } from "./agents";
import type { SystemEvent } from "../contracts/types";
import { FLEET_GRAPH_KEY_PREFIX, foldFleetGraph } from "./graph";
import {
  foldNotices,
  NOTICES_QUERY_KEY,
  NOTICES_RESOLVED_QUERY_KEY,
} from "./notices";
import { foldTasks, TASKS_QUERY_KEY } from "./tasks";
import type { FoldInvalidation, FoldOutcome, FoldWrite } from "./types";
import { NO_FOLD } from "./types";

export const ALL_PAGES_QUERY_KEY = ["all-pages"] as const;
export const AGENT_PAGES_QUERY_KEY_PREFIX = ["agent-pages"] as const;

/** Cache families whose snapshots are repaired by the global system stream.
 * Reconnect invalidates only these owners; unrelated server data such as
 * settings, config, and inspector aggregates did not miss global-fold events. */
export const RECONNECT_QUERY_KEYS: readonly (readonly unknown[])[] = [
  AGENTS_QUERY_KEY,
  AGENT_DIRECTORY_QUERY_KEY,
  AGENT_DETAIL_QUERY_KEY,
  AGENT_PAGES_QUERY_KEY_PREFIX,
  ALL_PAGES_QUERY_KEY,
  NOTICES_QUERY_KEY,
  NOTICES_RESOLVED_QUERY_KEY,
  FLEET_GRAPH_KEY_PREFIX,
  TASKS_QUERY_KEY,
];
export { AGENTS_QUERY_KEY, AGENT_DIRECTORY_QUERY_KEY, AGENT_DETAIL_QUERY_KEY } from "./agents";
export { NOTICES_QUERY_KEY, NOTICES_RESOLVED_QUERY_KEY } from "./notices";
export { TASKS_QUERY_KEY } from "./tasks";
export { FLEET_GRAPH_KEY_PREFIX } from "./graph";

export interface FoldContext {
  /** Current cache value for a key (undefined = never fetched — never seed). */
  getQueryData: (key: readonly unknown[]) => unknown;
  /** Apply a cache write (a folded value). */
  setQueryData: (key: readonly unknown[], value: unknown) => void;
  /** Invalidate a query family (coalescing and trailing repair live in the owner). */
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
  _ctx: FoldContext,
  ev: SystemEvent,
): FoldOutcome {
  const writes: FoldWrite[] = [];
  const invalidations: FoldInvalidation[] = [];

  invalidations.push(...foldAgents(ev).invalidations);

  // Page events are hints for both authoritative lists. The repair scheduler
  // skips absent queries, coalesces bursts, and follows an in-flight GET with
  // another read so its older snapshot cannot strand the cache.
  if (ev.role === "page_opened" || ev.role === "page_closed") {
    invalidations.push({ key: ["agent-pages", ev.agent_id] });
    invalidations.push({ key: ALL_PAGES_QUERY_KEY });
  }

  // Notices / fleet graph / tasks share the owner's bounded coalescing policy.
  invalidations.push(...foldNotices(ev).invalidations);
  invalidations.push(...foldFleetGraph(ev).invalidations);
  invalidations.push(...foldTasks(ev).invalidations);

  if (writes.length === 0 && invalidations.length === 0) return NO_FOLD;
  return { writes, invalidations };
}

export type { FoldOutcome };
export { NO_FOLD };
