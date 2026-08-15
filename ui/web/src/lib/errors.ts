// Normalize any caught value into a string — fetch reject sometimes
// throws a DOMException (some Safari CORS paths); user code may
// `throw "str"` / `throw {code:...}`. `(e as Error).message` would
// yield undefined in those cases, and a toast saying "failure: undefined"
// is impossible to debug.
//
// A plain object (no usable string `.message`) is JSON-serialized rather than
// passed through the bare `String(e)` fallback — `String()` on any non-Error
// object calls its default `.toString()`, which for a plain object is always
// the literal, contentless "[object Object]". JSON.stringify at least shows
// the object's actual shape; only a genuinely unstringifiable value (a
// circular structure) falls through to `String(e)`.

export function errMsg(e: unknown): string {
  if (e instanceof Error) return e.message;
  if (typeof e === "object" && e !== null) {
    if ("message" in e && typeof e.message === "string") return e.message;
    try {
      return JSON.stringify(e);
    } catch {
      // Circular structure — JSON.stringify itself threw. There is no better
      // fallback left than the base toString; deliberate, not a regression.
      // eslint-disable-next-line @typescript-eslint/no-base-to-string
      return String(e);
    }
  }
  return String(e);
}
