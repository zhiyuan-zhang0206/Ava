"use client";

// Run timeline entry — the tracing-related main entry for the run-level
// timeline (user ruling: main entry at the tracing position; the agent
// detail page gets a quick link). Picks an agent and opens
// /insights/run/{agentId}.

import { useQuery } from "@tanstack/react-query";
import { ArrowUpRight } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { api } from "@/lib/api";
import { FLEX } from "@/lib/layout";
import { cn } from "@/lib/utils";

export default function RunTimelineEntry() {
  const { data: agents } = useQuery({
    queryKey: ["agents"] as const,
    queryFn: () => api.listAgents(),
    staleTime: 30_000,
  });
  const live = useMemo(
    () => (agents ?? []).filter((a) => a.status !== "terminated"),
    [agents],
  );
  const [selected, setSelected] = useState<number | null>(null);

  return (
    <div className="space-y-2">
      <p className="text-xs text-muted-foreground">
        Open one agent&apos;s run timeline — the complete session from context
        initialization to the latest compact, as independent time and token
        axes with dashed connectors between the corresponding turns.
      </p>
      <div className={cn("items-center gap-2", FLEX)}>
        <select
          value={selected ?? ""}
          onChange={(e) => setSelected(e.target.value ? Number(e.target.value) : null)}
          aria-label="Agent"
          className="rounded border border-border bg-transparent px-2 py-1 font-mono text-xs text-muted-foreground hover:text-foreground focus:outline-none"
        >
          <option value="">Select an agent…</option>
          {(live.length > 0 ? live : agents ?? []).map((a) => (
            <option key={a.agent_id} value={a.agent_id}>
              #{a.agent_id} {a.label ?? ""}
            </option>
          ))}
        </select>
        <Link
          href={selected != null ? `/insights/run/${selected}` : "/insights"}
          aria-disabled={selected == null}
          className={cn(
            "inline-flex items-center gap-1 rounded border border-border px-2 py-1 font-mono text-xs",
            selected != null
              ? "text-foreground hover:bg-sidebar-accent"
              : "pointer-events-none text-muted-foreground/50",
          )}
        >
          Open run timeline
          <ArrowUpRight className="size-3" aria-hidden />
        </Link>
      </div>
    </div>
  );
}
