"use client";

// The fleet page's left pane: a tab switcher over the two supervision
// surfaces — the weighted relationship Graph (default) and the Task Graph.
// The view choice persists (display.fleet_left_view) so a refresh returns to
// it; a route-opened task (task #2909) temporarily forces the Tasks view —
// the jump must land on the task — until the user picks a tab themselves,
// without rewriting the stored preference.

import { memo, useState } from "react";
import { useTranslations } from "next-intl";

import { GraphView } from "@/components/fleet/graph-view";
import { TaskGraph } from "@/components/fleet/task-graph";
import { BAR_HEIGHT_CLASS, FLEX, FLEX_1, FLEX_COL, MIN_H_0 } from "@/lib/layout";
import { useUserSettings } from "@/lib/use-user-settings";
import { cn } from "@/lib/utils";

type LeftView = "graph" | "tasks";

export const LeftGraphPanel = memo(function LeftGraphPanel({
  selectedAgentId,
  setSelectedAgentId,
  selectedTaskId,
  setSelectedTaskId,
  routeTasksView,
}: {
  selectedAgentId: number | null;
  setSelectedAgentId: (id: number | null) => void;
  selectedTaskId: number | null;
  setSelectedTaskId: (id: number | null) => void;
  routeTasksView?: boolean;
}) {
  const t = useTranslations("fleet");
  const { settings, setSetting } = useUserSettings();
  const [userPickedView, setUserPickedView] = useState(false);
  const durableView: LeftView = settings["display.fleet_left_view"] === "tasks" ? "tasks" : "graph";
  const view: LeftView = !userPickedView && routeTasksView ? "tasks" : durableView;
  const setView = (v: LeftView) => {
    setUserPickedView(true);
    setSetting("display.fleet_left_view", v);
  };

  return (
    <div className={cn("h-full", FLEX, FLEX_COL, MIN_H_0)}>
      {/* BAR_HEIGHT_CLASS — the same fixed height as the Inbox's QueueHeader,
          so the two side-by-side bars (and their bottom borders) line up
          exactly. Padding alone cannot: the two bars hold different content
          heights. */}
      <div className={cn("shrink-0 items-center gap-1 border-b border-border px-2", BAR_HEIGHT_CLASS, FLEX)}>
        <TabButton label={t("agents")} count={0} active={view === "graph"} onClick={() => setView("graph")} />
        <TabButton
          label={t("tasks")}
          count={0}
          active={view === "tasks"}
          onClick={() => setView("tasks")}
        />
      </div>
      <div className={cn("relative", FLEX_1, MIN_H_0)}>
        {/* One graph mounted at a time (R4 layer 4: breakpoint + conditional
            render — the dual always-mounted display:none pattern is deleted). */}
        {view === "graph" ? (
          <div className="absolute inset-0">
            <GraphView selectedAgentId={selectedAgentId} onSelectAgent={setSelectedAgentId} />
          </div>
        ) : (
          <div className="absolute inset-0">
            <TaskGraph
              selectedTaskId={selectedTaskId}
              onSelectTask={setSelectedTaskId}
              selectedAgentId={selectedAgentId}
              onSelectAgent={setSelectedAgentId}
            />
          </div>
        )}
      </div>
    </div>
  );
});

function TabButton({
  label,
  count,
  active,
  onClick,
}: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "items-center gap-1.5 rounded px-3 py-1 text-xs font-medium",
        active ? "bg-sidebar-accent text-foreground" : "text-muted-foreground hover:text-foreground",
        FLEX
      )}
    >
      {label}
      {count > 0 && (
        <span className="rounded-full bg-muted px-1.5 text-[10px] tabular-nums text-foreground">
          {count}
        </span>
      )}
    </button>
  );
}
