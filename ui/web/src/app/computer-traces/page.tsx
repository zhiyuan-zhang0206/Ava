"use client";

import { useQuery } from "@tanstack/react-query";
import { Loader2, MonitorPlay, Search } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0 } from "@/lib/layout";

function fmt(ts: string | null | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString();
}

function outcomeBadge(outcome: string | undefined): string {
  if (outcome === "ok") return "bg-emerald-500/15 text-emerald-300";
  if (outcome === "error") return "bg-red-500/15 text-red-300";
  return "bg-amber-500/15 text-amber-300";
}

export default function ComputerTracesPage() {
  const [taskId, setTaskId] = useState("");
  const [submitted, setSubmitted] = useState<string | null>(null);

  const { data, error, isFetching } = useQuery({
    queryKey: ["computer-trace", submitted],
    queryFn: () => api.getComputerTrace(Number(submitted)),
    enabled: submitted !== null,
    retry: false,
  });

  return (
    <div className={`${FLEX_COL} h-full gap-4 p-6`}>
      <div className={`${FLEX} items-center gap-3`}>
        <MonitorPlay className="size-5 text-sky-400" />
        <h1 className="text-lg font-semibold">Computer-use task replay</h1>
      </div>
      <div className={`${FLEX} gap-2`}>
        <Input
          className="max-w-xs"
          placeholder="task id (e.g. 1101)"
          value={taskId}
          onChange={(e) => setTaskId(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && setSubmitted(taskId.trim())}
        />
        <Button
          variant="outline"
          size="sm"
          onClick={() => setSubmitted(taskId.trim())}
          disabled={!taskId.trim() || isFetching}
        >
          <Search className="mr-1 size-4" /> Query
        </Button>
      </div>

      {isFetching && <Loader2 className="size-5 animate-spin text-sky-400" />}
      {error && <p className="text-sm text-red-300">{error.message}</p>}

      {data && (
        <div className={`${FLEX_COL} ${FLEX_1} ${MIN_H_0} gap-3 overflow-y-auto`}>
          <div className="grid grid-cols-2 gap-2 rounded-lg border border-slate-800 bg-slate-900/50 p-3 text-sm md:grid-cols-4">
            <div>
              <div className="text-xs text-slate-400">task id</div>
              <div className="font-mono">{data.task_id}</div>
            </div>
            <div>
              <div className="text-xs text-slate-400">session start</div>
              <div>{data.start ? fmt(data.start.first_action_at) : "—"}</div>
            </div>
            <div>
              <div className="text-xs text-slate-400">session end</div>
              <div>
                {data.end
                  ? `${fmt(data.end.last_action_at)} · ${data.end.outcome}`
                  : "open"}
              </div>
            </div>
            <div>
              <div className="text-xs text-slate-400">actions</div>
              <div>{data.end?.action_count ?? data.actions.length}</div>
            </div>
          </div>

          {data.actions.length === 0 && (
            <p className="text-sm text-slate-400">No computer-use actions for this task.</p>
          )}

          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-800 text-left text-xs text-slate-400">
                <th className="py-1.5 pr-3">time</th>
                <th className="py-1.5 pr-3">action</th>
                <th className="py-1.5 pr-3">app</th>
                <th className="py-1.5 pr-3">result</th>
                <th className="py-1.5 pr-3">coords / args</th>
                <th className="py-1.5">snapshot</th>
              </tr>
            </thead>
            <tbody>
              {data.actions.map((a) => (
                <tr key={a.id} className="border-b border-slate-800/60">
                  <td className="py-1.5 pr-3 font-mono text-xs text-slate-400">
                    {fmt(a.ts)}
                  </td>
                  <td className="py-1.5 pr-3 font-mono">{a.action}</td>
                  <td className="py-1.5 pr-3">{a.app ?? "—"}</td>
                  <td className="py-1.5 pr-3">
                    <span className={`rounded px-1.5 py-0.5 text-xs ${outcomeBadge(a.outcome)}`}>
                      {a.outcome}
                    </span>
                  </td>
                  <td className="py-1.5 pr-3 font-mono text-xs">{a.coords ?? "—"}</td>
                  <td className="py-1.5 font-mono text-xs text-slate-500" title={a.path ?? undefined}>
                    {a.path ? a.path.split("/").pop() : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
