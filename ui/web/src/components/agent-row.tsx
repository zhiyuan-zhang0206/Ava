// Single agent row — status dot on the left + tree-guide vertical line +
// label + time, with persistent action buttons on the right.
//
// Status is expressed inline via a colored dot on the left (running green /
// idling yellow / restarting blue pulse / terminated red) — no separate
// section per status; the sidebar is laid
// out by spawn tree. Status colors are opt-in (display.show_agent_status,
// default off): by default the dot is a neutral static grey so the list
// shows static presence without broadcasting state changes.
//
// Tree-guide: a 1px vertical line per depth level (absolutely positioned,
// full row height), drawn only for ancestors that are not the last child —
// visually equivalent to the GitHub file tree. No ├/└ glyphs; hierarchy
// is expressed through indent + vertical lines, cleaner than ASCII.
// Default grey, no active / hover highlight swap (we tried path-stem
// highlighting, but it stole focus visually; the active row's background highlight is
// already distinctive enough).
//
// Row actions: alive rows carry no on-row buttons — restart / terminate /
// fork / force-kill all live in the right-click context menu (long-press on
// touch), keeping the list clean. A terminated row keeps one always-on
// resurrect button (the single primary action for a dead agent); in-flight
// restart / terminate / resurrect shows an in-place spinner so feedback
// stays on the row.
// Terminated rows are distinguished only by the red dot — label font
// matches alive rows, no italic or dim opacity (the red dot is strong
// enough; another visual layer just becomes noise).
//
// The context menu's "with prompt..." items open a textarea dialog; the
// plain items fire immediately with no prompt. The destructive Kill
// (SIGKILL) sits behind a confirm so it can't be hit by accident.

import { useQuery } from "@tanstack/react-query";
import { GitFork, Loader2, PowerOff, RotateCw, Shrink, X } from "lucide-react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import { api } from "@/lib/api";
import { useUserSettings } from "@/lib/use-user-settings";
import type { TimeMode, DateFormat } from "@/lib/types";
import { PRIORITY_BG, topNoticePriority } from "@/lib/notices";
import { formatRelativeTime } from "@/lib/sidebar";
import { formatShort } from "@/lib/time";
import type { AgentRow, PublicAgentStatus } from "@/lib/types";
import { useInspectorPrefetch } from "@/lib/inspector-prefetch";
import { cn } from "@/lib/utils";
import { FLEX, FLEX_1, FLEX_COL, MIN_W_0 } from "@/lib/layout";

// Radix Dialog is needed only after an advanced prompt-taking menu action;
// closed rows keep no modal payload in the initial graph.
const LazyPromptDialog = dynamic(() =>
  import("@/components/agent-prompt-dialog").then((module) => module.PromptDialog),
  { loading: () => null },
);

export type PendingAction = "restarting" | "terminating" | "resurrecting" | "compacting";

// Public status semantics: sky = actively running, emerald = idling/waiting,
// red = terminated. Internal launch/restart states are projected to idling
// before a row reaches this component; offline remains a separate liveness
// badge.
export const STATUS_DOT: Record<PublicAgentStatus, string> = {
  running: "bg-sky-500",
  idling: "bg-emerald-500",
  terminated: "bg-destructive",
};

// i18n keys (under the "agentRow" namespace) for the status titles — the row
// renders the translated title when the component has a translator. (The
// pre-i18n STATUS_TITLE literal map was removed as dead.).
export const STATUS_TITLE_KEY: Record<PublicAgentStatus, string> = {
  running: "statusRunning",
  idling: "statusIdling",
  terminated: "statusTerminated",
};

