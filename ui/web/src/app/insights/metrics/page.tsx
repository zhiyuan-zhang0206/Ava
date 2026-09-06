"use client";

// /insights/metrics — retired (user ruling 2026-08-04): the hand-made Metrics
// page is replaced by the Grafana dashboards. The route is kept so
// old bookmarks and deep links land on a short transition notice and then
// auto-redirect to the Ops section, which links to the "Ava Ops" Grafana
// dashboard through the gateway's /grafana reverse proxy (the same dashboard
// linked from /insights#ops — see ops/page.tsx).
//
// The backend /api/metrics + /api/metrics/agents endpoints are intentionally
// left in place: scripts/metrics.py (the CLI mirror of shared/metrics.py) and
// any script consumers still read them, and the redirect notice points there
// as the fallback when the Grafana proxy is off (AVA_GRAFANA_PROXY_ENABLED).
//
// The auto-redirect is CONDITIONAL on the /grafana proxy actually answering
// (HEAD probe): when the proxy is off or Grafana is down, redirecting would
// land the user on a broken Grafana link and destroy this page — the
// only place carrying the fallback (CLI / /api/metrics). The probe keeps the
// notice (and its fallback) alive in that case.

import { ExternalLink, LineChart } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { buttonVariants } from "@/components/ui/button";
import { api, API_BASE } from "@/lib/api";
import { FLEX } from "@/lib/layout";
import { cn } from "@/lib/utils";

// Give the notice a beat to render before moving on — a user who follows an
// old link sees *why* they landed on Grafana, not a silent jump.
const REDIRECT_DELAY_MS = 1200;

// Direct Grafana entry for the same dashboard linked from the Ops section.
const GRAFANA_PATH = "/grafana/d/ava-ops-main?from=now-24h&to=now&kiosk";
const GRAFANA_DIRECT = `${API_BASE}${GRAFANA_PATH}`;

export default function MetricsRedirectPage() {
  const t = useTranslations("metrics");
  const router = useRouter();
  // null = probe in flight; true = proxy reachable (redirect will fire);
  // false = proxy off / Grafana down (stay on the fallback notice).
  const [proxyUp, setProxyUp] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    void (async () => {
      let up: boolean;
      try {
        up = (await api.head(GRAFANA_PATH)).ok;
      } catch {
        up = false; // probe failed — stay on the fallback notice
      }
      // eslint-disable-next-line @typescript-eslint/no-unnecessary-condition -- cancelled is set by the cleanup below; flow analysis cannot see it
      if (cancelled) return;
      setProxyUp(up);
      if (up) {
        timer = window.setTimeout(
          () => router.replace("/insights#ops"),
          REDIRECT_DELAY_MS,
        );
      }
    })();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [router]);

  return (
    <div className={cn("justify-center py-12", FLEX)}>
      <div className="w-full max-w-md space-y-4 rounded-md border border-border p-6 text-center">
        <LineChart className="mx-auto size-8 text-muted-foreground" aria-hidden />
        <div>
          <h3 className="text-sm font-semibold">{t("movedTitle")}</h3>
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
            {t("intro")} {" "}
            <Link href="/insights#ops" className="underline underline-offset-2 hover:text-foreground">
              {t("opsSection")}
            </Link>{" "}
            {t("introAfterOps")} {" "}
            {proxyUp === false
              ? t("grafanaUnavailable")
              : t("redirecting")}
          </p>
        </div>
        <div className={cn("justify-center gap-2", FLEX)}>
          <Link href="/insights#ops" className={buttonVariants({ size: "sm" })}>
            {t("openOpsDashboard")}
          </Link>
          <a
            href={GRAFANA_DIRECT}
            className={buttonVariants({ variant: "outline", size: "sm" })}
          >
            <ExternalLink className="mr-1.5 size-3.5" aria-hidden />
            {t("openGrafana")}
          </a>
        </div>
        <p className="text-xs leading-relaxed text-muted-foreground">
          {t("fallbackBefore")} {" "}
          <code className="mx-1 rounded bg-muted px-1 py-0.5 font-mono">{t("metricsScript")}</code>{" "}
          {t("fallbackMiddle")} {" "}
          <code className="ml-1 rounded bg-muted px-1 py-0.5 font-mono">{t("metricsApi")}</code>
          {t("fallbackAfter")}
        </p>
      </div>
    </div>
  );
}
