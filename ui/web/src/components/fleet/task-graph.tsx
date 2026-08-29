// Task Graph — a free force-directed view of the task registry.
//
// Where the Graph View renders the weighted agent-relationship cloud, this view
// renders the task tree: parent_id chains become structural springs, and each
// node's fill colors by task status. The Graph mode is a free force-directed
// layout; the Kanban mode groups cards into columns by task status. Drag-drop
// between columns is removed.
//
// The graph mode deliberately REUSES the Agent Graph's canvas — the shared
// ForceGraph component (force-graph.tsx) renders both, with the ONLY visual
// difference being node shape: agents are circles, tasks are squares. Same
// physics, same ForceControls, same zoom/pan/focus interactions, same edge
// styling. Task-specific chrome (status filters, Kanban toggle) lives in this
// module's toolbar; task nodes are UNIFORM size (user ruling 2026-08-09
// #1070 — the old descendant-count size encoding read as a bug). A task whose
// parent is hidden by the status toggles keeps its parent as a dimmed ghost
// node so the tree never dangles a fake orphan (#1848).

"use client";

import { CheckCheck, CircleAlert, EyeOff } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, type ReactNode } from "react";

import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout";
import { PRIORITY_BG } from "@/lib/notices";
import { needsYouByTask, rollupNeedsYou } from "@/lib/task-notify";
import { formatRelative, formatUptime } from "@/lib/time";
import type { AgentRow, TaskRow } from "@/lib/types";
import { useTasks } from "@/lib/use-tasks";
import { useUserSettings } from "@/lib/use-user-settings";
import { cn } from "@/lib/utils";

import {
  FORCE_DEFAULTS,
  TASK_FORCE_GROUPS,
  useForceParams,
} from "./force-controls";
import {
  ForceGraph,
  type ForceGraphEdge,
  type ForceGraphNode,
} from "./force-graph";
import { TaskKanban } from "./task-kanban";

// Task status → color class for the node's fill (and the Kanban left strip).
// 'ongoing' is the system root task's permanent state (never assignable to a
// regular task): it gets a dedicated color so the root stands apart from every
// status-colored node in the graph — status colors have no meaning for it.
const STATUS_FILL: Record<string, string> = {
  // The old 'open' color — 'open' was dropped (tasks are born in_progress)
  // and the graph no longer separates the two shades (user ruling 2026-08-29).
  in_progress: "text-slate-400",
  done: "text-emerald-500",
  cancelled: "text-destructive",
  ongoing: "text-violet-500",
};

// Human-readable status label (Kanban columns; 'ongoing' shows in the legend
// as the root's own swatch).
const STATUS_LABEL: Record<string, string> = {
  in_progress: "In progress",
  done: "Done",
  cancelled: "Canceled",
  ongoing: "Root",
};

// ── Hover detail card ──
//
// The instant, cursor-following replacement for the old native SVG <title>
// (whose appearance the browser deferred ~0.5–1s — the perceived hover lag).
// Renders the task like a real detail page: every registry field the wire
// carries, grouped under labeled rows, with empty states for unset fields
// instead of a bare text dump. Rendered through the shared ForceGraph's
// hoverCard slot; the card itself is pointer-events-none, so it never steals
// the cursor from the canvas.

// `created_by` is an agent id as text, or 'system'/'user' for non-agent rows
// (schema CHECK) — spelled out for the card's "Created by" row.
function formatCreator(createdBy: string): string {
  if (createdBy === "user") return "User";
  if (createdBy === "system") return "System";
  return `Agent #${createdBy}`;
}

// Reminder cadence from the registry's seconds field: 7200 → "every 2h".
function formatRemindInterval(seconds: number | null | undefined): string {
  return seconds == null ? "—" : `every ${formatUptime(seconds)}`;
}

// One labeled meta row of the detail grid.
function MetaRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className={MIN_W_0}>
      <p className="text-[9px] font-medium uppercase tracking-wider text-muted-foreground/60">
        {label}
      </p>
      <p className="truncate text-[11px] text-popover-foreground">{children}</p>
    </div>
  );
}

