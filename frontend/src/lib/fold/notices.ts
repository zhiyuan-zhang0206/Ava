// R4 layer 1 — notices-domain reducer (Task #1024).
//
// notice_posted / notice_resolved do NOT carry a full NoticeItem (no body, no
// timestamps), so the open queue cannot be folded from events — the correct
// reconciliation is invalidation (refetch the notice endpoint family). This
// reducer centralizes that policy; use-notices.ts no longer subscribes.
//
// Keys: the open-queue query + the resolved-history infinite query. A resolve
// moves a row out of open/awaiting AND into the history — both families go.

import type { SystemEvent } from "../types";
import type { FoldOutcome } from "./types";
import { NO_FOLD } from "./types";

export const NOTICES_QUERY_KEY = ["notices"] as const;
export const NOTICES_RESOLVED_QUERY_KEY = ["notices-resolved"] as const;

export function foldNotices(ev: SystemEvent): FoldOutcome {
  if (ev.role === "notice_posted") {
    return { writes: [], invalidations: [{ key: NOTICES_QUERY_KEY }] };
  }
  if (ev.role === "notice_resolved") {
    return {
      writes: [],
      invalidations: [{ key: NOTICES_QUERY_KEY }, { key: NOTICES_RESOLVED_QUERY_KEY }],
    };
  }
  return NO_FOLD;
}
