"use client";

import { formatAbsolute } from "@/lib/time";

// Timestamp on agent_reasoning / agent_chat / agent_code headers.
// `[YYYY-MM-DD HH:MM:SS TZ]`, e.g. `[2026-05-06 16:56:29 PDT]`. When
// `showWeekday` is true, the weekday abbreviated name is included between
// date and time: `[YYYY-MM-DD DayOfWeek HH:MM:SS TZ]`,
// e.g. `[2026-05-06 Tue 16:56:29 PDT]`.
//
// Uses the viewer's local timezone (Intl default); TZ abbrev follows the
// locale. May disagree with backend `settings.timezone` on cross-tz
// deployments — accepted as the trade-off for not needing server-side
// tz config in the frontend.
//
// The shape mirrors the agent-facing stamp (`shared.config.format_timestamp`)
// but keeps the trailing timezone, which the backend dropped: the agent is
// told its cluster timezone once, in a standing context note, and every
// timestamp it reads is in that one timezone. A viewer has no such
// declaration — the timezone here is whatever browser they opened — so the
// label has to ride along.

export function formatItemTime(iso: string, showWeekday = false): string {
  const formatted = formatAbsolute(iso, { weekday: showWeekday });
  return formatted ? `[${formatted}]` : "";
}

export function ItemTimestamp({ iso, showWeekday = false }: { iso: string; showWeekday?: boolean }) {
  const time = formatItemTime(iso, showWeekday);
  if (!time) return null;
  return (
    <span className="ml-2 text-2xs text-muted-foreground/60 font-mono tabular-nums">
      {time}
    </span>
  );
}
