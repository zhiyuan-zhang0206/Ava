// Task Kanban — the list view paired with the Task Graph. Tasks are grouped
// into one full-width section per task status, stacked top to bottom
// (In Progress / Done / Canceled); each card is one row spanning the
// board. Read-only: cards cannot be moved between sections.
// Selection highlights sync with the graph view.

"use client";

import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useRef } from "react";

import { PRIORITY_BG, PRIORITY_RANK } from "@/lib/notices";
import type { TaskRow } from "@/lib/types";
import { cn } from "@/lib/utils";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0, OVERFLOW_HIDDEN } from "@/lib/layout";

const KANBAN_LANE_KEYS = ["inProgress", "done", "canceled"] as const;

export const STATUS_TO_LANE: Record<string, number> = {
  in_progress: 0,
  // Ongoing tasks are active long-running work, so they share In progress.
  ongoing: 0,
  done: 1,
  cancelled: 2,
};

export function TaskKanban({
  tasks,
  statusFill,
  statusLabel,
  selectedTaskId,
  onSelectTask,
  selectedAgentId,
  onSelectAgent,
}: {
  tasks: readonly TaskRow[];
  statusFill: Record<string, string>;
  statusLabel: Record<string, string>;
  selectedTaskId: number | null;
  onSelectTask: (id: number | null) => void;
  selectedAgentId: number | null;
  onSelectAgent: (id: number | null) => void;
}) {
  const t = useTranslations("fleet.task");
  const laneLabels = [t("status.inProgress"), t("status.done"), t("status.canceled")] as const;
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
      {KANBAN_LANE_KEYS.map((statusKey, lane) => {
        // Priority-first within the section (P0 on top); Array.sort is stable, so
        // same-priority cards keep the incoming created_at-desc order.
        const cards = tasks
          .filter((t) => (STATUS_TO_LANE[t.status] ?? 0) === lane && t.parent_id !== null)
          .sort((a, b) => PRIORITY_RANK[a.priority] - PRIORITY_RANK[b.priority]);
        if (cards.length === 0) return null;
        return (
          <div
            key={statusKey}
            className={cn("shrink-0 rounded-lg border border-border bg-background", FLEX, FLEX_COL)}
          >
            <div className={cn("shrink-0 items-center gap-2 border-b border-border px-3 py-2", FLEX)}>
              <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                {laneLabels[lane]}
              </span>
              <span className="rounded-full bg-muted px-1.5 font-mono text-2xs tabular-nums text-foreground">
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
  selected,
  onSelect,
  agentOwner,
  selectedAgentId,
  router,
}: {
  task: TaskRow;
  statusFill: Record<string, string>;
  statusLabel: Record<string, string>;
  selected: boolean;
  onSelect: () => void;
  agentOwner: number | null;
  selectedAgentId: number | null;
  router: ReturnType<typeof useRouter>;
}) {
  const t = useTranslations("fleet.task");
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
          "mt-px shrink-0 rounded px-1 font-mono text-2xs font-bold leading-tight text-white tabular-nums",
          PRIORITY_BG[task.priority],
        )}
      >
        {task.priority}
      </span>
      {/* Title + description take the full remaining row width. */}
      <div className={cn(MIN_W_0, FLEX_1)}>
        <div className="break-words text-xs font-semibold text-foreground">
          <span className="text-muted-foreground">#{task.id}</span> {task.title}
        </div>
        {task.description && (
          <div className="mt-1 whitespace-pre-wrap break-words text-2xs text-muted-foreground/70">
            {task.description}
          </div>
        )}
      </div>
      {/* Meta rail — status / owner, right-aligned so titles stay flush left
          across the whole section. */}
      <div className={cn("max-w-[40%] items-center gap-2 text-2xs text-muted-foreground", FLEX, MIN_W_0)}>
        <span>{statusLabel[task.status] ?? task.status}</span>
        {task.owner != null ? (
          <span className={cn("truncate", MIN_W_0)}>
            {task.owner_label ? (
              <>{task.owner_label} <span className="text-muted-foreground/60">#{task.owner}</span></>
            ) : (
              <>{t("agent", { id: task.owner })}</>
            )}
          </span>
        ) : (
          <span className="text-amber-500">{t("unowned")}</span>
        )}
      </div>
    </div>
  );
}
