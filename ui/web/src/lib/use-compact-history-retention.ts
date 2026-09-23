"use client";

// Compact-history retention (task #3698; user ruling 2026-09-17): a compact
// wholesale-replaces the active timeline, and the new summary must not clear
// the view — older history pages re-attach automatically above it through
// the same cross-segment fetch as scroll-up. How many pages stay visible is
// controlled by `display.compact_history_sessions` (default 1, -1 restores all
// available history). The fetch may need the post-compact GET to settle
// first (`hasMoreOlder` only turns true with it), so a pending request
// retries as the window state changes; misses are budgeted, then dropped.

import { useCallback, useEffect, useLayoutEffect, useRef } from "react";

import { useTimelineStore } from "./timeline-store";
import { useUserSettings } from "./use-user-settings";

/** Retention gives up after this many consecutive attempts that load
 * nothing: a miss means the cursor is absent, a fetch failed, or the window
 * is not loadable yet — and every window change (live traffic bumps the item
 * count) re-invokes the retry path. 5 gives a transient failure room to
 * retry, then abandons a permanent absence instead of re-attempting on every
 * streamed item. */
const RETENTION_MAX_ATTEMPTS = 5;

/** A successful terminal page is distinct from an unavailable window: the
 * post-compact timeline GET may not have supplied has_more yet. */
export type OlderSegmentLoadResult = "loaded" | "exhausted" | "unready" | "aborted" | "failed";

export function useCompactHistoryRetention(options: {
  /** Open thread; null when no agent is selected. */
  agentId: number | null;
  /** Hidden views suspend history reads; visibility resumes the same intent. */
  isVisible: boolean;
  /** Scroll-up fetch; reports whether a completed page has more history. */
  loadOlderSegment: () => Promise<OlderSegmentLoadResult>;
  hasMoreOlder: boolean;
  loadingOlder: boolean;
  /** Current item count — window growth re-invokes a blocked attempt. */
  itemCount: number;
}): void {
  const { agentId, isVisible, loadOlderSegment, hasMoreOlder, loadingOlder, itemCount } = options;
  const compactReplaceSeq = useTimelineStore((s) => s.compactReplaceSeq);
  const compactReplaceAgent = useTimelineStore((s) => s.compactReplaceAgent);

  // Retention knob: how many older pages to restore after a compact rewrites
  // the active history. Default 1; 0 clears, -1 restores all available pages.
  const { settings } = useUserSettings();
  const configuredCompactSessions = settings["display.compact_history_sessions"];
  const compactHistorySessions =
    typeof configuredCompactSessions === "number" &&
    Number.isInteger(configuredCompactSessions) && configuredCompactSessions >= -1
      ? configuredCompactSessions
      : 1;

  const retentionRef = useRef<{ thread: number; remaining: number; attempts: number } | null>(
    null,
  );
  const retentionInFlightRef = useRef<{ pending: object } | null>(null);
  const ownerRef = useRef<object | null>(null);
  // Selection/visibility ownership is separate from the compact edge. A late
  // completion cannot consume a newer run, including A-to-B-to-A or hide/show.
  useLayoutEffect(() => {
    const owner = agentId !== null && isVisible ? {} : null;
    ownerRef.current = owner;
    retentionInFlightRef.current = null;
    if (retentionRef.current?.thread !== agentId) retentionRef.current = null;
    return () => { ownerRef.current = null; };
  }, [agentId, isVisible]);
  const lastRetentionSeqRef = useRef(useTimelineStore.getState().compactReplaceSeq);
  const runRetention = useCallback(() => {
    const pending = retentionRef.current;
    const owner = ownerRef.current;
    if (!isVisible || pending === null || owner === null || retentionInFlightRef.current?.pending === pending) return;
    if (agentId == null || pending.thread !== agentId) {
      retentionRef.current = null;
      return;
    }
    const st = useTimelineStore.getState();
    if (st.activeThreadId !== agentId) return;
    if (!st.hasMoreOlder || st.loadingOlder) return; // wait for a loadable window
    const run = { pending };
    retentionInFlightRef.current = run;
    void (async () => {
      try {
        while (ownerRef.current === owner && retentionRef.current === pending) {
          if (useTimelineStore.getState().activeThreadId !== agentId) return;
          const result = await loadOlderSegment();
          if (ownerRef.current !== owner || retentionRef.current !== pending) return;
          const current = pending;
          if (result === "aborted") return;
          if (result === "unready" || result === "failed") {
            current.attempts += 1;
            if (current.attempts >= RETENTION_MAX_ATTEMPTS) retentionRef.current = null;
            return;
          }
          if (result === "exhausted") {
            retentionRef.current = null;
            return;
          }
          current.attempts = 0;
          if (current.remaining === -1) {
            // Yield between pages: an unlimited walk must leave room for
            // selection/visibility changes and browser rendering.
            await new Promise<void>((resolve) => setTimeout(resolve, 0));
            continue;
          }
          current.remaining -= 1;
          if (current.remaining <= 0) retentionRef.current = null;
        }
      } finally {
        if (retentionInFlightRef.current === run) retentionInFlightRef.current = null;
      }
    })();
  }, [agentId, isVisible, loadOlderSegment]);
  useEffect(() => {
    if (compactReplaceSeq === lastRetentionSeqRef.current) return;
    lastRetentionSeqRef.current = compactReplaceSeq;
    if (agentId == null || compactReplaceAgent !== agentId || compactHistorySessions === 0) return;
    retentionRef.current = { thread: agentId, remaining: compactHistorySessions, attempts: 0 };
    runRetention();
  }, [compactReplaceSeq, compactReplaceAgent, agentId, compactHistorySessions, runRetention]);
  // Retry a pending retention as the post-replace window settles: the GET
  // refetch flips hasMoreOlder on, prepends change the item count — either
  // can make a previously-blocked attempt loadable.
  useEffect(() => {
    runRetention();
  }, [hasMoreOlder, loadingOlder, itemCount, runRetention]);
}
