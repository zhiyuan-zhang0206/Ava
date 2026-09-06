"use client";

import {
  ArrowUpDown,
  Circle,
  CircleDot,
  Eye,
  EyeOff,
  ListTree,
  Loader2,
} from "lucide-react";
import * as Popover from "@radix-ui/react-popover";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { AgentRow as AgentRowItem } from "@/components/agent-row";
import { SpawnButton } from "@/components/spawn-button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { buildAgentTree, type AgentNode } from "@/lib/agent-tree";
import { SORT_DEFAULT_DIR, useSidebarSort, type FlatSortKey, type SidebarSort } from "@/lib/sidebar";
import { useStore } from "@/lib/store";
import type { AgentRow } from "@/lib/types";
import { useUserSettings } from "@/lib/use-user-settings";
import { cn } from "@/lib/utils";

import type { InnerProps } from "./types";
import { fleetHref } from "./links";
import { FLEX, FLEX_1, MIN_H_0, MIN_W_0 } from "@/lib/layout";

// ── Expanded sidebar body ──

// Ascending comparator per sort key; direction is applied by sortAgentsFlat.
function compareAsc(a: AgentRow, b: AgentRow, key: FlatSortKey): number {
  switch (key) {
    case "id":
      return a.agent_id - b.agent_id;
    case "last_active": {
      const ta = a.last_active_at ? new Date(a.last_active_at).getTime() : 0;
      const tb = b.last_active_at ? new Date(b.last_active_at).getTime() : 0;
      return ta - tb;
    }
    case "status":
      return a.status.localeCompare(b.status);
  }
}

// Sort agents for flat list mode (key + direction).
function sortAgentsFlat(agents: AgentRow[], sort: SidebarSort): AgentRow[] {
  const sign = sort.dir === "asc" ? 1 : -1;
  return [...agents].sort((a, b) => sign * compareAsc(a, b, sort.key));
}


