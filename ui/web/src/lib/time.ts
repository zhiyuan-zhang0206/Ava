// Shared timestamp formatters — the single place that turns an ISO string
// into something a human reads. Consolidates what used to be 9 independent,
// drifting implementations (tz audit, 2026-08).
//
// Display principle (audit §4.4): humans see BROWSER-LOCAL time. This is a
// deliberate divergence from what an agent sees (cluster `AVA_TIMEZONE`) —
// do not read AVA_TIMEZONE here. Consistency means "one wording scheme per
// audience", not "the same clock for every audience".
//
// Three shapes, one job each:
//   - formatAbsolute — full precision + timezone name, for a dedicated
//     "when exactly" slot (inspector detail, computer-trace rows).
//   - formatShort — compact, no timezone, for space-constrained rows
//     (sidebar time column, a wall-clock cell).
//   - formatRelative — one signed wording scheme ("in 4m" / "5m ago" / "now")
//     for every relative-time surface in the app.

export interface FormatAbsoluteOptions {
  /** Include the abbreviated weekday name between date and time. */
  weekday?: boolean;
}

/** Full datetime with seconds and a short timezone name, in the viewer's
 *  local timezone: `2026-07-25 02:53:29 PDT`, or with `weekday: true`:
 *  `2026-07-25 Sat 02:53:29 PDT`. Returns "" for an unparseable iso string —
 *  callers that need a placeholder substitute one (e.g. "—"). */
export function formatAbsolute(iso: string, opts: FormatAbsoluteOptions = {}): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const options: Intl.DateTimeFormatOptions = {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZoneName: "short",
  };
  if (opts.weekday) options.weekday = "short";
  const parts = new Intl.DateTimeFormat("en-US", options).formatToParts(d);
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  const datePart = `${get("year")}-${get("month")}-${get("day")}`;
  const weekdayPart = opts.weekday ? ` ${get("weekday")}` : "";
  const timePart = `${get("hour")}:${get("minute")}:${get("second")}`;
  return `${datePart}${weekdayPart} ${timePart} ${get("timeZoneName")}`;
}

export interface FormatShortOptions {
  /** Include the `M/D` date prefix. Default true; pass false for a bare
   *  wall-clock cell (e.g. "a paused-until time today"). */
  includeDate?: boolean;
}

/** Compact, no timezone: `7/12 15:45`, or with `includeDate: false`:
 *  `15:45`. For space-constrained rows where a full timestamp doesn't fit —
 *  the viewer's own clock is implied context. Returns "—" for an
 *  unparseable iso string. */
export function formatShort(iso: string, opts: FormatShortOptions = {}): string {
  const { includeDate = true } = opts;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const h = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  if (!includeDate) return `${h}:${mi}`;
  const mo = d.getMonth() + 1;
  const day = d.getDate();
  return `${mo}/${day} ${h}:${mi}`;
}

/** Signed relative time, both directions: `in 4m` (future) / `5m ago`
 *  (past) / `now` within a minute. Seconds through years. The one wording
 *  scheme every relative-time surface in the app uses. Returns "—" for an
 *  unparseable iso string. */
export function formatRelative(iso: string, now: Date = new Date()): string {
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return "—";
  const deltaSec = Math.round((t - now.getTime()) / 1000);
  const mag = Math.abs(deltaSec);
  if (mag < 60) return "now";
  const min = Math.floor(mag / 60);
  let span: string;
  if (min < 60) span = `${min}m`;
  else if (min < 60 * 24) span = `${Math.floor(min / 60)}h`;
  else if (min < 60 * 24 * 30) span = `${Math.floor(min / 60 / 24)}d`;
  else if (min < 60 * 24 * 365) span = `${Math.floor(min / 60 / 24 / 30)}mo`;
  else span = `${Math.floor(min / 60 / 24 / 365)}y`;
  return deltaSec > 0 ? `in ${span}` : `${span} ago`;
}
