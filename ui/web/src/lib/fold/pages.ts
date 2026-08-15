// R4 layer 1 — pages-domain reducer (Task #1024).
//
// Pure fold of page_opened / page_closed into the pages caches. Two cache
// shapes share the same row semantics:
// - ["agent-pages", agentId] — one agent's pages (rows matched by name).
// - ["all-pages"] — every agent's pages (rows matched by agent_id + name).
// Extracted from use-agent-pages.ts / use-all-pages.ts — the merge rule was
// duplicated across both hooks; it now lives in the fold and hooks only read.
//
// The empty-cache guard (guardSeeded) refuses to seed a partial cache from a
// single SSE event before the initial fetch lands; a fetched-empty list ([])
// is a real state and merges in.

import type { PageRow, SystemEvent } from "../types";
import { guardSeeded, removeByKey } from "./types";

/** Row key for a pages cache: (agent_id, name) fleet-wide, name alone per-agent. */
function rowKey(scopeAgentId: number | null, row: PageRow): string {
  return scopeAgentId === null ? `${row.agent_id}:${row.name}` : row.name;
}

/** Fold page_opened / page_closed into a pages cache.
 *  @param scopeAgentId the agent whose per-agent cache this is; null = the
 *         fleet-wide ["all-pages"] cache.
 *  Returns undefined = no write (event for another agent, or empty-cache
 *  guard). A re-open keeps the original created_at (the row's open time is
 *  its first open — the port/url may change on re-open). */
export function foldPages(
  prev: PageRow[] | undefined,
  ev: SystemEvent,
  scopeAgentId: number | null,
): PageRow[] | undefined {
  if (ev.role === "page_opened") {
    if (scopeAgentId !== null && ev.agent_id !== scopeAgentId) return undefined;
    const seeded = guardSeeded(prev);
    if (seeded === undefined) return undefined;
    const key = rowKey(scopeAgentId, {
      agent_id: ev.agent_id,
      name: ev.name,
    } as PageRow);
    const idx = seeded.findIndex((row) => rowKey(scopeAgentId, row) === key);
    const next: PageRow = {
      id: ev.page_id,
      agent_id: ev.agent_id,
      name: ev.name,
      port: ev.port,
      title: ev.title,
      serve_dir: null,
      url: ev.url,
      created_at: idx >= 0 ? seeded[idx].created_at : new Date().toISOString(),
      closed_at: null,
    };
    if (idx >= 0) {
      const out = seeded.slice();
      out[idx] = next;
      return out;
    }
    return [...seeded, next];
  }
  if (ev.role === "page_closed") {
    if (scopeAgentId !== null && ev.agent_id !== scopeAgentId) return undefined;
    return removeByKey(
      prev,
      scopeAgentId === null ? `${ev.agent_id}:${ev.name}` : ev.name,
      (row) => rowKey(scopeAgentId, row),
    );
  }
  return undefined;
}
