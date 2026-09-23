"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { errMsg } from "@/lib/errors";
import { AGENTS_QUERY_KEY, AGENT_DETAIL_QUERY_KEY } from "@/lib/fold/agents";
import { FLEX } from "@/lib/layout";
import type { AgentRow } from "@/lib/types";

/** Selected-agent observation; the roster's SSE refresh does not track host probes. */
export function AgentAvailability({ agent }: { agent: AgentRow }) {
  const t = useTranslations("agentAvailability");
  const queryClient = useQueryClient();
  const [retryPending, setRetryPending] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
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
  const current = data ?? agent;
  const availability = current.availability;
  const observedAt = Date.parse(availability?.observed_at ?? "");
  const fresh = Number.isFinite(observedAt)
    && observedAt <= now && now - observedAt <= 120_000;
  const launchFailed = availability?.reason.startsWith("launch_") ?? false;
  const reason = fresh || launchFailed ? availability?.reason ?? "unknown" : "unknown";
  const canRetry = current.status === "idling" && (
    launchFailed || (reason === "unknown" && current.started_at === null)
  );
  const detail = availability?.admission_outcome;
  const message = reason === "admission_refused" && detail
    ? t(detail)
    : t(reason);

  async function retryLaunch() {
    setRetryPending(true);
    setRetryError(null);
    try {
      await api.retryAgentLaunch(agent.agent_id);
    } catch (error) {
      setRetryError(`${t("retryLaunchFailed")}: ${errMsg(error)}`);
    } finally {
      await queryClient.invalidateQueries({ queryKey: AGENTS_QUERY_KEY });
      await queryClient.invalidateQueries({ queryKey: [...AGENT_DETAIL_QUERY_KEY, agent.agent_id] });
      await queryClient.invalidateQueries({ queryKey: ["agent-availability", agent.agent_id] });
      setRetryPending(false);
    }
  }

  return (
    <div className="px-4 py-1 text-xs text-muted-foreground" role="status">
      <div className={`mx-auto ${FLEX} items-center gap-2 border-t border-border pt-1.5`}>
        <span>{message}</span>
        {launchFailed && availability?.evidence_at && (
          <span>{t("failureObservedAt", {
            time: new Date(availability.evidence_at).toLocaleString(), machine: agent.machine,
          })}</span>
        )}
        {canRetry && (
          <button type="button" className="shrink-0 underline underline-offset-2"
            disabled={retryPending} onClick={() => { void retryLaunch(); }}>
            {t("retryLaunch")}
          </button>
        )}
        {retryError && <span>{retryError}</span>}
        {(reason === "host_unavailable" || reason === "admission_refused" || reason === "unknown") && (
          <Link href="/insights/status" className="shrink-0 underline underline-offset-2">
            {t("machineDiagnostics")}
          </Link>
        )}
      </div>
    </div>
  );
}
