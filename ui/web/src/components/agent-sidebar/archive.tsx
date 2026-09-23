"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { AgentRow as AgentRowItem } from "@/components/agent-row";
import { api } from "@/lib/api";
import { AGENT_DIRECTORY_QUERY_KEY } from "@/lib/fold/agents";
import { FLEX } from "@/lib/layout";
import { cn } from "@/lib/utils";
import type { InnerProps } from "./types";

/** Only the current archive page is retained. Search restarts at the latest page. */
export function AgentArchive({ props }: { props: InnerProps & { wide: boolean } }) {
  const t = useTranslations("sidebar");
  const [query, setQuery] = useState("");
  return <>
    <input aria-label={t("archiveSearch")} placeholder={t("archiveSearch")} value={query} onChange={(event) => setQuery(event.target.value)} className="m-2 w-[calc(100%-1rem)] rounded border border-border px-2 py-1 text-xs" />
    <ArchivePage key={query} query={query} props={props} />
  </>;
}

function ArchivePage({ query, props }: { query: string; props: InnerProps & { wide: boolean } }) {
  const t = useTranslations("sidebar");
  const [beforeId, setBeforeId] = useState<number | undefined>();
  const page = useQuery({
    queryKey: [...AGENT_DIRECTORY_QUERY_KEY, "terminated", query, beforeId],
    queryFn: ({ signal }) => api.listAgents({ scope: "terminated", query, beforeId, limit: 50, signal }),
    staleTime: Infinity,
    gcTime: 0,
  });
  const nextCursor = page.data?.next_cursor;
  return <section aria-label={t("archive")} className="border-t border-border py-2">
    <div className="px-3 text-xs text-muted-foreground">{t("archive")}</div>
    {page.isPending && <p className="px-3 text-xs">{t("loadingAgents")}</p>}
    {page.isError && <button onClick={() => void page.refetch()} className="px-3 text-xs text-destructive">{t("archiveRetry")}</button>}
    {page.isSuccess && page.data.agents.length === 0 && <p className="px-3 text-xs">{t("noResults")}</p>}
    <ul>{page.data?.agents.map((agent) => <AgentRowItem key={agent.agent_id} agent={agent}
      label={agent.label ?? undefined} active={props.activeId === agent.agent_id}
      pending={props.pendingActions[agent.agent_id]} wide={props.wide} depth={0} ancestorsIsLast={[]}
      onSelect={() => props.onSelect(agent.agent_id)} onTerminate={() => props.onTerminate(agent.agent_id)}
      onForceExpire={(sessionId) => props.onForceExpire(agent.agent_id, sessionId)}
      onForceKill={() => props.onTerminate(agent.agent_id, true)} onRestart={() => props.onRestart(agent.agent_id)}
      onResurrect={(prompt) => props.onResurrect(agent.agent_id, prompt)} onFork={(prompt) => props.onFork(agent.agent_id, prompt)}
      onCompact={() => props.onCompact(agent.agent_id)} onRename={(label) => props.onRename(agent.agent_id, label)}
    />)}</ul>
    <div className={cn(FLEX, "justify-between px-3 text-xs")}>
      {beforeId != null && <button onClick={() => setBeforeId(undefined)}>{t("archiveLatest")}</button>}
      {nextCursor != null && <button onClick={() => setBeforeId(nextCursor)}>{t("archiveOlder")}</button>}
    </div>
  </section>;
}
