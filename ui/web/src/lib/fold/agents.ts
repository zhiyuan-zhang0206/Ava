// Lifecycle events are invalidation hints, never unversioned cache snapshots.
// Authoritative roster reads replace the whole bounded live tree, so terminated
// history cannot accumulate in a browser that stays open indefinitely.
import type { SystemEvent } from "../types";
import type { FoldOutcome } from "./types";
import { NO_FOLD } from "./types";

export const AGENTS_QUERY_KEY = ["agents", "roster"] as const;
export const AGENT_DIRECTORY_QUERY_KEY = ["agent-directory"] as const;
export const AGENT_DETAIL_QUERY_KEY = ["agent-detail"] as const;

export function foldAgents(ev: SystemEvent): FoldOutcome {
  // FYI notice changes do not publish agent_updated, but change card counts.
  switch (ev.role) {
    case "agent_spawned":
    case "agent_updated":
    case "label_updated":
    case "notice_posted":
    case "notice_resolved":
      break;
    default:
      return NO_FOLD;
  }
  return { writes: [], invalidations: [
    { key: AGENTS_QUERY_KEY },
    { key: AGENT_DIRECTORY_QUERY_KEY },
    { key: [...AGENT_DETAIL_QUERY_KEY, ev.agent_id] },
  ] };
}
