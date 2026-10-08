"use client";

// The run timeline's page header: which agent this is, named as the conversation's own header
// names it (number and label). The page is a review of what happened, so it shows nothing
// about the agent's present state.

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";

import { PageHeader } from "@/components/shell/page-header";
import { useAgentRoster } from "@/lib/agents/use-agents";
import { AGENT_DETAIL_QUERY_KEY } from "@/lib/fold/agents";
import { api } from "@/lib/transport/api";

export function RunTimelineHeader({ agentId }: { agentId: number | null }) {
  const t = useTranslations("runTimeline");
  const roster = useAgentRoster();
  const live = agentId === null ? undefined : roster.data?.agents.find((agent) => agent.agent_id === agentId);
  // An agent off the live roster (an old, terminated one) is read by id.
  const detail = useQuery({
    queryKey: [...AGENT_DETAIL_QUERY_KEY, agentId],
    queryFn: ({ signal }) => api.getAgent(agentId ?? 0, signal),
    enabled: agentId !== null && roster.isSuccess && live === undefined,
    staleTime: 30_000,
    retry: false,
  });
  const agent = live ?? detail.data;

  return (
    <PageHeader title={t("title")} backHref="/insights" backLabel={t("backToInsights")}>
      {agentId !== null ? (
        <span className="truncate font-mono" data-testid="run-timeline-agent">
          {`Agent #${agentId}${agent?.label ? ` · ${agent.label}` : ""}`}
        </span>
      ) : null}
    </PageHeader>
  );
}