function TaskHoverCard({
  task,
  ownerLabel,
  parentTitle,
}: {
  task: TaskRow;
  /** Resolved owner display ("label #id" / "Agent #id" / "Unowned"). */
  ownerLabel: string;
  /** Parent display ("title (#id)") or null when the task has no parent. */
  parentTitle: string | null;
}) {
  return (
    <div className="w-80 max-w-[80vw] rounded-lg border border-border bg-popover/95 p-3 shadow-xl backdrop-blur">
      {/* Header — status dot + title + id, priority badge on the right. */}
      <div className={cn(FLEX, "items-start gap-2")}>
        <span
          className={cn("mt-1 size-2 shrink-0 rounded-full bg-current", STATUS_FILL[task.status] ?? "text-slate-400")}
        />
        <div className={cn(MIN_W_0, FLEX_1)}>
          <p className="line-clamp-2 break-words text-xs font-semibold leading-snug text-popover-foreground">
            {task.title}
          </p>
          <p className="mt-0.5 text-[10px] tabular-nums text-muted-foreground">
            Task #{task.id}
          </p>
        </div>
        <span
          className={cn(
            "shrink-0 self-start rounded px-1 py-0.5 text-[9px] font-bold leading-none text-white",
            PRIORITY_BG[task.priority],
          )}
        >
          {task.priority}
        </span>
      </div>

      {/* Meta grid — the registry's key fields, labeled like a detail page. */}
      <div className="mt-2.5 grid grid-cols-2 gap-x-3 gap-y-1.5">
        <MetaRow label="Status">{STATUS_LABEL[task.status] ?? task.status}</MetaRow>
        <MetaRow label="Priority">{task.priority}</MetaRow>
        <MetaRow label="Owner">{ownerLabel}</MetaRow>
        <MetaRow label="Created by">{formatCreator(task.created_by)}</MetaRow>
        <MetaRow label="Parent">{parentTitle ?? "—"}</MetaRow>
        <MetaRow label="Reminder">{formatRemindInterval(task.remind_interval_seconds)}</MetaRow>
        <MetaRow label="Created">{formatRelative(task.created_at)}</MetaRow>
        <MetaRow label="Updated">{formatRelative(task.updated_at)}</MetaRow>
      </div>

      {/* Reminder extras — only when the task actually has a reminder history. */}
      {task.reminder_count > 0 || task.last_reminded_at != null ? (
        <p className="mt-1.5 text-[10px] text-muted-foreground/70">
          {task.reminder_count} reminder{task.reminder_count === 1 ? "" : "s"}
          {task.last_reminded_at != null ? ` · last ${formatRelative(task.last_reminded_at)}` : ""}
        </p>
      ) : null}

      {/* Long text — clamped so the card stays a hover preview; the full text
          lives in the task registry. */}
      {task.description ? (
        <div className="mt-2 border-t border-border pt-2">
          <p className="text-[9px] font-medium uppercase tracking-wider text-muted-foreground/60">
            Description
          </p>
          <p className="mt-0.5 line-clamp-4 whitespace-pre-wrap break-words text-[11px] leading-snug text-popover-foreground/90">
            {task.description}
          </p>
        </div>
      ) : null}
      {task.results ? (
        <div className="mt-2 border-t border-border pt-2">
          <p className="text-[9px] font-medium uppercase tracking-wider text-muted-foreground/60">
            Result
          </p>
          <p className="mt-0.5 line-clamp-4 whitespace-pre-wrap break-words text-[11px] leading-snug text-popover-foreground/90">
            {task.results}
          </p>
        </div>
      ) : null}

      <p className="mt-2 border-t border-border pt-1.5 text-[9px] text-muted-foreground/60">
        Double-click the node to open the owner&apos;s conversation
      </p>
    </div>
  );
}

// The task graph is an independent UI from the Agent Graph (user ruling
// 2026-08-10 #1127): it keeps its OWN tuning key, so adjusting one graph
// never changes the other. (2026-08-05 had both graphs share
// display.graph_force_params — "one tuning, applied to both canvases" — the
// 8/10 ruling splits them. The .v2 key was already wired in
// settings-migration.ts but never populated.) Task nodes also carry a single
// fixed size — TASK_FORCE_GROUPS shows one "Size" slider, no min/max band.
export const TASK_FORCE_KEY = "display.task_force_params.v2";

