"use client";

// UpdatingPage — full-screen "System updating..." page shown when the cluster
// is known to be updating, regardless of auth status. It does not reload: the
// cluster-health poll clears the state and AuthGuard reveals the app in place,
// or redirects an expired session to /login.

import { Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { FLEX, FLEX_COL } from "@/lib/layout";
import { cn } from "@/lib/utils";

export function UpdatingPage() {
  const t = useTranslations("updatingPage");
  const [elapsed, setElapsed] = useState(0);

  // Tick a visible elapsed-seconds counter so the user knows the page is alive
  // and actively waiting (not frozen).
  useEffect(() => {
    const timer = setInterval(() => setElapsed((s) => s + 1), 1_000);
    return () => clearInterval(timer);
  }, []);

  const minutes = Math.floor(elapsed / 60);
  const seconds = elapsed % 60;
  const elapsedLabel = minutes > 0
    ? t("minutesSeconds", { minutes, seconds })
    : t("seconds", { seconds });

  return (
    <div className={cn("min-h-screen items-center justify-center gap-4 bg-background", FLEX, FLEX_COL)}>
      <Loader2 className="size-8 animate-spin text-muted-foreground" aria-hidden />
      <div className="space-y-1 text-center">
        <h1 className="text-xl font-semibold tracking-tight">{t("systemUpdating")}</h1>
        <p className="text-sm text-muted-foreground">
          {t("reconnecting")}
        </p>
      </div>
      <p className="text-xs text-muted-foreground/60">
        Waiting {elapsedLabel}
      </p>
    </div>
  );
}
