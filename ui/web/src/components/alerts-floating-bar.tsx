"use client";

// The timeline's floating aggregation bar (user design 2026-08-12): a pill
// at the top center of the conversation column showing "N 条未解决". Clicking
// jumps to the alert section (/insights#alerts — the history list, which
// auto-marks everything read). The bar auto-disappears ~3 minutes after the
// last count increase; the next new unresolved alert brings it back.

import { Bell } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { FLEX } from "@/lib/layout";
import { useAlerts } from "@/lib/use-alerts";
import { cn } from "@/lib/utils";

/** Auto-hide window (user design: ~3 minutes, then fade away). */
const AUTO_HIDE_MS = 3 * 60 * 1000;

export function AlertsFloatingBar() {
  const t = useTranslations("alerts");
  const { data } = useAlerts();
  const unresolved = data?.meta.unresolved_count ?? 0;
  const [visible, setVisible] = useState(unresolved > 0);
  const lastIncreaseRef = useRef<number | null>(null);
  const prevCountRef = useRef(unresolved);

  // Track increases: a rising count (or the first load with alerts pending)
  // re-arms the 3-minute visibility window.
  useEffect(() => {
    if (unresolved > prevCountRef.current || (unresolved > 0 && lastIncreaseRef.current === null)) {
      lastIncreaseRef.current = Date.now();
      setVisible(true);
    }
    prevCountRef.current = unresolved;
  }, [unresolved]);

  // Auto-hide after the window; a re-arm (above) brings it back.
  useEffect(() => {
    if (!visible) return;
    const remaining = AUTO_HIDE_MS - (Date.now() - (lastIncreaseRef.current ?? Date.now()));
    const timer = setTimeout(() => setVisible(false), Math.max(remaining, 0));
    return () => clearTimeout(timer);
  }, [visible]);

  if (!visible || unresolved === 0) return null;

  return (
    <Link
      href="/insights#alerts"
      data-testid="alerts-floating-bar"
      className={cn(
        "absolute left-1/2 top-12 z-30 -translate-x-1/2",
        FLEX,
        "items-center gap-1.5 rounded-full border border-border bg-background/90",
        "px-3 py-1.5 font-mono text-xs text-foreground shadow-sm backdrop-blur-md",
        "transition-colors hover:bg-accent",
      )}
    >
      <Bell className="size-3.5 shrink-0 text-destructive" aria-hidden />
      <span className="tabular-nums font-semibold">{unresolved}</span>
      <span>{t("unresolved")}</span>
    </Link>
  );
}
