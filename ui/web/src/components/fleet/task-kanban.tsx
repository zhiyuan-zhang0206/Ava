// Task Kanban — the list view paired with the Task Graph. Tasks are grouped
// into one full-width section per task status, stacked top to bottom
// (Open / In Progress / Done / Canceled); each card is one row spanning the
// board. Read-only: cards cannot be moved between sections.
// Selection highlights sync with the graph view.

"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef } from "react";

import { PRIORITY_BG, PRIORITY_RANK } from "@/lib/notices";
import type { TaskNeedsYou } from "@/lib/task-notify";
import type { TaskRow } from "@/lib/types";
import { cn } from "@/lib/utils";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0, OVERFLOW_HIDDEN } from "@/lib/layout";

export const KANBAN_LANES = ["Open", "In progress", "Done", "Canceled"] as const;

export const STATUS_TO_LANE: Record<string, number> = {
  open: 0,
  in_progress: 1,
  done: 2,
  cancelled: 3,
};

export function TaskKanban({
  tasks,
  statusFill,
  statusLabel,
  needsYou,
  selectedTaskId,
  onSelectTask,
  selectedAgentId,
  onSelectAgent,
}: {
  tasks: readonly TaskRow[];
  statusFill: Record<string, string>;
  statusLabel: Record<string, string>;
  needsYou: ReadonlyMap<number, TaskNeedsYou>;
  selectedTaskId: number | null;
  onSelectTask: (id: number | null) => void;
  selectedAgentId: number | null;
  onSelectAgent: (id: number | null) => void;
}) {
  const router = useRouter();
  // Track last agent-sync trigger to avoid overriding manual clicks.
  const lastSyncedAgentRef = useRef<number | null>(null);

  // Bidirectional sync: when an agent is selected externally, auto-select the
  // first task owned by that agent.  Does nothing if there is no matching task
  // or if the currently-selected task already belongs to this agent.
  useEffect(() => {
    if (selectedAgentId == null) return;
    if (lastSyncedAgentRef.current === selectedAgentId) return;
    const cur = tasks.find((t) => t.id === selectedTaskId);
    if (cur?.owner === selectedAgentId) { lastSyncedAgentRef.current = selectedAgentId; return; }
    const first = tasks.find((t) => t.owner === selectedAgentId && t.parent_id !== null);
    if (first) {
      lastSyncedAgentRef.current = selectedAgentId;
      onSelectTask(first.id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only react to selectedAgentId changes
  }, [selectedAgentId]);

  // When user clicks a task card, sync the agent selection.
  const handleSelectTask = (taskId: number | null) => {
    onSelectTask(taskId);
    if (taskId != null) {
      const task = tasks.find((t) => t.id === taskId);
      if (task?.owner != null) {
        lastSyncedAgentRef.current = task.owner;
        onSelectAgent(task.owner);
      }
    }
  }
  return (
    // Sections stack vertically and the whole board is the single scrollport;
    // each section is shrink-0 so a long list scrolls instead of squashing.
    <div className={cn("h-full gap-3 overflow-y-auto p-3", FLEX, MIN_H_0, FLEX_COL)}>
      {KANBAN_LANES.map((label, lane) => {
        // Priority-first within the section (P0 on top); Array.sort is stable, so
        // same-priority cards keep the incoming created_at-desc order.
        const cards = tasks
          .filter((t) => (STATUS_TO_LANE[t.status] ?? 0) === lane && t.parent_id !== null)
          .sort((a, b) => PRIORITY_RANK[a.priority] - PRIORITY_RANK[b.priority]);
        if (cards.length === 0) return null;
        return (
          <div
            key={label}
            className={cn("shrink-0 rounded-lg border border-border bg-background", FLEX, FLEX_COL)}
          >
            <div className={cn("shrink-0 items-center gap-2 border-b border-border px-3 py-2", FLEX)}>
              <span className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                {label}
              </span>
              <span className="rounded-full bg-muted px-1.5 text-[10px] tabular-nums text-foreground">
                {cards.length}
              </span>
            </div>
            <div className={cn("gap-2 p-2", FLEX, FLEX_COL)}>
              {cards.map((task) => (
                <KanbanCard
                  key={task.id}
                  task={task}
                  statusFill={statusFill}
                  statusLabel={statusLabel}
                  needsYou={needsYou.get(task.id)}
                  selected={selectedTaskId === task.id}
                  onSelect={() =>
                    handleSelectTask(selectedTaskId === task.id ? null : task.id)
                  }
                  agentOwner={task.owner}
                  selectedAgentId={selectedAgentId}
                  router={router}
                />
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function KanbanCard({
  task,
  statusFill,
  statusLabel,
  needsYou,
  selected,
  onSelect,
  agentOwner,
  selectedAgentId,
  router,
}: {
  task: TaskRow;
  statusFill: Record<string, string>;
  statusLabel: Record<string, string>;
  needsYou: TaskNeedsYou | undefined;
  selected: boolean;
  onSelect: () => void;
  agentOwner: number | null;
  selectedAgentId: number | null;
  router: ReturnType<typeof useRouter>;
}) {
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onSelect}
      onDoubleClick={(ev) => {
        ev.preventDefault();
        if (agentOwner != null) router.push(`/?agent_id=${agentOwner}`);
      }}
      onKeyDown={(ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          onSelect();
        }
      }}
      className={cn(
        "relative w-full shrink-0 items-start gap-3 rounded-md border bg-card py-2 pl-4 pr-3 text-left",
        selected
          ? "border-sky-400"
          : selectedAgentId != null && agentOwner === selectedAgentId
            ? "border-sky-400/40"
            : "border-border hover:border-muted-foreground/50",
            FLEX, OVERFLOW_HIDDEN
      )}
    >
      {/* Status-colored left strip. */}
      <span
        className={cn("absolute inset-y-0 left-0 w-1", statusFill[task.status])}
        style={{ backgroundColor: "currentColor" }}
      />
      {/* Priority badge — the task's own stakes rung (P0..P3), the section sort
          key; leads the row so the ordering is readable down the left edge. */}
      <span
        className={cn(
          "mt-px shrink-0 rounded px-1 text-[9px] font-bold leading-tight text-white",
          PRIORITY_BG[task.priority],
        )}
      >
        {task.priority}
      </span>
      {/* Title + description take the full remaining width of the row. */}
      <div className={cn(MIN_W_0, FLEX_1)}>
        <div className="break-words text-xs font-semibold text-foreground">
          <span className="text-muted-foreground">#{task.id}</span> {task.title}
        </div>
        {task.description && (
          <div className="mt-1 whitespace-pre-wrap break-words text-[10px] text-muted-foreground/70">
            {task.description}
          </div>
        )}
      </div>
      {/* Meta rail — status / owner / needs-you, right-aligned so titles stay
          flush left across the whole section. */}
      <div className={cn("max-w-[40%] items-center gap-2 text-[10px] text-muted-foreground", FLEX, MIN_W_0)}>
        <span>{statusLabel[task.status] ?? task.status}</span>
        {task.owner != null ? (
          <span className={cn("truncate", MIN_W_0)}>
            {task.owner_label ? (
              <>{task.owner_label} <span className="text-muted-foreground/60">#{task.owner}</span></>
            ) : (
              <>Agent #{task.owner}</>
            )}
          </span>
        ) : (
          <span className="text-amber-500">Unowned</span>
        )}
        {/* Needs-you badge — pending require_response notices on this task's
            owner agents, colored by top priority. */}
        {needsYou && (
          <span
            className={cn(
              "rounded-full px-1.5 text-[9px] font-semibold tabular-nums text-white",
              PRIORITY_BG[needsYou.top],
            )}
            aria-label={`${needsYou.count} waiting on you`}
          >
            {needsYou.count}
          </span>
        )}
      </div>
    </div>
  );
}
