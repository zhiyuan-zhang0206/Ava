// R4 layer 1 — fold protocol (Task #1024, design v0.6 §5.2).
//
// One SSE stream, one fold: every domain's snapshot×stream reconciliation is a
// PURE reducer `(prev, event) -> next`, applied in ONE place (the fold owner
// inside EventStreamProvider). Hooks no longer hand-roll reconciliation — they
// subscribe to their Query key and nothing else.
//
// Two reducer kinds:
// - FOLDING reducers (agents / pages): the event carries enough to merge into
//   the cached state — `(prev, ev) -> next | undefined` (undefined = "no
//   write", used by the empty-cache guard: never seed a partial before the
//   initial fetch lands).
// - INVALIDATING reducers (notices / fleet-graph / tasks): the event does NOT
//   carry a full row (notice_posted has no body; graph/tasks need server
//   recompute) — the reducer declares the query families to invalidate. This
//   is the design's reconnect-invalidation primitive; the debounce policy is
//   owned by the fold owner, not the hook.

export interface FoldWrite {
  readonly key: readonly unknown[];
  readonly value: unknown;
}
export interface FoldInvalidation {
  readonly key: readonly unknown[];
}

export interface FoldOutcome {
  /** Cache writes to apply (already guarded — never seed an un-fetched key). */
  readonly writes: readonly FoldWrite[];
  /** Query families to invalidate (debounce policy applied by the owner). */
  readonly invalidations: readonly FoldInvalidation[];
}

export const NO_FOLD: FoldOutcome = { writes: [], invalidations: [] };

// ── Merge-protocol primitives (shared by the folding reducers) ──

/** Empty-cache guard: refuse to seed a partial cache from a single SSE event
 *  before the initial fetch lands. `undefined` = fetch pending; `[]` = a real
 *  fetched-empty state (a first event then merges in). */
export function guardSeeded<T>(prev: T[] | undefined): T[] | undefined {
  // undefined = initial fetch pending (never seed); any array is a real state.
  return prev ?? undefined;
}

/** Upsert by key fn: replace-or-append, returning the SAME reference when
 *  nothing material changed (no re-render noise on heartbeat-ish events). */
export function upsertByKey<T>(
  prev: T[] | undefined,
  next: T,
  keyOf: (row: T) => number | string,
  unchanged: (cur: T, next: T) => boolean,
): T[] | undefined {
  const seeded = guardSeeded(prev);
  if (seeded === undefined) return undefined;
  const idx = seeded.findIndex((row) => keyOf(row) === keyOf(next));
  if (idx === -1) return [...seeded, next];
  if (unchanged(seeded[idx], next)) return seeded;
  const out = seeded.slice();
  out[idx] = next;
  return out;
}

/** Remove rows whose key matches, same-reference when nothing matched. */
export function removeByKey<T>(
  prev: T[] | undefined,
  key: number | string,
  keyOf: (row: T) => number | string,
): T[] | undefined {
  const seeded = guardSeeded(prev);
  if (seeded === undefined) return undefined;
  const idx = seeded.findIndex((row) => keyOf(row) === key);
  if (idx === -1) return seeded;
  const out = seeded.slice();
  out.splice(idx, 1);
  return out;
}
