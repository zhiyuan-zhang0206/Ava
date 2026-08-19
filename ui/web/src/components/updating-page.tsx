"use client";

// UpdatingPage — full-screen "System updating..." page shown when the cluster
// is known to be updating and the backend is unreachable (auth check failed).
// Replaces the login page to avoid confusing users with a password prompt
// during planned downtime.
//
// Polls the auth endpoint every 5s; when the backend answers again (any
// response — a paused gateway 503s the check), reloads the page: a valid
// session transitions to the normal app, an expired one lands on the login
// page.

import { Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
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

  // Periodically retry auth — when the backend answers again, reload the page.
  // A full reload is the simplest reliable recovery: AuthProvider re-checks
  // auth on mount, and it either sees "authenticated" (renders app) or
  // "unauthenticated" (renders login page). ANY successful response triggers
  // the reload, `authenticated: false` included: /api/auth/check has no
  // control-plane pause exemption, so a paused (mid-update) gateway 503s it
  // (the catch below) — an answer means the gateway is serving again, and if
  // the session expired meanwhile (e.g. across a host crash) the reload lands
  // on the login page. Reloading only on `authenticated: true` left an
  // expired session stuck on this page forever (2026-08-19 incident).
  useEffect(() => {
    let mounted = true;
    // In-flight guard: if a checkAuth hangs (a half-dead backend accepts the
    // connection but never answers), the 5s tick must not stack another
    // request on top of it. The next tick after the response (success or
    // failure) starts a fresh check.
    let inFlight = false;

    const retry = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        await api.checkAuth();
        if (mounted) {
          window.location.reload();
        }
      } catch {
        // Backend still unreachable — nothing to do.
      } finally {
        inFlight = false;
      }
    };

    // Fire immediately so a quick recovery is caught fast.
    void retry();
    const interval = setInterval(() => { void retry(); }, 5_000);
    return () => {
      mounted = false;
      clearInterval(interval);
    };
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
