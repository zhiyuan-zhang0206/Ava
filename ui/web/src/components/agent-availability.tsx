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

// KEEP (task #3696 exception inventory): protocol tolerance — observed_at is
// stamped server-side after the client clock snapshot; 5s covers the measured
// 0.25s lead plus a slow-probe margin.
const FUTURE_SKEW_MS = 5_000;

/** Selected-agent observation; the roster's SSE refresh does not track host probes. */
export function AgentAvailability({ agent }: { agent: AgentRow }) {
  const t = useTranslations("agentAvailability");
  const queryClient = useQueryClient();
  const [retryPending, setRetryPending] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    // KEEP (task #3696 exception inventory): expiry re-render cadence — the gate
    // reads the live clock; the tick only bounds how long a stale observation can
    // outlive its window.
    const timer = setInterval(() => setNow(Date.now()), 15_000);
    return () => clearInterval(timer);
  }, []);
  const { data } = useQuery({
    queryKey: ["agent-availability", agent.agent_id],
    queryFn: ({ signal }) => api.getAgent(agent.agent_id, signal),
    // KEEP (task #3696 exception inventory): detail refresh cadence — host-probe
    // changes emit no lifecycle events, so this poll is the strip's only
    // freshness source.
    refetchInterval: 15_000,
  });
  const current = data ?? agent;
  const availability = current.availability;
  const observedAt = Date.parse(availability?.observed_at ?? "");
  // KEEP (task #3696 exception inventory): freshness window — with the refresh
  // stopped, an older observation no longer stands for a current verdict; two
  // minutes matches the read model's probe freshness span (PROBE_FRESH_FOR,
  // shared/agent_observation.py).
  const fresh = Number.isFinite(observedAt)
    && observedAt <= now + FUTURE_SKEW_MS && now - observedAt <= 120_000;
  const launchFailed = availability?.reason.startsWith("launch_") ?? false;
  const reason = fresh || launchFailed ? availability?.reason ?? "unknown" : "unknown";
  if (reason === "unknown") return null;
  const canRetry = current.status === "idling" && launchFailed;
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
        {(reason === "host_unavailable" || reason === "admission_refused") && (
          <Link href="/insights/status" className="shrink-0 underline underline-offset-2">
            {t("machineDiagnostics")}
          </Link>
        )}
      </div>
    </div>
  );
}
