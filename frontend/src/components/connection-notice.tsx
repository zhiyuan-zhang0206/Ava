"use client";

// ConnectionNotice — inline notification banner rendered inside the timeline
// area (not as a full-width page-top bar). Shows SSE connection status
// without causing layout shifts.
//
// No "cluster updating" state here: while a rollout is in flight, an
// unauthenticated reload is served the full-screen UpdatingPage (see
// components/auth/auth-guard.tsx), and an already-open authenticated tab
// just sees this same SSE-drop notice — a separate "updating" banner on top
// of it would be redundant duplicate signaling.

import { useStore } from "@/lib/store";

export function ConnectionNotice() {
  const connState = useStore((s) => s.connState);

  if (connState === "open") return null;

  const isClosed = connState === "closed";
  const className = isClosed
    ? "rounded-md bg-destructive/15 text-destructive border border-destructive/40 px-3 py-1.5 text-xs"
    : "rounded-md bg-amber-500/10 text-amber-700 dark:text-amber-400 border border-amber-500/30 px-3 py-1.5 text-xs";
  const message = isClosed
    ? "SSE connection lost — refresh the page to restore live updates"
    : "SSE reconnecting…";

  return (
    <div role="status" aria-live="polite" className={className}>
      {message}
    </div>
  );
}