export function SidebarBody(props: InnerProps & { wide: boolean }) {
  const t = useTranslations("sidebar");
  const {
    agents,
    activeId,
    pendingSpawnCount,
    showTerminated,
    onToggleTerminated,
    viewMode,
    onToggleViewMode,
    onSpawn,
    isLoading,
  } = props;

  // searchQuery (store) drives the list filter below; the search INPUT itself
  // lives in the floating SearchOverlay (#723), not inline in the sidebar.
  const searchQuery = useStore((s) => s.searchQuery);
  const { sort, setSort } = useSidebarSort();
  // Quiet defaults (RCS): both are opt-in (`=== true`, default off).
  const { settings: userSettings, setSetting } = useUserSettings();
  const showAgentStatus = userSettings["display.show_agent_status"] === true;
  const notifyAwaitingReply = userSettings["notification.awaiting_reply"] === true;
  const terminatedCount = agents.filter((a) => a.status === "terminated").length;

  // `agents` is the combined live + terminated roster (use-agents always
  // merges the terminated scope — the tree builder needs terminated parent
  // rows to re-parent live children under their nearest visible ancestor).
  // showTerminated is a pure render filter: when hidden, drop terminated
  // rows from the flat list and let buildAgentTree flatten them out of the
  // tree (children re-parent under the nearest visible ancestor).
  const visibleAgents = showTerminated
    ? agents
    : agents.filter((a) => a.status !== "terminated");

  const isEmpty = visibleAgents.length === 0 && pendingSpawnCount === 0;
  const hasPending = pendingSpawnCount > 0;

  const treeProps: InnerProps & { wide: boolean } = hasPending
    ? { ...props, activeId: null }
    : props;

  // Compute flat sorted list (only used in flat mode).
  const flatAgents = useMemo(
    () => sortAgentsFlat(visibleAgents, sort),
    [visibleAgents, sort],
  );

  // Fold the tree once per roster/sort/toggle change. The combined roster
  // (live + terminated history) can be large, so the fold must not re-run on
  // every unrelated sidebar render. hideTerminated=!showTerminated lets
  // buildAgentTree re-parent children of hidden terminated nodes to the
  // nearest visible ancestor (#312 orphan regression).
  const tree = useMemo(
    () => buildAgentTree(agents, sort, { hideTerminated: !showTerminated }),
    [agents, sort, showTerminated],
  );

  const waiting = agents.reduce((n, a) => n + a.notices_awaiting_response.length, 0);

  return (
    <>
      {/* Spawn bar — min-w-0 lets SpawnButton's own selects shrink/wrap
          instead of pushing this row wider than the sidebar. */}
      <div className={cn("items-center gap-2 border-b border-border px-3 py-2", FLEX, MIN_W_0)}>
        <SpawnButton variant="sm" onSpawn={onSpawn} />
      </div>

      {/* 3. Toolbar: waiting indicator + view mode + status toggle + terminated toggle + sort.
          flex-wrap so a narrow sidebar wraps controls onto a second line
          instead of overflowing horizontally (none of these shrink). */}
      <div className={cn("flex-wrap items-center gap-1.5 px-3 py-1.5 border-b border-border text-[11px] text-muted-foreground", FLEX)}>
        {/* X waiting on you — a jumping count is a dynamic signal; only when
            awaiting-reply notifications are opted in. */}
        {notifyAwaitingReply && waiting > 0 && (
          <Link
            href={fleetHref(activeId)}
            className={cn("items-center gap-1 shrink-0 hover:text-foreground transition-colors", FLEX)}
          >
            <span className="size-1.5 rounded-full bg-destructive" />
            <span>{waiting}</span>
          </Link>
        )}

        {/* View mode toggle */}
        <button
          onClick={onToggleViewMode}
          className={cn(
            "items-center gap-1 px-1.5 py-0.5 rounded hover:bg-sidebar-accent/40 transition-colors shrink-0",
            "hover:text-foreground",
            FLEX
          )}
          aria-label={viewMode === "tree" ? t("switchFlat") : t("switchTree")}
        >
          <ListTree className="size-3.5" />
          <span>{viewMode === "tree" ? t("tree") : t("flat")}</span>
        </button>

        {/* Agent status quick toggle — reveals the per-row status colors
            (display.show_agent_status, default off: quiet). */}
        <button
          onClick={() => setSetting("display.show_agent_status", !showAgentStatus)}
          aria-label={showAgentStatus ? t("hideAgentStatus") : t("showAgentStatus")}
          aria-pressed={showAgentStatus}
          className={cn(
            "items-center gap-1 px-1.5 py-0.5 rounded hover:bg-sidebar-accent/40 transition-colors shrink-0",
            showAgentStatus && "text-foreground bg-sidebar-accent",
            "hover:text-foreground",
            FLEX
          )}
        >
          {showAgentStatus ? (
            <CircleDot className="size-3.5 shrink-0" />
          ) : (
            <Circle className="size-3.5 shrink-0" />
          )}
          <span>{t("status")}</span>
        </button>

        {/* Terminated toggle — only when terminated agents exist */}
        {terminatedCount > 0 && (
          <button
            onClick={onToggleTerminated}
            aria-label={showTerminated ? t("hideTerminatedAgents") : t("showTerminatedAgents")}
            aria-pressed={showTerminated}
            className={cn(
              "items-center gap-1 px-1.5 py-0.5 rounded hover:bg-sidebar-accent/40 transition-colors shrink-0",
              showTerminated && "text-foreground bg-sidebar-accent",
              "hover:text-foreground",
              FLEX
            )}
          >
            {showTerminated ? (
              <EyeOff className="size-3.5 shrink-0" />
            ) : (
              <Eye className="size-3.5 shrink-0" />
            )}
            <span>{showTerminated ? t("hideTerminated", { count: terminatedCount }) : t("showTerminated")}</span>
          </button>
        )}

        {/* Spacer to push sort to the right */}
        <span className={cn(FLEX_1)} />

        {/* Sort controls — collapsed to a single icon button (user ruling,
            #723 round 2): the toolbar row no longer carries the "Sort:" label
            or the three key buttons inline, so a narrower sidebar no longer
            wraps the row. Clicking the icon opens a small popover with the
            three keys; clicking the active key again flips the direction. */}
        <Popover.Root>
          <Popover.Trigger asChild>
            <button
              type="button"
              aria-label={t("sortByKey", { key: sort.key, dir: sort.dir === "asc" ? t("ascending") : t("descending") })}
              className="p-1 rounded hover:bg-sidebar-accent/40 hover:text-foreground shrink-0 transition-colors"
            >
              <ArrowUpDown className="size-3.5" />
            </button>
          </Popover.Trigger>
          <Popover.Portal>
            <Popover.Content
              sideOffset={6}
              align="end"
              className="z-50 min-w-[150px] rounded-md border border-border bg-popover text-popover-foreground shadow-md outline-none"
            >
              <div className="px-3 py-2 border-b border-border text-xs font-medium text-muted-foreground">
                {t("sortBy")}
              </div>
              <ul className="py-1">
                {(["id", "last_active", "status"] as FlatSortKey[]).map((key) => {
                  const active = sort.key === key;
                  const label = key === "last_active" ? "active" : key;
                  return (
                    <li key={key}>
                      <button
                        type="button"
                        onClick={() =>
                          setSort(
                            active
                              ? { key, dir: sort.dir === "asc" ? "desc" : "asc" }
                              : { key, dir: SORT_DEFAULT_DIR[key] },
                          )
                        }
                        aria-pressed={active}
                        className={cn(
                          "w-full text-left px-3 py-1.5 text-xs hover:bg-sidebar-accent/60 items-center justify-between gap-3",
                          active && "text-foreground",
                          FLEX
                        )}
                      >
                        <span>{label}</span>
                        {active ? (
                          <span aria-hidden>{sort.dir === "asc" ? "↑" : "↓"}</span>
                        ) : null}
                      </button>
                    </li>
                  );
                })}
              </ul>
            </Popover.Content>
          </Popover.Portal>
        </Popover.Root>
      </div>

      {/* ── Agent list ── */}
      <ScrollArea className={cn(FLEX_1, MIN_H_0)}>
        {isLoading && visibleAgents.length === 0 ? (
          <div className={cn("items-center justify-center gap-2 py-4 text-xs text-muted-foreground", FLEX)}>
            <Loader2 className="size-3.5 animate-spin" />
            {t("loadingAgents")}
          </div>
        ) : isEmpty ? (
          <div className="px-4 py-3 text-xs text-muted-foreground">
            {searchQuery ? t("noResults") : ""}
          </div>
        ) : viewMode === "flat" ? (
          <ul className="py-1">
            {Array.from({ length: pendingSpawnCount }, (_, i) => (
              <SpawningRow key={`spawning-${i}`} active={i === 0} showStatus={showAgentStatus} />
            ))}
            {flatAgents.map((agent) => (
              <AgentRowItem
                key={agent.agent_id}
                agent={agent}
                label={agent.label ?? undefined}
                active={treeProps.activeId === agent.agent_id}
                pending={treeProps.pendingActions[agent.agent_id]}
                wide={treeProps.wide}
                depth={0}
                ancestorsIsLast={[]}
                onSelect={() => treeProps.onSelect(agent.agent_id)}
                onTerminate={() => treeProps.onTerminate(agent.agent_id)}
                onForceKill={() => treeProps.onTerminate(agent.agent_id, true)}
                onRestart={() => treeProps.onRestart(agent.agent_id)}
                onResurrect={(prompt) => treeProps.onResurrect(agent.agent_id, prompt)}
                onFork={(prompt) => treeProps.onFork(agent.agent_id, prompt)}
                onCompact={() => treeProps.onCompact(agent.agent_id)}
                onRename={(label) => treeProps.onRename(agent.agent_id, label)}
              />
            ))}
          </ul>
        ) : (
          <ul className="py-1">
            {Array.from({ length: pendingSpawnCount }, (_, i) => (
              <SpawningRow key={`spawning-${i}`} active={i === 0} showStatus={showAgentStatus} />
            ))}
            {tree.map((node) => (
              <TreeNode
                key={node.agent.agent_id}
                node={node}
                depth={0}
                ancestorsIsLast={[]}
                {...treeProps}
              />
            ))}
          </ul>
        )}
      </ScrollArea>
    </>
  );
}

