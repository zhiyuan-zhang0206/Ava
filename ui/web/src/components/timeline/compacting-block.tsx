"use client";

// The ticking "Compacting" block — the frontend half of the forced/auto
// compact live run pair (wire: task #3323; this view: task #3324). Mounted
// once at the live tail of the active thread's timeline; self-gating on the
// store's `liveCompact` entry (fed by compact_started / compact_finished on
// the active thread only). The run's ISO `started_at` drives a per-second
// clock; the terminal role settles the label and the block hands over to the
// summary item that lands in the history (matched by compact_id — the store
// retires the entry), or self-hides after a grace window so a missed terminal
// can never leave it ticking forever.

import { Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";

import { formatDuration } from "@/lib/item-summary";
import { FLEX } from "@/lib/layout";
import { useTimelineStore } from "@/lib/timeline-store";
import type { LiveCompact } from "@/lib/timeline-store";
import { useTimelineColors } from "@/lib/use-timeline-colors";
import { cn } from "@/lib/utils";
import { useNow } from "./reasoning-clock";

/** Terminal states linger this long when no summary item follows — the
 *  normal handover retires the block well before that. */
const SETTLE_GRACE_MS = 8_000;
/** A failure has no follow-up summary — keep it readable a little longer. */
const FAILURE_GRACE_MS = 30_000;
/** Safety cap: a still-"running" block older than this lost its terminal. */
const MAX_RUNNING_MS = 30 * 60_000;

export function CompactingBlock() {
  const live = useTimelineStore((s) => s.liveCompact);
  if (live === null) return null;
  return <CompactingBlockBody compact={live} />;
}

function CompactingBlockBody({ compact }: { compact: LiveCompact }) {
  const t = useTranslations("timeline");
  const colors = useTimelineColors();
  // 100ms tick (the reasoning-clock cadence); it also drives the grace
  // expiry below, which must fire even without another store write.
  const now = useNow(true);

  const startedMs = compact.startedAt === null ? null : Date.parse(compact.startedAt);
  const finishedMs = compact.finishedAt === null ? null : Date.parse(compact.finishedAt);
  const settled = compact.status !== null;

  // Self-hide: a terminal whose summary handover never came, or a running
  // block whose terminal event was lost.
  if (settled && finishedMs !== null) {
    const grace = compact.status === "failure" ? FAILURE_GRACE_MS : SETTLE_GRACE_MS;
    if (now - finishedMs > grace) return null;
  } else if (!settled && startedMs !== null && now - startedMs > MAX_RUNNING_MS) {
    return null;
  }

  const label =
    compact.status === null
      ? t("compacting")
      : compact.status === "success"
        ? t("compacted")
        : compact.status === "failure"
          ? t("compactFailed")
          : t("compactSuperseded");
  const elapsedMs = startedMs === null ? null : (finishedMs ?? now) - startedMs;

  return (
    <div
      data-testid="compacting-block"
      data-status={compact.status ?? "running"}
      className={cn(
        FLEX,
        "items-center gap-2 rounded-lg border px-3 py-2 text-xs",
        colors.inbound_system.border,
        colors.inbound_system.bg,
      )}
    >
      {settled ? null : (
        <Loader2 aria-hidden className="size-3.5 shrink-0 animate-spin text-muted-foreground" />
      )}
      <span className="font-medium text-foreground">{label}</span>
      {elapsedMs === null ? null : (
        <span className="tabular-nums text-muted-foreground" data-testid="compacting-elapsed">
          {formatDuration(Math.max(0, elapsedMs))}
        </span>
      )}
    </div>
  );
}
