"use client";

// Timestamp on agent_reasoning / agent_chat / agent_code headers.
// Format aligns with backend `shared.config.now_timestamp()`:
// `[YYYY-MM-DD HH:MM:SS TZ]`, e.g. `[2026-05-06 16:56:29 PDT]`.
// When `showWeekday` is true, the weekday abbreviated name is included
// between date and time: `[YYYY-MM-DD DayOfWeek HH:MM:SS TZ]`,
// e.g. `[2026-05-06 Tue 16:56:29 PDT]`.
//
// Uses the user local timezone (Intl default); TZ abbrev follows the
// locale. May disagree with backend `settings.timezone` on cross-tz
// deployments — accepted as the trade-off for not needing server-side
// tz config in the frontend.

export function formatItemTime(iso: string, showWeekday = false): string {
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
  if (showWeekday) {
    options.weekday = "short";
  }
  const parts = new Intl.DateTimeFormat("en-US", options).formatToParts(d);
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  if (showWeekday) {
    const weekday = get("weekday");
    return `[${get("year")}-${get("month")}-${get("day")} ${weekday} ${get("hour")}:${get("minute")}:${get("second")} ${get("timeZoneName")}]`;
  }
  return `[${get("year")}-${get("month")}-${get("day")} ${get("hour")}:${get("minute")}:${get("second")} ${get("timeZoneName")}]`;
}

export function ItemTimestamp({ iso, showWeekday = false }: { iso: string; showWeekday?: boolean }) {
  const time = formatItemTime(iso, showWeekday);
  if (!time) return null;
  return (
    <span className="ml-2 text-[10px] text-muted-foreground/60 font-mono tabular-nums">
      {time}
    </span>
  );
}