// Placeholder row for an in-flight spawn. The row itself is direct feedback to
// a user-initiated click, but its *motion* (pulse dot + spinner) counts as a
// dynamic signal and follows the status-color opt-in: quiet mode renders a
// neutral static placeholder instead.
function SpawningRow({ active, showStatus }: { active: boolean; showStatus: boolean }) {
  return (
    <li className="relative" data-testid="spawning-row">
      <div
        className={cn(
          "w-full text-left py-2.5 pr-4 font-mono text-xs items-center gap-2 border-l-2",
          active
            ? "bg-sidebar-accent text-sidebar-accent-foreground border-primary"
            : "text-muted-foreground border-transparent",
            FLEX
        )}
        style={{ paddingLeft: "16px" }}
      >
        {/* status dot follows the same opt-in as agent rows: hidden entirely
            when display.show_agent_status is off. */}
        {showStatus ? (
          <span className="size-1.5 rounded-full shrink-0 bg-slate-400 animate-pulse" />
        ) : null}
        <span className={cn("items-center min-h-4", FLEX_1, FLEX)}>
          {showStatus ? (
            <Loader2 className="size-3 animate-spin opacity-60" />
          ) : (
            <span className="text-[10px] opacity-60 leading-none">…</span>
          )}
        </span>
        <span className="text-[10px] opacity-0 shrink-0 tabular-nums">·</span>
        <span className="w-9 shrink-0" />
      </div>
    </li>
  );
}

interface TreeNodeProps extends InnerProps {
  wide: boolean;
  node: AgentNode;
  depth: number;
  ancestorsIsLast: readonly boolean[];
}

function TreeNode({ node, depth, ancestorsIsLast, ...rest }: TreeNodeProps) {
  const childAncestors = [...ancestorsIsLast, node.isLast];
  return (
    <>
      <AgentRowItem
        agent={node.agent}
        label={node.agent.label ?? undefined}
        active={rest.activeId === node.agent.agent_id}
        pending={rest.pendingActions[node.agent.agent_id]}
        wide={rest.wide}
        depth={depth}
        ancestorsIsLast={childAncestors}
        onSelect={() => rest.onSelect(node.agent.agent_id)}
        onTerminate={() => rest.onTerminate(node.agent.agent_id)}
        onForceKill={() => rest.onTerminate(node.agent.agent_id, true)}
        onRestart={() => rest.onRestart(node.agent.agent_id)}
        onResurrect={(prompt) => rest.onResurrect(node.agent.agent_id, prompt)}
        onFork={(prompt) => rest.onFork(node.agent.agent_id, prompt)}
        onCompact={() => rest.onCompact(node.agent.agent_id)}
        onRename={(label) => rest.onRename(node.agent.agent_id, label)}
      />
      {node.children.map((child) => (
        <TreeNode
          key={child.agent.agent_id}
          node={child}
          depth={depth + 1}
          ancestorsIsLast={childAncestors}
          {...rest}
        />
      ))}
    </>
  );
}