// Inline toggle button for task status filters (Done / Canceled).
function StatusToggleButton({
  active,
  onClick,
  label,
  hiddenCount,
  icon,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  hiddenCount: number;
  icon: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium",
        "border transition-colors select-none",
        active
          ? "border-border bg-accent text-accent-foreground"
          : "border-transparent text-muted-foreground/50 hover:text-muted-foreground hover:border-border",
      )}
    >
      {icon}
      <span>{label}</span>
      {!active && hiddenCount > 0 && (
        <span className="rounded-full bg-muted px-1 text-[9px] tabular-nums text-muted-foreground">
          {hiddenCount}
        </span>
      )}
    </button>
  );
}

// Stale-while-error marker for the board toolbar. Shown when a poll failed but
// the last-loaded tasks are still on screen — a lightweight flag, never a blank
// board (the full "Failed to load tasks." screen is reserved for a cold failure).
function StaleBadge({ show }: { show: boolean }) {
  if (!show) return null;
  return (
    <span
      className="inline-flex items-center gap-1 text-[10px] text-amber-600 dark:text-amber-400"
    >
      <span className="size-1.5 rounded-full bg-amber-500" />
      Stale
    </span>
  );
}

// The "Needs you" filter toggle. Inactive shows the board-wide pending
// needs-you total (the aggregate count — allowed here: the board is an
// explicitly opened pull surface, not an ambient one); active narrows the
// board to subtrees waiting on the user.
function NeedsYouToggle({
  active,
  onClick,
  total,
}: {
  active: boolean;
  onClick: () => void;
  total: number;
}) {
  return (
    <StatusToggleButton
      active={active}
      onClick={onClick}
      label="Needs you"
      hiddenCount={total}
      icon={<CircleAlert className="size-3" />}
    />
  );
}

