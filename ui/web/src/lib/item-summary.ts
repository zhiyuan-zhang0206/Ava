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
  /** Per-method call counts as recorded by the executing run, grouped by
   *  namespace alphabetically, then descending by count and method name.
   *  `method` is the full dotted path after `ava.`
   *  (e.g. `files.read`, `self.log`) so reads and writes on the same
   *  namespace stay distinct. Empty when none were recorded (plain Python,
   *  or the field is absent while streaming). */
  readonly calls: readonly SdkCall[];
  /** Total `ava.*` call sites (sum of `calls[].count`). */
  readonly totalCalls: number;
  /** Non-blank line count of the snippet — shown when there are no recorded
   *  calls. */
  readonly lines: number;
}

export interface OutputSummary {
  readonly lines: number;
  readonly chars: number;
  /** Whether the output looks like it carried a Python error (traceback or a
   *  trailing `SomeError: ...` line) — surfaced as a red dot on the chip. */
  readonly hasError: boolean;
}

/** The first dotted segment of a method; an undotted method is its own namespace. */
export function sdkNamespace(method: string): string {
  const dot = method.indexOf(".");
  return dot === -1 ? method : method.slice(0, dot);
}

/** Order namespace groups lexicographically, independent of counts, so a live
 *  tally cannot move a whole group. Within each group, sort by descending
 *  count, breaking ties by method name. Only positions within a group can
 *  change as counts grow. */
export function orderSdkCalls(calls: readonly SdkCall[]): SdkCall[] {
  return [...calls].sort((a, b) =>
    sdkNamespace(a.method).localeCompare(sdkNamespace(b.method))
    || b.count - a.count
    || a.method.localeCompare(b.method),
  );
}

export function summarizeCode(
  payload: string,
  sdkCalls?: readonly SdkCall[] | null,
): CodeSummary {
  // Calls come only from the backend's recorded tally — the SDK-call
  // metadata projected onto the item; the runtime that executed the snippet
  // counted its real calls. No text scanning, not even as a streaming
  // fallback: while the field is absent the chip shows the line count alone.
  const calls = orderSdkCalls(sdkCalls ?? []);
  return {
    calls,
    totalCalls: calls.reduce((sum, c) => sum + c.count, 0),
    lines: nonBlankLineCount(payload),
  };
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
