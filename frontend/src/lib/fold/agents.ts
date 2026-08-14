// R4 layer 1 — agents-domain reducer (Task #1024).
//
// Pure fold of the global-stream lifecycle events into the ["agents"] cache.
// Extracted from use-agents.ts's applyAgentEvent/upsertAgent — the merge rule
// was already centralized; it now lives in the fold and hooks only read.

import type { AgentRow, SystemEvent } from "../types";
import { projectAgentStatus } from "../types";
import { guardSeeded, upsertByKey } from "./types";

export const AGENTS_QUERY_KEY = ["agents"] as const;

/** Upsert a snapshot into the agents cache by agent_id. Exported so the
 *  read-only fleet view can share the same merge rule (fleet normalizes
 *  independently; the upsert semantics stay in one place). */
export function upsertAgent(prev: AgentRow[] | undefined, next: AgentRow): AgentRow[] | undefined {
  return upsertByKey(
    prev,
    next,
    (a) => a.agent_id,
    (cur, nxt) =>
      cur === nxt ||
      (cur.status === nxt.status &&
        cur.label === nxt.label &&
        cur.pid === nxt.pid &&
        cur.unread_notice_count === nxt.unread_notice_count &&
        cur.notices_awaiting_response.length === nxt.notices_awaiting_response.length),
  );
}

/** Fold one lifecycle event into the agents cache by id.
 *
 *  - agent_spawned / agent_updated → upsert the projected snapshot (hibernating
 *    → idling projection happens here, mirroring the listAgents fetch).
 *  - label_updated → patch just the label on the existing row (instant rename,
 *    no fetch).
 *  The empty-cache guard refuses to seed a single-agent partial before the
 *  initial listAgents fetch lands. Returns undefined = no write. */
export function foldAgents(
  prev: AgentRow[] | undefined,
  ev: SystemEvent,
): AgentRow[] | undefined {
  if (ev.role === "agent_spawned" || ev.role === "agent_updated") {
    const snapshot = projectAgentStatus(ev.snapshot);
    const seeded = guardSeeded(prev);
    if (seeded === undefined) return undefined;
    return upsertAgent(seeded, snapshot);
  }
  if (ev.role === "label_updated") {
    const seeded = guardSeeded(prev);
    if (seeded === undefined) return undefined;
    const idx = seeded.findIndex((a) => a.agent_id === ev.agent_id);
    if (idx === -1) return seeded;
    const out = seeded.slice();
    out[idx] = { ...out[idx], label: ev.label };
    return out;
  }
  return undefined;
}