export function TaskGraph({
  agents,
  selectedTaskId,
  onSelectTask,
  selectedAgentId,
  onSelectAgent,
}: {
  agents: readonly AgentRow[];
  selectedTaskId: number | null;
  onSelectTask: (id: number | null) => void;
  selectedAgentId: number | null;
  onSelectAgent: (id: number | null) => void;
}) {
  const router = useRouter();
  const { tasks, loading, error } = useTasks();
  const { params, setParams, reset } = useForceParams(TASK_FORCE_KEY, FORCE_DEFAULTS);

  // Mode + filters are DB-backed user settings (display.task_*), so they follow
  // the user across frontends and stay in one source of truth.
  const { settings, setSetting } = useUserSettings();
  const mode: "graph" | "kanban" =
    settings["display.task_graph_mode"] === "kanban" ? "kanban" : "graph";
  const setMode = (m: "graph" | "kanban") => setSetting("display.task_graph_mode", m);
  const showDone = settings["display.task_show_done"] === true;
  const setShowDone = (v: boolean) => setSetting("display.task_show_done", v);
  const showCanceled = settings["display.task_show_canceled"] === true;
  const setShowCanceled = (v: boolean) => setSetting("display.task_show_canceled", v);
  const needsOnly = settings["display.task_needs_you"] === true;
  const setNeedsOnly = (v: boolean) => setSetting("display.task_needs_you", v);

  // Needs-you join (task-notify.ts): per-task pending require_response load
  // from the owning agents, plus the subtree rollup that keeps a manager task
  // visible while any descendant waits on the user.
  const needsYou = useMemo(() => needsYouByTask(tasks, agents), [tasks, agents]);
  const subtreeNeeds = useMemo(() => rollupNeedsYou(tasks, needsYou), [tasks, needsYou]);
  const needsYouTotal = useMemo(() => {
    let n = 0;
    for (const ny of needsYou.values()) n += ny.count;
    return n;
  }, [needsYou]);

  // Filter tasks based on toggles.
  const filteredTasks = useMemo(
    () =>
      tasks.filter(
        (t) =>
          (showDone || t.status !== "done") &&
          (showCanceled || t.status !== "cancelled") &&
          (!needsOnly || (subtreeNeeds.get(t.id) ?? 0) > 0),
      ),
    [tasks, showDone, showCanceled, needsOnly, subtreeNeeds],
  );
  const hiddenDoneCount = useMemo(() => tasks.filter((t) => t.status === "done").length, [tasks]);
  const hiddenCanceledCount = useMemo(() => tasks.filter((t) => t.status === "cancelled").length, [tasks]);

  // Id → task lookup for the hover card's parent/owner resolution.
  const taskById = useMemo(() => new Map(tasks.map((t) => [t.id, t])), [tasks]);

  // Instant hover detail card (see TaskHoverCard) — the shared canvas shows it
  // the moment the cursor enters a node, replacing the delayed native <title>.
  const taskHoverCard = useCallback(
    (node: ForceGraphNode) => {
      const task = taskById.get(node.id);
      if (!task) return null;
      const owner = task.owner;
      const ownerLabel =
        owner != null
          ? task.owner_label != null
            ? `${task.owner_label} #${owner}`
            : `Agent #${owner}`
          : "Unowned";
      const parent = task.parent_id != null ? taskById.get(task.parent_id) : null;
      const parentTitle =
        parent != null
          ? `${parent.title} (#${parent.id})`
          : task.parent_id != null
            ? `#${task.parent_id}`
            : null;
      return <TaskHoverCard task={task} ownerLabel={ownerLabel} parentTitle={parentTitle} />;
    },
    [taskById],
  );

  // Everything except the system root — match the kanban view. Post
  // root-anchoring the root is the sole parent-less row (every other task
  // descends from it), so `parent_id !== null` selects all real tasks and hides
  // only the root itself.
  const subtasks = useMemo(
    () => filteredTasks.filter((t) => t.parent_id !== null),
    [filteredTasks],
  );

  // Structural ghost parents: a visible task whose parent was filtered out by
  // the status toggles must not dangle as a fake orphan (#1848: parent done
  // while Done is toggled off). Hidden ancestors of visible tasks join the
  // graph as dimmed ghost nodes so the tree stays connected — graph-only
  // (the kanban is a flat list with no parent edges).
  const ghostTasks = useMemo(() => {
    if (subtasks.length === 0) return [] as TaskRow[];
    const visible = new Set(subtasks.map((t) => t.id));
    const byId = new Map(tasks.map((t) => [t.id, t]));
    const ghosts = new Map<number, TaskRow>();
    for (const t of subtasks) {
      let cur = t.parent_id == null ? undefined : byId.get(t.parent_id);
      // Walk up while the parent exists, is hidden, and is not the root (the
      // root is always rendered and parent-less) — adding each hidden
      // ancestor once.
      while (
        cur?.parent_id != null &&
        !visible.has(cur.id) &&
        !ghosts.has(cur.id)
      ) {
        ghosts.set(cur.id, cur);
        cur = byId.get(cur.parent_id);
      }
    }
    return [...ghosts.values()];
  }, [subtasks, tasks]);

  const ghostIds = useMemo(() => new Set(ghostTasks.map((t) => t.id)), [ghostTasks]);

  // Graph node/edge set: visible subtasks PLUS their structural ghost parents
  // PLUS every parent-less task (the system root) — top-level tasks hang
  // under the root node in the graph (user ruling 2026-08-06: show the root).
  // The root is a structural anchor: it joins the graph even when its own
  // status would be hidden by the toggles (the kanban keeps hiding it — this
  // set is graph-only).
  const graphTasks = useMemo(
    () => [...subtasks, ...ghostTasks, ...tasks.filter((t) => t.parent_id == null)],
    [subtasks, ghostTasks, tasks],
  );

  // Bidirectional sync: when an agent is selected externally (from the
  // notification queue, graph, or review panel), auto-select the first task
  // owned by that agent. When user manually selects a task, also propagate
  // its owner as the selected agent so the graph + notification panels sync.
  useEffect(() => {
    if (selectedAgentId == null) return;
    // If the currently selected task already belongs to this agent, no change.
    const cur = tasks.find((t) => t.id === selectedTaskId);
    if (cur?.owner === selectedAgentId) return;
    const first = tasks.find((t) => t.owner === selectedAgentId);
    if (first) onSelectTask(first.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only react to selectedAgentId changes
  }, [selectedAgentId]);

  // When user clicks a task, sync its owner as the selected agent (bidirectional).
  const handleSelectTask = useCallback((taskId: number | null) => {
    onSelectTask(taskId);
    if (taskId != null) {
      const task = tasks.find((t) => t.id === taskId);
      if (task?.owner != null) onSelectAgent(task.owner);
    }
  }, [tasks, onSelectTask, onSelectAgent]);

  // Double-click a task → the owner's conversation (the command panel's
  // drill-down path — same interaction as the Agent Graph's double-click).
  const openTaskTimeline = useCallback((taskId: number) => {
    const task = tasks.find((t) => t.id === taskId);
    if (task?.owner != null) router.push(`/?agent_id=${task.owner}`);
  }, [tasks, router]);

  // Adapt the task list to the shared node/edge model (square nodes).
  const graphNodes = useMemo<ForceGraphNode[]>(
    () =>
      graphTasks.map((t) => ({
        id: t.id,
        label: t.title,
        status: t.status,
        // Uniform node size (user ruling 2026-08-09 #1070): every task
        // node sits at the minimum radius — subtree size no longer drives
        // the square. The Agent Graph keeps its score-driven sizing.
        score: 0,
        // Hidden-by-toggle structural parents render dimmed (see ghostTasks).
        ghost: ghostIds.has(t.id),
      })),
    [graphTasks, ghostIds],
  );
  // Parent → child edges; the shared layout drops edges whose endpoints are
  // not both visible (a hidden parent leaves its children as isolates).
  const graphEdges = useMemo<ForceGraphEdge[]>(() => {
    const edges: ForceGraphEdge[] = [];
    for (const t of graphTasks) {
      if (t.parent_id != null) {
        edges.push({ from: t.parent_id, to: t.id, kind: "lineage", weight: 1 });
      }
    }
    return edges;
  }, [graphTasks]);

  if (loading) {
    return (
      <div className={cn("h-full items-center justify-center text-xs text-muted-foreground", FLEX)}>
        Loading tasks…
      </div>
    );
  }
  // Cold failure only — with tasks already loaded, a failed poll keeps the board
  // (stale-while-error); the StaleBadge in the toolbar flags it instead of blanking.
  if (error && tasks.length === 0) {
    return (
      <div className={cn("h-full items-center justify-center text-xs text-destructive", FLEX)}>
        Failed to load tasks.
      </div>
    );
  }
  if (filteredTasks.length === 0) {
    // With the needs-you filter on, an empty board is the healthy steady state
    // — keep the FULL filter toolbar reachable: the needs-you toggle to turn
    // the filter back off, and the Done/Canceled toggles because a pending
    // decision can sit on a status-hidden task (needsYouTotal > 0 here means
    // exactly that — every needy task was excluded by the status filters).
    if (needsOnly) {
      return (
        <div className={cn("h-full", FLEX, FLEX_COL, MIN_H_0)}>
          <div className={cn("shrink-0 flex-wrap items-center gap-1 border-b border-border px-2 py-1.5", FLEX)}>
            <div className={cn("ml-auto items-center gap-1", FLEX)}>
              <NeedsYouToggle active onClick={() => setNeedsOnly(false)} total={needsYouTotal} />
              <StatusToggleButton
                active={showDone}
                onClick={() => setShowDone(!showDone)}
                label="Done"
                hiddenCount={showDone ? 0 : hiddenDoneCount}
                icon={<CheckCheck className="size-3" />}
              />
              <StatusToggleButton
                active={showCanceled}
                onClick={() => setShowCanceled(!showCanceled)}
                label="Canceled"
                hiddenCount={showCanceled ? 0 : hiddenCanceledCount}
                icon={<EyeOff className="size-3" />}
              />
            </div>
          </div>
          <div className={cn("items-center justify-center px-4 text-center text-xs text-muted-foreground", FLEX, FLEX_1)}>
            {needsYouTotal > 0
              ? "Pending decisions sit on Done/Canceled tasks — use the toggles above to reveal them."
              : "Nothing needs you right now."}
          </div>
        </div>
      );
    }
    return (
      <div className={cn("h-full items-center justify-center text-xs text-muted-foreground", FLEX)}>
        No tasks yet.
      </div>
    );
  }

  if (mode === "kanban") {
    return (
      <div className={cn("h-full", FLEX, FLEX_COL, MIN_H_0)}>
        <div className={cn("shrink-0 flex-wrap items-center gap-1 border-b border-border px-2 py-1.5", FLEX)}>
          <div className={cn("gap-1", FLEX)}>
            <button
              type="button"
              onClick={() => setMode("graph")}
              className="rounded px-2 py-0.5 text-[11px] text-muted-foreground hover:text-foreground hover:bg-sidebar-accent"
            >
              Graph
            </button>
            <span className="rounded bg-sidebar-accent px-2 py-0.5 text-[11px] font-medium text-foreground">
              Kanban
            </span>
          </div>
          <span className="text-xs text-muted-foreground">
            {subtasks.length} task{subtasks.length !== 1 ? "s" : ""}
          </span>
          <StaleBadge show={error} />
          <div className={cn("ml-auto items-center gap-1", FLEX)}>
            <NeedsYouToggle
              active={needsOnly}
              onClick={() => setNeedsOnly(!needsOnly)}
              total={needsYouTotal}
            />
            <StatusToggleButton
              active={showDone}
              onClick={() => setShowDone(!showDone)}
              label="Done"
              hiddenCount={showDone ? 0 : hiddenDoneCount}
              icon={<CheckCheck className="size-3" />}
            />
            <StatusToggleButton
              active={showCanceled}
              onClick={() => setShowCanceled(!showCanceled)}
              label="Canceled"
              hiddenCount={showCanceled ? 0 : hiddenCanceledCount}
              icon={<EyeOff className="size-3" />}
            />
          </div>
        </div>
        <TaskKanban
          tasks={filteredTasks}
          statusFill={STATUS_FILL}
          statusLabel={STATUS_LABEL}
          selectedTaskId={selectedTaskId}
          onSelectTask={handleSelectTask}
          selectedAgentId={selectedAgentId}
          onSelectAgent={onSelectAgent}
        />
      </div>
    );
  }

  return (
    <div className={cn("h-full", FLEX, FLEX_COL, MIN_H_0)}>
      <div className={cn("shrink-0 flex-wrap items-center gap-1 border-b border-border px-2 py-1.5", FLEX)}>
        <div className={cn("gap-1", FLEX)}>
          <span className="rounded bg-sidebar-accent px-2 py-0.5 text-[11px] font-medium text-foreground">
            Graph
          </span>
          <button
            type="button"
            onClick={() => setMode("kanban")}
            className="rounded px-2 py-0.5 text-[11px] text-muted-foreground hover:text-foreground hover:bg-sidebar-accent"
          >
            Kanban
          </button>
        </div>
        <span className="text-xs text-muted-foreground">
          {subtasks.length} task{subtasks.length !== 1 ? "s" : ""}
        </span>
        <StaleBadge show={error} />
        <div className={cn("ml-auto items-center gap-1", FLEX)}>
          <NeedsYouToggle
            active={needsOnly}
            onClick={() => setNeedsOnly(!needsOnly)}
            total={needsYouTotal}
          />
          <StatusToggleButton
            active={showDone}
            onClick={() => setShowDone(!showDone)}
            label="Done"
            hiddenCount={showDone ? 0 : hiddenDoneCount}
            icon={<CheckCheck className="size-3" />}
          />
          <StatusToggleButton
            active={showCanceled}
            onClick={() => setShowCanceled(!showCanceled)}
            label="Canceled"
            hiddenCount={showCanceled ? 0 : hiddenCanceledCount}
            icon={<EyeOff className="size-3" />}
          />
        </div>
      </div>
      <div className={cn("relative", FLEX_1, MIN_H_0)}>
        <ForceGraph
          nodes={graphNodes}
          edges={graphEdges}
          shape="square"
          statusText={STATUS_FILL}
          selectedId={selectedTaskId}
          onSelect={handleSelectTask}
          onOpen={openTaskTimeline}
          params={params}
          setParams={setParams}
          resetParams={reset}
          groups={TASK_FORCE_GROUPS}
          hoverCard={taskHoverCard}
          legend={
            <div aria-label="Task graph legend" className="space-y-1">
              <div className="grid grid-cols-2 gap-x-3 gap-y-0.5">
                {Object.entries(STATUS_LABEL).map(([status, label]) => (
                  <span key={status} className={cn("items-center gap-1.5", FLEX)}>
                    <span className={cn("size-2 rounded-full bg-current", STATUS_FILL[status])} />
                    {label}
                  </span>
                ))}
              </div>
            </div>
          }
        />
      </div>
    </div>
  );
}