export interface AgentRowProps {
  agent: AgentRow;
  label: string | undefined; // undefined → default `#N`
  active: boolean;
  pending: PendingAction | undefined;
  /** Wide sidebar mode (width past the threshold) — reveals a second line
   *  per row showing the agent's self-reported activity (or its lifecycle
   *  status when no activity is set). Narrow mode stays single-line. */
  wide: boolean;
  /** Tree depth (top level = 0); drives left-side indent. */
  depth: number;
  /** Whether each ancestor on the chain is its parent's last child —
   *  ordered root → direct parent. index 0 = furthest ancestor (root),
   *  index depth-1 = direct parent. d-th ancestor isLast → don't draw a
   *  vertical line at that depth (the family branch has ended). */
  ancestorsIsLast: readonly boolean[];
  onSelect: () => void;
  onTerminate: () => void;
  /** Force kill — SIGKILLs the process immediately, for an agent stuck in a
   *  loop / hung call that the graceful terminate can't reach. Right-click
   *  menu only (guarded by a confirm), never a one-click button. */
  onForceKill: () => void;
  onRestart: () => void;
  /** Resurrect a terminated agent. The plain button / quick menu item is a
   *  pure lifecycle event — no prompt; the agent just wakes. An optional
   *  prompt (the "with prompt..." menu item) is delivered as the agent wakes. */
  onResurrect: (prompt?: string) => void;
  /** Fork a child agent from this agent's latest checkpoint. An optional
   *  prompt seeds the child's first turn (the "with prompt..." menu item);
   *  omit it for a plain fork. */
  onFork: (prompt?: string) => void;
  onRename: (label: string) => void;
  onCompact: () => void;
}

/** Per-level indent (px). 14 = 1px guide line + surrounding spacing. */
const INDENT = 14;
/** x offset of the status dot center relative to the row's left edge
 *  (16px base paddingLeft + 3px dot radius). Bars sit on the d-th
 *  ancestor's dot-center column → visually drop straight down from the
 *  parent dot. */
const BAR_X_BASE = 19;

/** 1px vertical line per depth level spanning the full row height.
 *  Drawn for every ancestor depth — every child at every level gets a
 *  vertical bar so the lineage column is contiguous from top to bottom.
 *  In dark mode /20 alpha is too faint on the sidebar bg to be visible;
 *  bump to /40 so it stands out from the row. */
function TreeGuides({
  depth,
}: {
  depth: number;
  ancestorsIsLast: readonly boolean[];
}) {
  if (depth === 0) return null;
  const bars: React.ReactNode[] = [];
  for (let d = 0; d < depth; d++) {
    bars.push(
      <span
        key={d}
        aria-hidden
        data-testid="tree-guide"
        data-depth={d}
        style={{ left: BAR_X_BASE + d * INDENT }}
        className="pointer-events-none absolute inset-y-0 w-px bg-muted-foreground/20 dark:bg-muted-foreground/40"
      />,
    );
  }
  return <>{bars}</>;
}


export function AgentRow({
  agent,
  label,
  active,
  pending,
  depth,
  ancestorsIsLast,
  onSelect,
  onTerminate,
  onForceKill,
  onRestart,
  onResurrect,
  onFork,
  onRename,
  onCompact,
}: AgentRowProps) {
  const t = useTranslations("agentRow");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  // Prompt dialogs for the "... with prompt" menu items. Two independent
  // flags (not one "which") so a closing dialog never flips its title to
  // the other action mid-fade.
  const [forkOpen, setForkOpen] = useState(false);
  const [resurrectOpen, setResurrectOpen] = useState(false);
  const inspectPrefetch = useInspectorPrefetch(agent.agent_id);
  // Re-render every minute so the relative-time string refreshes
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((n) => n + 1), 60_000);
    return () => clearInterval(id);
  }, []);

  // Single-machine deployments: machine badge is noise; only show on
  // multi-machine setups so the user can see where each agent runs. Use
  // cluster.machines.length (registered count) rather than filter(online):
  // the latter hides every badge when one of two cluster machines is
  // briefly offline — exactly when the user most needs to see placement.
  // queryKey ["status"] shares cache with the Control page / SpawnButton;
  // TanStack Query dedupes, so no extra fetch.
  // machine='unknown' is the db default (manual INSERT / old row not
  // backfilled); skip rendering to avoid printing the literal "unknown"
  // into the sidebar.
  const { data: status } = useQuery({
    queryKey: ["status"],
    queryFn: api.getSystemStatus,
  });
  const machineCount = status?.cluster.machines.length ?? 0;
  const multiMachine = machineCount >= 2 && agent.machine !== "unknown";
  // User setting can override: if explicitly disabled, hide; if enabled and
  // multi-machine, show.
  const { settings: userSettings } = useUserSettings();
  const showMachineSetting = userSettings["display.show_machine_name"] !== false;
  const showMachine = showMachineSetting ? multiMachine : false;
  const rawTimeMode = userSettings["display.time_mode"];
  const timeMode: TimeMode =
    rawTimeMode === "last_active" || rawTimeMode === "spawned" || rawTimeMode === "hidden"
      ? rawTimeMode
      : "last_active";
  const rawDateFormat = userSettings["display.date_format"];
