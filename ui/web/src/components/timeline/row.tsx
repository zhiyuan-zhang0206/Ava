"use client";

// Per-item card-config cache + the memoized row renderer. Split out of
// index.tsx (outlier cleanup, task #1010) so the view file stays under the
// 800-line budget; behavior is identical.

import { memo, useCallback } from "react";

import type { BackendTimelineItem } from "@/lib/types";

import { CopyButton, ForkButton } from "./buttons";
import { type CardConfig, CardHeader, MessageCard, messageCardConfig } from "./card";
import { ItemView } from "./item";
import { ItemErrorBoundary } from "./item-error-boundary";

// Per-item card-config cache. messageCardConfig is pure over `item`, and the
// store preserves object identity for items unchanged across a commit (its
// slice()-based reducers replace only the changed item), so a WeakMap keyed on
// the item reference hands back a STABLE config object for every unchanged row.
// That stability — combined with the memoized TimelineRow below — is what lets
// an unchanged row skip re-render on every streaming chunk: the items ARRAY
// identity changes each chunk, but the individual item refs (and thus their
// configs) do not. Recomputed only when an item's reference changes.
const configCache = new WeakMap<BackendTimelineItem, CardConfig | null>();
export function cardConfigFor(item: BackendTimelineItem): CardConfig | null {
  if (configCache.has(item)) return configCache.get(item) ?? null;
  const config = messageCardConfig(item);
  configCache.set(item, config);
  return config;
}

// One timeline row, memoized so an unchanged item skips its entire subtree
// (card container + header summary scan + body) on every streaming chunk.
// Every prop is either a primitive or a reference stable across commits for an
// unchanged item: `item` (store-preserved ref), `config` (WeakMap cache),
// `expanded`/`streaming`/`showActions` (booleans), `onToggle` (parent's stable
// useCallback), and `onFork`/`forkPending` (inert — null/false — for every row
// except the single fork row). config === null is the ephemeral system marker,
// which renders bare (no card, not collapsible).
export const TimelineRow = memo(function TimelineRow({
  item,
  config,
  streaming,
  expanded,
  showActions,
  onToggle,
  onFork,
  forkPending,
}: {
  item: BackendTimelineItem;
  config: CardConfig | null;
  streaming: boolean;
  expanded: boolean;
  showActions: boolean;
  onToggle: (id: string, kind: BackendTimelineItem["kind"]) => void;
  onFork: (() => void) | null;
  forkPending: boolean;
}) {
  // Bound toggle for the header. Created inside the memoized row, so it stays
  // stable across the PARENT's re-renders (the row re-renders only when its own
  // props change) — that is what keeps CardHeader's own memo intact.
  const handleToggle = useCallback(
    () => onToggle(item.item_id, item.kind),
    [onToggle, item.item_id, item.kind],
  );

  if (config === null) {
    return (
      <div data-item-id={item.item_id} className="timeline-item">
        <ItemErrorBoundary resetKey={item.payload}>
          <ItemView item={item} streaming={streaming} />
        </ItemErrorBoundary>
      </div>
    );
  }

  // Built inside the memoized row (not passed in from the parent's per-render
  // `renderRow` closure) so the JSX element stays a fresh object only when
  // THIS row's own props change — passing it down from renderRow would
  // recreate the node on every TimelineView render and defeat TimelineRow's
  // memo for every row, not just this one. Fork sits left of copy; copy is
  // always the rightmost action (row is `justify-end`, so DOM order = visual
  // left-to-right order).
  //
  // Skip the card-level CopyButton for kinds that already render their own
  // per-block copy button inside the item body (agent_code via PythonCode,
  // code_output via EnvelopeContent showCopy) — the card-level button would
  // copy the same payload and overlap visually with the body-level one.
  const hasInternalCopy = item.kind === "agent_code" || item.kind === "code_output";
  const actions = showActions ? (
    <>
      {onFork ? <ForkButton onFork={onFork} pending={forkPending} /> : null}
      {!hasInternalCopy ? <CopyButton text={item.payload} /> : null}
    </>
  ) : null;

  return (
    <div data-item-id={item.item_id} className="timeline-item">
      {/* Boundary wraps header + content together: the header's summary
          derivation parses the payload too, so a malformed payload must
          not escape the per-item fallback. */}
      <ItemErrorBoundary resetKey={item.payload}>
        <MessageCard config={config} actions={actions}>
          <CardHeader item={item} config={config} expanded={expanded} onToggle={handleToggle} />
          {expanded ? (
            <div className="px-3 pb-2 pt-0.5">
              <ItemView item={item} streaming={streaming} />
            </div>
          ) : null}
        </MessageCard>
      </ItemErrorBoundary>
    </div>
  );
});
