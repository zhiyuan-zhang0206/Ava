"use client";

// The header-bar alerts badge: the unresolved count as a pill button; clicking
// jumps to the alert section (/insights#alerts). Renders nothing when there is
// nothing unresolved — a zero badge is noise.

import { Bell } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";

import { FLEX } from "@/lib/layout";
import { useAlerts } from "@/lib/use-alerts";
import { cn } from "@/lib/utils";

export function AlertsBadge() {
  const t = useTranslations("alerts");
  const { data } = useAlerts();
  const unresolved = data?.meta.unresolved_count ?? 0;
  if (unresolved === 0) return null;

  return (
    <Link
      href="/insights#alerts"
      aria-label={t("unresolvedAlerts", { count: unresolved })}
      data-testid="alerts-badge"
      className={cn("relative size-8 items-center justify-center rounded-md transition-colors hover:bg-sidebar-accent", FLEX)}
    >
      <Bell className="size-4" aria-hidden />
      <span
        data-testid="alerts-badge-count"
        className="absolute -right-1 -top-1 rounded-full bg-destructive px-1 font-mono text-2xs font-semibold tabular-nums leading-4 text-white"
      >
        {unresolved > 99 ? "99+" : unresolved}
      </span>
    </Link>
  );
}
