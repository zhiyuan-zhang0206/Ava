"use client";

import { formatAbsolute } from "@/lib/time";

// Compact browser-local timestamp for crowded timeline headers. Today's rows
// show only HH:MM; older rows add MM-DD and, when enabled, the weekday. Exact
// seconds and timezone remain available through ItemTimestamp's title metadata.
export function formatItemTime(
  iso: string,
  showWeekday = false,
  now: Date = new Date(),
): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const hour = String(date.getHours()).padStart(2, "0");
  const minute = String(date.getMinutes()).padStart(2, "0");
  const sameDay =
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate();
  if (sameDay) return `${hour}:${minute}`;
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const weekday = showWeekday
    ? ` ${new Intl.DateTimeFormat("en-US", { weekday: "short" }).format(date)}`
    : "";
  return `${month}-${day}${weekday} ${hour}:${minute}`;
}

export function ItemTimestamp({ iso, showWeekday = false }: { iso: string; showWeekday?: boolean }) {
  const time = formatItemTime(iso, showWeekday);
  if (!time) return null;
  const fullTime = formatAbsolute(iso, { weekday: showWeekday });
  return (
    <span
      title={fullTime}
      className="ml-2 text-[11px] text-muted-foreground font-mono tabular-nums"
    >
      {time}
    </span>
  );
}
