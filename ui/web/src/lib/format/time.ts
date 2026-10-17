// Shared timestamp formatters — the single place that turns an ISO string
// into something a human reads. Consolidates what used to be 9 independent,
// drifting implementations (tz audit, 2026-08).
//
// Display principle (audit §4.4): humans see BROWSER-LOCAL time. This is a
// deliberate divergence from what an agent sees (cluster `MEIVA_TIMEZONE`) —
// do not read MEIVA_TIMEZONE here. Consistency means "one wording scheme per
// audience", not "the same clock for every audience".
//
// Three shapes, one job each:
//   - formatAbsolute — full precision + timezone name, for a dedicated
//     "when exactly" slot (inspector detail).
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

/** Compact elapsed span, floors: `45s` / `2h 14m` / `24d 3h` (the day tier
 *  kicks in past 24h). Used for runtimes and TTL-remaining countdowns. */
export function formatUptime(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  const d = Math.floor(h / 24);
  const remH = h % 24;
  return remH ? `${d}d ${remH}h` : `${d}d`;
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

const MS_PER_DAY = 86_400_000;
const TOMORROW_DAYS = 1;
const WEEKDAY_DAYS_MAX = 6;
const DATE_DAYS_MIN = 30;

export interface FormatUpcomingOptions {
  timeZone?: string;
}

// meiva:note
// formatUpcoming renders an upcoming instant as a 12-hour clock time plus the day context its viewer-local calendar date requires, and never as a bare clock time for a future day: clock only later today, 'Tomorrow' on the next local date, a weekday name from two to six days, a day count with the date from seven to 29 days, and the date alone from 30 days. The boundary is local midnight, not 24 elapsed hours, so a DST day still counts as one day. A past instant keeps the shared relative wording and an unparseable input yields an em dash.
// meiva:link ui/web/src/lib/format/docs/time.meiva.okf.md
export function formatUpcoming(
  iso: string | Date,
  now: Date = new Date(),
  opts: FormatUpcomingOptions = {},
): string {
  const target = new Date(iso);
  if (Number.isNaN(target.getTime())) return "—";
  if (target.getTime() <= now.getTime()) return formatRelative(target.toISOString(), now);

  const zone = opts.timeZone;
  const targetDate = localDateFields(target, zone);
  const nowDate = localDateFields(now, zone);
  const days = dayOrdinal(targetDate) - dayOrdinal(nowDate);
  const clock = clockLabel(target, zone);
  const monthDay = monthDayLabel(target, zone, targetDate.year !== nowDate.year);

  if (days <= 0) return clock;
  if (days === TOMORROW_DAYS) return `Tomorrow ${clock}`;
  if (days <= WEEKDAY_DAYS_MAX) return `${intlPart(target, zone, { weekday: "short" })} ${clock}`;
  if (days < DATE_DAYS_MIN) return `In ${days} days (${monthDay})`;
  return monthDay;
}

interface LocalDateFields {
  year: number;
  month: number;
  day: number;
}

function intlFormatter(
  timeZone: string | undefined,
  options: Intl.DateTimeFormatOptions,
): Intl.DateTimeFormat {
  return new Intl.DateTimeFormat("en-US", { ...options, timeZone });
}

function intlPart(
  d: Date,
  timeZone: string | undefined,
  options: Intl.DateTimeFormatOptions,
): string {
  return intlFormatter(timeZone, options).format(d);
}

function localDateFields(d: Date, timeZone?: string): LocalDateFields {
  const parts = intlFormatter(timeZone, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(d);
  const num = (type: string) => Number(parts.find((p) => p.type === type)?.value);
  return { year: num("year"), month: num("month"), day: num("day") };
}

function dayOrdinal({ year, month, day }: LocalDateFields): number {
  return Math.round(Date.UTC(year, month - 1, day) / MS_PER_DAY);
}

export function normalizeDayPeriodSpace(value: string): string {
  return value.replace(/[\u202f\u00a0]/g, " ");
}

function clockLabel(d: Date, timeZone?: string): string {
  return normalizeDayPeriodSpace(
    intlPart(d, timeZone, { hour: "numeric", minute: "2-digit", hour12: true }),
  );
}

function monthDayLabel(d: Date, timeZone: string | undefined, withYear: boolean): string {
  return intlPart(d, timeZone, {
    month: "short",
    day: "numeric",
    ...(withYear ? { year: "numeric" } : {}),
  });
}