const dateFormat: DateFormat = rawDateFormat === "absolute" || rawDateFormat === "relative" ? rawDateFormat : "relative";
  // Quiet defaults (RCS): dynamic signals are opt-in (`=== true`, default off) —
  // the status color dot and the awaiting-reply badge stay hidden until the
  // user explicitly enables them.
  const showAgentStatus = userSettings["display.show_agent_status"] === true;
  const notifyAwaitingReply = userSettings["notification.awaiting_reply"] === true;

  // #ID and label coexist, complementing each other: `#180 · screen-permission` or `#180` (when there's no label)
  // "hidden" renders no timestamp; for the other modes pick the source instant.
  const showTime = timeMode !== "hidden";
  const timeIso = timeMode === "spawned" ? agent.spawned_at : agent.last_active_at;
  const absTime = dateFormat === "absolute"
    ? formatShort(timeIso)
    : formatRelativeTime(timeIso);
  const indentPx = depth * INDENT;

  const submit = () => {
    onRename(draft);
    setEditing(false);
  };

  /** Safely call window.confirm — degrades gracefully in test environments
   *  where window.confirm may not be defined (happy-dom). */
  const safeConfirm = (message: string): boolean => {
    if (typeof window.confirm === "function") {
      return window.confirm(message);
    }
    // No confirm available (test environment) — proceed as if confirmed
    return true;
  };

  const confirmForceKill = () => {
    const confirmSetting = userSettings["behavior.confirm_force_kill"] !== false;
    if (!confirmSetting) {
      onForceKill();
      return;
    }
    if (
      safeConfirm(
        t("forceKillConfirm", { id: agent.agent_id }),
      )
    ) {
      onForceKill();
    }
  };

  const confirmTerminate = () => {
    const confirmSetting = userSettings["behavior.confirm_terminate"] !== false;
    if (!confirmSetting) {
      onTerminate();
      return;
    }
    if (
      safeConfirm(
        t("terminateConfirm", { id: agent.agent_id }),
      )
    ) {
      onTerminate();
    }
  };

  const confirmRestart = () => {
    const confirmSetting = userSettings["behavior.confirm_restart"] !== false;
    if (!confirmSetting) {
      onRestart();
      return;
    }
    if (
      safeConfirm(
        t("restartConfirm", { id: agent.agent_id }),
      )
    ) {
      onRestart();
    }
  };

  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <li className="group relative">
          {/* Adjacent rows' bars at the same x naturally stack into a
              continuous vertical line — this is why the "path" looks
              contiguous, and the hard reason buttons can't be
              position:relative (positioned elements paint after bars,
              covering them). */}
          <TreeGuides depth={depth} ancestorsIsLast={ancestorsIsLast} />
          <button
            type="button"
            onClick={() => {
              inspectPrefetch.prefetch();
              onSelect();
            }}
            onMouseEnter={inspectPrefetch.schedule}
            onMouseLeave={inspectPrefetch.cancel}
            className={cn(
              // No transition-colors — active switch must be instant,
              // otherwise the old-focused row's bg fades over 150ms while
              // the placeholder lights up, making it look like "the
              // placeholder gets focus first, then moves"
              // cursor-default: the global button cursor:pointer rule (#709)
              // gives this row a hand cursor; a switching row is a list item,
              // not a button — user ruling (#723) keeps the default arrow.
              "w-full text-left py-2.5 pr-4 font-sans text-xs items-center gap-2 cursor-default",
              FLEX,
              active
                ? "bg-sidebar-accent text-sidebar-accent-foreground"
                : "text-muted-foreground hover:bg-sidebar-accent/50 hover:text-foreground",
            )}
            style={{ paddingLeft: `${16 + indentPx}px` }}
          >
            {/* status dot — colored per lifecycle status, shown only when
                display.show_agent_status is on. When off the dot is hidden
                entirely (not just decolored) so the row broadcasts no state at
                all — the quiet default. One exception: an agent judged offline
                by the gateway liveness pass (Task #1174 — machine unreachable
                or process lease expired) always shows a grey dot, so a dead
                agent can never masquerade as present under the quiet default. */}
            {showAgentStatus || agent.liveness_state === "offline" ? (
              <span
                title={
                  agent.liveness_state === "offline" ? t("statusOffline") : undefined
                }
                className={cn(
                  "size-1.5 rounded-full shrink-0",
                  agent.liveness_state === "offline"
                    ? "bg-muted-foreground/50"
                    : STATUS_DOT[agent.status],
                )}
              />
            ) : null}
            {/* notices-awaiting-response badge — count colored by highest
                priority; only when notification.awaiting_reply is on (a
                jumping count is a dynamic signal). */}
            {(() => {
              const p = notifyAwaitingReply
                ? topNoticePriority(agent.notices_awaiting_response)
                : null;
              return p ? (
                <span
                  className={cn(
                    "shrink-0 rounded-full px-1 font-mono text-2xs font-semibold leading-[1.4] text-white tabular-nums",
                    PRIORITY_BG[p],
                  )}
                >
                  {agent.notices_awaiting_response.length}
                </span>
              ) : null;
            })()}
            <span className={cn(FLEX_1, MIN_W_0, FLEX, FLEX_COL)}>
              {editing ? (
                <span className={cn("items-center gap-1", MIN_W_0, FLEX)}>
                  <span className="font-mono text-xs shrink-0 select-none">#{agent.agent_id} ·</span>
                  <input
                    autoFocus
                    value={draft}
                    aria-label={t("rename", { id: agent.agent_id })}
                    onChange={(e) => setDraft(e.target.value)}
                    onClick={(e) => e.stopPropagation()}
                    onBlur={submit}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        submit();
                      } else if (e.key === "Escape") {
                        e.preventDefault();
                        setEditing(false);
                      }
                    }}
                    className={cn("bg-background border border-primary rounded px-1 py-0 text-xs focus:outline-none h-5", MIN_W_0)}
                    placeholder={label ?? "Label"}
                  />
                </span>
              ) : (
                <span
                  className={cn("items-center", MIN_W_0, FLEX)}
                  onDoubleClick={(e) => {
                    e.stopPropagation();
                    setDraft(label ?? "");
                    setEditing(true);
                  }}
                >
                  <span className="shrink-0 font-mono">#{agent.agent_id}</span>
                  {label ? (
                    <>
                      {" · "}
                      <span className={cn("break-words", MIN_W_0)}>{label}</span>
                    </>
                  ) : null}
                </span>
              )}
            </span>
            {showMachine ? (
              <span
                className="font-mono text-2xs opacity-60 shrink-0 px-1 rounded border border-border/50"
              >
                {agent.machine}
              </span>
            ) : null}
            {showTime ? (
              <span className="font-mono text-2xs opacity-60 shrink-0 tabular-nums">
                {absTime}
              </span>
            ) : null}
            {/* Reserve room for the absolutely-positioned resurrect button /
                pending spinner so the timestamp never underlaps it. Alive
                idle rows render nothing there, so the timestamp keeps the
                right edge. */}
            {!editing && (agent.status === "terminated" || pending) ? (
              <span className="w-5 shrink-0" />
            ) : null}
          </button>
          {!editing ? (
            <RowActions
              agent={agent}
              pending={pending}
              onResurrect={() => onResurrect()}
            />
          ) : null}
        </li>
      </ContextMenuTrigger>
      <ContextMenuContent>
        {/* onSelect hands Radix's Event to the callback; wrap the
            prompt-taking actions so the Event is not passed as a prompt. */}
        {agent.status === "terminated" ? (
          <>
            <ContextMenuItem onSelect={() => onResurrect()}>
              <RotateCw /> {t("resurrect")}
            </ContextMenuItem>
            <ContextMenuItem onSelect={() => setResurrectOpen(true)}>
              <RotateCw /> {t("resurrectWithPrompt")}
            </ContextMenuItem>
          </>
        ) : (
          <>
            <ContextMenuItem
              onSelect={confirmRestart}
              disabled={pending !== undefined}
            >
              <RotateCw className="text-emerald-500" /> {t("restart")}
            </ContextMenuItem>
            <ContextMenuItem
              onSelect={confirmTerminate}
              disabled={pending !== undefined}
            >
              <PowerOff className="text-destructive" /> {t("terminate")}
            </ContextMenuItem>
            <ContextMenuItem onSelect={onCompact} disabled={pending !== undefined}>
              <Shrink className="text-amber-500" /> {t("compact")}
            </ContextMenuItem>
            <ContextMenuItem onSelect={() => onFork()}>
              <GitFork /> {t("fork")}
            </ContextMenuItem>
            <ContextMenuItem onSelect={() => setForkOpen(true)}>
              <GitFork /> {t("forkWithPrompt")}
            </ContextMenuItem>
            <ContextMenuSeparator />
            <ContextMenuItem
              variant="destructive"
              onSelect={confirmForceKill}
              disabled={pending !== undefined}
            >
              <X /> {t("kill")}
            </ContextMenuItem>
          </>
        )}
      </ContextMenuContent>
      {forkOpen ? (
        <LazyPromptDialog
          open
          onOpenChange={setForkOpen}
          title={t("forkAgent")}
          description={t("forkAgentDesc")}
          placeholder={t("forkPromptPlaceholder")}
          submitLabel={t("fork")}
          onSubmit={onFork}
        />
      ) : null}
      {resurrectOpen ? (
        <LazyPromptDialog
          open
          onOpenChange={setResurrectOpen}
          title={t("resurrectAgent")}
          description={t("resurrectDesc")}
          placeholder={t("resurrectPromptPlaceholder")}
          submitLabel={t("resurrect")}
          onSubmit={onResurrect}
        />
      ) : null}
    </ContextMenu>
  );
}

