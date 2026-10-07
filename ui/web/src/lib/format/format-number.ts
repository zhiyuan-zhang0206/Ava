// Shared compact scaling for token counts and other large counts.
//
// Every token-count display in the app renders through one of the two
// exports below; both carry at the same thresholds — 1_000 (k),
// 1_000_000 (M), 1_000_000_000 (B), 1_000_000_000_000 (T) — so a count never
// overflows its unit the way "4109.3M" did for 4.1093e9 tokens. They differ
// only in mantissa precision: `formatTokens` (two decimals from M up) suits
// breakdown rows with room to spare; `formatTokensCompact` (one decimal
// through M) suits narrow tiles and chips. Earlier each consumer kept its own
// local copy that forgot the upper tiers; new large-count displays import
// from here instead of re-deriving the scaling.

/** Token count at breakdown precision: `820` / `26.3k` / `1.00M` / `4.11B`. */
export function formatTokens(n: number): string {
  return formatScaled(n, 2);
}

/** Token count at compact precision: `820` / `1.2k` / `3.4M` / `4.11B`. */
export function formatTokensCompact(n: number): string {
  return formatScaled(n, 1);
}

// k → M → B → T. `mDecimals` is the caller's mantissa precision at the M tier
// (formatTokensCompact stays one decimal there; the other tiers are fixed).
function formatScaled(n: number, mDecimals: number): string {
  if (n >= 1_000_000_000_000) return `${(n / 1_000_000_000_000).toFixed(2)}T`;
  if (n >= 1_000_000_000) {
    const b = Number((n / 1_000_000_000).toFixed(2));
    if (b < 1_000) return `${(n / 1_000_000_000).toFixed(2)}B`;
    return `${(n / 1_000_000_000_000).toFixed(2)}T`;
  }
  if (n >= 1_000_000) {
    const m = Number((n / 1_000_000).toFixed(mDecimals));
    if (m < 1_000) return `${(n / 1_000_000).toFixed(mDecimals)}M`;
    return `${(n / 1_000_000_000).toFixed(2)}B`;
  }
  if (n >= 1_000) {
    const k = Number((n / 1_000).toFixed(1));
    if (k < 1_000) return `${(n / 1_000).toFixed(1)}k`;
    return `${(n / 1_000_000).toFixed(mDecimals)}M`;
  }
  return String(n);
}
