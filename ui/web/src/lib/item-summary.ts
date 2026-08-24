// Pure summarizers for the toggle chips (timeline/toggle-chip).
//
// Every Thinking / Code / Output item carries an always-visible chip header
// with a one-line glanceable summary, so the user keeps a sense of what the
// agent did even while the content below is collapsed. These functions derive
// that summary from the item payload; they are pure (no React) so the heavy
// parsing stays vitest-testable and memoizable.

/** One SDK method's call count in a code block, e.g.
 *  `{ method: "files.read", count: 3 }` for `ava.files.read(...)` ×3. */
export interface SdkCall {
  readonly method: string;
  readonly count: number;
}

export interface CodeSummary {
  /** Per-method `ava.<method>(...)` call counts, descending by count then
   *  method name. `method` is the full dotted path after `ava.` (e.g.
   *  `files.read`, `self.log`) so reads and writes on the same
   *  namespace stay distinct. Empty when the snippet calls no SDK (plain
   *  Python). */
  readonly calls: readonly SdkCall[];
  /** Total `ava.*` call sites (sum of `calls[].count`). */
  readonly totalCalls: number;
  /** Non-blank line count of the snippet — the fallback signal shown when the
   *  snippet makes no SDK calls. */
  readonly lines: number;
}

export interface OutputSummary {
  readonly lines: number;
  readonly chars: number;
  /** Whether the output looks like it carried a Python error (traceback or a
   *  trailing `SomeError: ...` line) — surfaced as a red dot on the chip. */
  readonly hasError: boolean;
}

// `ava.<dotted.path>(` call sites. The full dotted path after `ava.` is the
// method key (`ava.files.read(...)` -> `files.read`), so a namespace's read
// and write count separately. Requires the trailing `(` so attribute reads
// like `ava.self.AGENT_ID` are not counted as calls. `\bava\.` avoids matching
// `something_ava.x(`.
const AVA_CALL_RE = /\bava\.([a-z_]\w*(?:\.[a-z_]\w*)*)\s*\(/g;

export function summarizeCode(
  payload: string,
  sdkCalls?: readonly SdkCall[] | null,
): CodeSummary {
  // When the backend supplies AST-parsed SDK calls (committed items), use
  // them directly — zero false positives from string literals / comments.
  // During streaming the field is absent; fall back to the regex heuristic
  // for the live chip (transient, replaced by the committed snapshot).
  if (sdkCalls != null) {
    const totalCalls = sdkCalls.reduce((sum, c) => sum + c.count, 0);
    return { calls: sdkCalls, totalCalls, lines: nonBlankLineCount(payload) };
  }
  const counts = new Map<string, number>();
  let total = 0;
  for (const m of payload.matchAll(AVA_CALL_RE)) {
    const method = m[1];
    counts.set(method, (counts.get(method) ?? 0) + 1);
    total += 1;
  }
  const calls = [...counts.entries()]
    .map(([method, count]) => ({ method, count }))
    .sort((a, b) => b.count - a.count || a.method.localeCompare(b.method));
  return { calls, totalCalls: total, lines: nonBlankLineCount(payload) };
}

// Traceback header, or a line that is just `WordError: ...` / `WordException:
// ...` (multiline flag so a trailing error line in a longer dump is caught).
const ERROR_RE = /Traceback \(most recent call last\):|^[A-Za-z_][\w.]*(Error|Exception):/m;

export function summarizeOutput(payload: string): OutputSummary {
  return {
    lines: lineCount(payload),
    chars: payload.length,
    hasError: ERROR_RE.test(payload),
  };
}

// --- formatting helpers (shared by the chip) ---

/** Compact token count: `820` / `1.2k` / `3.4M`. */
export function formatTokensCompact(n: number): string {
  if (n < 1_000) return String(n);
  if (n < 1_000_000) return `${(n / 1_000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

/** Human duration from milliseconds: `0.1s` / `0.3s` / `8s` / `1m 12s`. Shared
 *  by the thinking chip ("Thinking for Xs") and the output chip ("ran in Xs").
 *  Values <0.1s floor at 0.1s; 0.1–1s round to 0.1s; 1–60s round to whole
 *  seconds; >=60s switches to `Nm Ss`. */
export function formatDuration(ms: number): string {
  const totalSec = ms / 1000;
  if (totalSec < 0.1) return "0.1s";
  if (totalSec < 1) return `${Math.round(totalSec * 10) / 10}s`;
  if (totalSec < 60) return `${Math.round(totalSec)}s`;
  const m = Math.floor(totalSec / 60);
  const s = Math.round(totalSec % 60);
  return s === 0 ? `${m}m` : `${m}m ${s}s`;
}

function lineCount(s: string): number {
  if (s === "") return 0;
  // Trailing newline is a terminator, not an extra empty line.
  const body = s.endsWith("\n") ? s.slice(0, -1) : s;
  return body.split("\n").length;
}

function nonBlankLineCount(s: string): number {
  return s.split("\n").filter((l) => l.trim() !== "").length;
}