// On-row feedback + the one quick action that stays on the row. Alive rows
// render nothing unless an action is in flight (restart / terminate live in
// the context menu); a terminated row keeps the always-on resurrect button —
// always visible, not hover-gated, because mobile has no hover.
function RowActions({
  agent,
  pending,
  onResurrect,
}: {
  agent: AgentRow;
  pending: PendingAction | undefined;
  onResurrect: () => void;
}) {
  const t = useTranslations("agentRow");
  // right-3 (12px) instead of flush with the right edge — leaves room
  // for the ScrollArea vertical scrollbar (w-2.5 / 10px); otherwise the
  // button is covered by the scrollbar and clicks land on the scrollbar.
  const wrapperCls =
    "absolute right-3 top-1/2 -translate-y-1/2 flex items-center gap-0.5";

  if (agent.status === "terminated") {
    return (
      <div className={wrapperCls}>
        {pending === "resurrecting" ? (
          <Spinner color="text-emerald-500" />
        ) : (
          <button
            type="button"
            onClick={onResurrect}
            disabled={pending !== undefined}
            className="p-0.5 rounded hover:bg-emerald-500/20 hover:text-emerald-500 text-muted-foreground disabled:opacity-30"
            aria-label={t("resurrectConfirm", { id: agent.agent_id })}
          >
            <RotateCw className="size-3" />
          </button>
        )}
      </div>
    );
  }

  if (pending === "restarting") {
    return (
      <div className={wrapperCls}>
        <Spinner color="text-emerald-500" />
      </div>
    );
  }
  if (pending === "terminating") {
    return (
      <div className={wrapperCls}>
        <Spinner color="text-destructive" />
      </div>
    );
  }
  if (pending === "compacting") {
    return (
      <div className={wrapperCls}>
        <Spinner color="text-amber-500" />
      </div>
    );
  }

  // Alive, idle — no on-row actions
  return null;
}

function Spinner({ color }: { color: string }) {
  return (
    <span className="p-0.5 inline-flex items-center justify-center">
      <Loader2 className={cn("size-3 animate-spin", color)} />
    </span>
  );
}
