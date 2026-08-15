"use client";

// The header-bar alerts badge (user design 2026-08-12): the unread count as
// a pill button; clicking jumps to the alert section (/insights#alerts),
// which auto-marks everything read. Renders nothing when there is nothing
// unread — a zero badge is noise.

import { Bell } from "lucide-react";

import { cn } from "@/lib/utils";
import Link from "next/link";

import { FLEX } from "@/lib/layout";
import { useAlerts } from "@/lib/use-alerts";

export function AlertsBadge() {
  const { data } = useAlerts();
  const unread = data?.meta.unread_count ?? 0;
  if (unread === 0) return null;

  return (
    <Link
      href="/insights#alerts"
      aria-label={`${unread} unread alerts`}
      data-testid="alerts-badge"
      className={cn("relative size-8 items-center justify-center rounded-md transition-colors hover:bg-sidebar-accent", FLEX)}
    >
      <Bell className="size-4" aria-hidden />
      <span
        data-testid="alerts-badge-count"
        className="absolute -right-1 -top-1 rounded-full bg-destructive px-1 text-[10px] font-semibold tabular-nums leading-4 text-white"
      >
        {unread > 99 ? "99+" : unread}
      </span>
    </Link>
  );
}
