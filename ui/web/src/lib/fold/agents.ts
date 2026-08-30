// R4 layer 1 — agents-domain reducer (Task #1024).
//
// Pure fold of the global-stream lifecycle events into the scoped agent caches.
// Extracted from use-agents.ts's applyAgentEvent/upsertAgent — the merge rule
// was already centralized; it now lives in the fold and hooks only read.

import type { AgentRow, SystemEvent } from "../types";
import { projectAgentStatus } from "../types";
import { guardSeeded, removeByKey, upsertByKey } from "./types";

export const AGENTS_QUERY_KEY = ["agents", "live"] as const;
export const TERMINATED_AGENTS_QUERY_KEY = ["agents", "terminated"] as const;
export const AGENTS_SNAPSHOT_BUFFER_KEY = ["agent-snapshot-events", "live"] as const;
export const TERMINATED_AGENTS_SNAPSHOT_BUFFER_KEY = [
  "agent-snapshot-events",
  "terminated",
] as const;

export type AgentRosterScope = "live" | "terminated";

export interface AgentSnapshotEventBuffer {
  generations: Record<number, SystemEvent[]>;
}

let nextSnapshotGeneration = 0;

export function allocateAgentSnapshotGeneration(): number {
  nextSnapshotGeneration += 1;
  return nextSnapshotGeneration;
}

export function startAgentSnapshotGeneration(
  prev: AgentSnapshotEventBuffer | undefined,
  generation: number,
): AgentSnapshotEventBuffer {
  return {
    generations: { ...prev?.generations, [generation]: [] },
  };
}

export function appendAgentSnapshotEvent(
  prev: AgentSnapshotEventBuffer | undefined,
  ev: SystemEvent,
): AgentSnapshotEventBuffer | undefined {
  if (prev === undefined || !isAgentRosterEvent(ev)) return undefined;
  const generations = Object.fromEntries(
    Object.entries(prev.generations).map(([generation, events]) => [
      generation,
      [...events, ev],
    ]),
  );
  return { generations };
}

export function finishAgentSnapshotGeneration(
  prev: AgentSnapshotEventBuffer | undefined,
  generation: number,
): { events: SystemEvent[]; remaining: AgentSnapshotEventBuffer | undefined } {
  const events = prev?.generations[generation] ?? [];
  if (prev === undefined) return { events, remaining: undefined };
  const generations = Object.fromEntries(
    Object.entries(prev.generations).filter(
      ([candidate]) => Number(candidate) !== generation,
    ),
  );
  return {
    events,
    remaining:
      Object.keys(generations).length === 0 ? undefined : { generations },
  };
}

export function isAgentRosterEvent(ev: SystemEvent): boolean {
  return (
    ev.role === "agent_spawned" ||
    ev.role === "agent_updated" ||
    ev.role === "label_updated"
  );
}

export function replayAgentSnapshotEvents(
  snapshot: AgentRow[],
  events: readonly SystemEvent[],
  scope: AgentRosterScope,
): AgentRow[] {
  return events.reduce(
    (rows, event) => foldAgents(rows, event, scope) ?? rows,
    snapshot,
  );
}

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
        cur.liveness_state === nxt.liveness_state &&
        cur.last_probe_at === nxt.last_probe_at &&
        cur.label === nxt.label &&
        cur.pid === nxt.pid &&
        cur.supports_vision === nxt.supports_vision &&
        cur.unread_notice_count === nxt.unread_notice_count &&
        cur.notices_awaiting_response.length === nxt.notices_awaiting_response.length),
  );
}

/** Fold one lifecycle event into the agents cache by id.
 *
 *  - agent_spawned / agent_updated → upsert the projected snapshot
 *    (restarting → idling projection happens here, mirroring the listAgents
 *    fetch).
 *  - label_updated → patch just the label on the existing row (instant rename,
 *    no fetch).
 *  The empty-cache guard refuses to seed a single-agent partial before the
 *  initial listAgents fetch lands. Returns undefined = no write. */
export function foldAgents(
  prev: AgentRow[] | undefined,
  ev: SystemEvent,
  scope: AgentRosterScope = "live",
): AgentRow[] | undefined {
  if (ev.role === "agent_spawned" || ev.role === "agent_updated") {
    const snapshot = projectAgentStatus(ev.snapshot);
    const seeded = guardSeeded(prev);
    if (seeded === undefined) return undefined;
    const belongs =
      scope === "terminated"
        ? snapshot.status === "terminated"
        : snapshot.status !== "terminated";
    if (!belongs) {
      return removeByKey(seeded, snapshot.agent_id, (agent) => agent.agent_id);
    }
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
