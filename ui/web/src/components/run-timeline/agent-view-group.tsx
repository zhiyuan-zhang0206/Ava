"use client";

// One agent's heading in the agent view (named as the conversation's own header names it: number and
// label) and what stands in for an agent whose timeline is not there yet. The view is a review of what
// happened, so it shows nothing about an agent's present state.

import { useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";
import { useTranslations } from "next-intl";

import { buttonVariants } from "@/components/ui/button";
import { useAgentRoster } from "@/lib/agents/use-agents";
import { AGENT_DETAIL_QUERY_KEY } from "@/lib/fold/agents";
import { api } from "@/lib/transport/api";
import { FLEX } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

import { RunTimelineChartSkeleton } from "./run-timeline-skeleton";

function useAgentLabel(agentId: number): string | undefined {
  const roster = useAgentRoster();
  const live = roster.data?.agents.find((agent) => agent.agent_id === agentId);
  // An agent off the live roster (an old, terminated one) is read by id.
  const detail = useQuery({
    queryKey: [...AGENT_DETAIL_QUERY_KEY, agentId],
    queryFn: ({ signal }) => api.getAgent(agentId, signal),
    enabled: roster.isSuccess && live === undefined,
    staleTime: 30_000,
    retry: false,
  });
  return (live ?? detail.data)?.label ?? undefined;
}

export function AgentGroupHeader({
  agentId,
  onRemove,
}: {
  agentId: number;
  /** Null while this is the only agent: the view never goes empty. */
  onRemove: ((agent: number) => void) | null;
}) {
  const t = useTranslations("runTimeline");
  const label = useAgentLabel(agentId);
  return (
    <div className={cn(FLEX, "items-center gap-2 pl-[88px] text-xs")}>
      <span className="truncate font-mono" data-testid="run-timeline-agent">
        {`Agent #${agentId}${label ? ` · ${label}` : ""}`}
      </span>
      {onRemove !== null ? (
        <button
          type="button"
          aria-label={t("removeAgent", { id: agentId })}
          title={t("removeAgent", { id: agentId })}
          data-testid={`agent-view-remove-${agentId}`}
          onClick={() => onRemove(agentId)}
          className={cn(buttonVariants({ size: "sm", variant: "ghost" }), "h-5 px-1")}
        >
          <X className="size-3.5" aria-hidden />
        </button>
      ) : null}
    </div>
  );
}

/** An agent still loading, or one whose timeline could not be read (retry or remove it). */
export function AgentPending({
  agentId,
  failed,
  onRetry,
  onRemove,
}: {
  agentId: number;
  failed: boolean;
  onRetry: (agent: number) => void;
  onRemove: ((agent: number) => void) | null;
}) {
  const t = useTranslations("runTimeline");
  return (
    <section data-testid={`agent-view-agent-${agentId}`} className="space-y-1.5">
      <AgentGroupHeader agentId={agentId} onRemove={onRemove} />
      {failed ? (
        <div className="space-y-2 pl-[88px] font-mono text-sm text-destructive" role="alert">
          <p>{t("loadFailedAgent", { id: agentId })}</p>
          <button type="button" className={buttonVariants({ size: "sm" })} onClick={() => onRetry(agentId)}>
            {t("retry")}
          </button>
        </div>
      ) : (
        <RunTimelineChartSkeleton />
      )}
    </section>
  );
}
