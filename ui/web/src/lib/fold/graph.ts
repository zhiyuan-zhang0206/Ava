// R4 layer 1 — fleet-graph domain (Task #1024).
//
// The fleet graph is a server-computed weighted view (lineage + message
// edges) — an agent_spawned/updated event cannot fold into it client-side.
// The reconciliation is: debounced invalidation (the poll below it keeps
// self-healing). Debounce state belongs to the fold owner, not the hook.

import type { SystemEvent } from "../types";
import type { FoldOutcome } from "./types";
import { NO_FOLD } from "./types";

export const FLEET_GRAPH_KEY_PREFIX = ["fleet-graph"] as const;

export function foldFleetGraph(ev: SystemEvent): FoldOutcome {
  if (ev.role === "agent_spawned" || ev.role === "agent_updated") {
    return { writes: [], invalidations: [{ key: FLEET_GRAPH_KEY_PREFIX }] };
  }
  return NO_FOLD;
}
