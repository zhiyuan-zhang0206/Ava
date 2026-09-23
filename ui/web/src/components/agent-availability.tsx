"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { FLEX } from "@/lib/layout";
import type { AgentRow } from "@/lib/types";

/** Selected-agent observation; the roster's SSE refresh does not track host probes. */
export function AgentAvailability({ agent }: { agent: AgentRow }) {
  const t = useTranslations("agentAvailability");
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 15_000);
    return () => clearInterval(timer);
  }, []);
  const { data } = useQuery({
    queryKey: ["agent-availability", agent.agent_id],
    queryFn: ({ signal }) => api.getAgent(agent.agent_id, signal),
    refetchInterval: 15_000,
  });
  const availability = data?.availability ?? agent.availability;
  const observedAt = Date.parse(availability?.observed_at ?? "");
  const fresh = Number.isFinite(observedAt)
    && observedAt <= now && now - observedAt <= 120_000;
  const reason = fresh ? availability?.reason ?? "unknown" : "unknown";
  const detail = availability?.admission_outcome;
  const message = reason === "admission_refused" && detail
    ? t(detail)
    : t(reason);

  return (
    <div className="px-4 py-1 text-xs text-muted-foreground" role="status">
      <div className={`mx-auto ${FLEX} items-center gap-2 border-t border-border pt-1.5`}>
        <span>{message}</span>
        {(reason === "host_unavailable" || reason === "admission_refused" || reason === "unknown") && (
          <Link href="/insights/status" className="shrink-0 underline underline-offset-2">
            {t("machineDiagnostics")}
          </Link>
        )}
      </div>
    </div>
  );
}
