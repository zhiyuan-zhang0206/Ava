// R4 layer 1 — tasks domain (Task #1024).
//
// task_created / task_updated invalidate the tasks query (the SDK write path
// publishes them; the 30s poll beneath stays as the reconciliation fallback
// for non-SDK writes). Debounce owned by the fold owner.

import type { SystemEvent } from "../types";
import type { FoldOutcome } from "./types";
import { NO_FOLD } from "./types";

export const TASKS_QUERY_KEY = ["tasks"] as const;

export function foldTasks(ev: SystemEvent): FoldOutcome {
  if (ev.role === "task_created" || ev.role === "task_updated") {
    return { writes: [], invalidations: [{ key: TASKS_QUERY_KEY }] };
  }
  return NO_FOLD;
}
