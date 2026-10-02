// R4 layer 1 — fold protocol (Task #1024, design v0.6 §5.2).
//
// One SSE stream, one owner: domain reducers declare Query invalidations from
// event hints. The owner coalesces and repairs reads, including an event that
// arrives during an earlier GET. Hooks subscribe to their Query keys.

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
