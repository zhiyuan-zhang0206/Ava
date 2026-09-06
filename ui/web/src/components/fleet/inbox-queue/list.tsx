"use client";

import { Fragment, type RefObject } from "react";
import { ChevronRight, PanelRightClose } from "lucide-react";
import { useTranslations } from "next-intl";

import { BAR_HEIGHT_CLASS, FLEX, FLEX_1, MIN_W_0 } from "@/lib/layout";
import type { QueueUnit } from "@/lib/task-notify";
import type { PublicAgentStatus, NoticeItem, PageRow } from "@/lib/types";
import { cn } from "@/lib/utils";

import { fmtLabel, openKey, resolvedKey, type OpenItem } from "./keys";
import { NoticeListRow } from "./rows";

// BAR_HEIGHT_CLASS is fixed (not padding-derived) so this bar and the left
// pane's Agents/Tasks bar line up exactly — their content heights differ.
export function QueueHeader({
  total,
  onCollapse,
  stale,
}: {
  total: number;
  onCollapse?: () => void;
  stale: boolean;
}) {
  const t = useTranslations("fleet.inboxPanel");
  return (
    <header className={cn("shrink-0 items-center gap-2 border-b border-border px-4", BAR_HEIGHT_CLASS, FLEX)}>
      <h2 className="text-sm font-semibold">{t("title")}</h2>
      {total > 0 && (
        <span className="rounded-full bg-muted px-1.5 font-mono text-2xs tabular-nums text-foreground">
          {total}
        </span>
      )}
      {/* Stale-while-error marker (audit C3): a refresh failed but the last
          loaded entries are still on screen — lightweight flag, never a blank
          queue (the "Failed to load inbox." screen is reserved for a cold
          failure). Same treatment as the Task Graph's StaleBadge. */}
      {stale && (
        <span
          className="inline-flex items-center gap-1 text-2xs text-amber-600 dark:text-amber-400"
          title={t("refreshFailed")}
        >
          <span className="size-1.5 rounded-full bg-amber-500" />
          {t("stale")}
        </span>
      )}
      {onCollapse && (
        <button
          type="button"
          onClick={onCollapse}
          aria-label={t("collapseQueue")}
          className="ml-auto p-1 rounded text-muted-foreground hover:bg-sidebar-accent hover:text-foreground"
        >
          <PanelRightClose className="size-3.5" aria-hidden />
        </button>
      )}
    </header>
  );
}


export function EmptyState({ failed, loading }: { failed: boolean; loading: boolean }) {
  const t = useTranslations("fleet.inboxPanel");
  if (failed) {
    return (
      <p className="px-4 py-8 text-center text-xs text-destructive">
        {t("loadFailed")}
      </p>
    );
  }
  if (loading) {
    return (
      <p className="px-4 py-8 text-center text-xs text-muted-foreground">
        {t("loading")}
      </p>
    );
  }
  return (
    <p className="px-4 py-8 text-center text-xs text-muted-foreground">
      {t("empty")}
    </p>
  );
}


// The scrolling list body — the grouped open stream, then the collapsed resolved
// history. Shared by the desktop and compact list columns. `effectiveKey` null
// means the list shows no selection (the compact list-only view).
export function QueueList({
  units,
  resolved,
  resolvedOpen,
  setResolvedOpen,
  agentStatus,
  activePageByAgent,
  effectiveKey,
  onSelect,
  onSelectAgent,
  hasNextPage,
  isFetchingNextPage,
  fetchNextPage,
  openFailed,
  historyFailed,
  listRef,
  anchorHighlightKey,
}: {
  units: QueueUnit<OpenItem>[];
  resolved: NoticeItem[];
  resolvedOpen: boolean;
  setResolvedOpen: (fn: (o: boolean) => boolean) => void;
  agentStatus: Map<number, PublicAgentStatus>;
  activePageByAgent: Map<number, PageRow>;
  effectiveKey: string | null;
  onSelect: (key: string) => void;
  onSelectAgent?: (agentId: number | null) => void;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  fetchNextPage: () => void;
  openFailed: boolean;
  historyFailed: boolean;
  listRef: RefObject<HTMLUListElement | null>;
  anchorHighlightKey: string | null;
}) {
  const t = useTranslations("fleet.inboxPanel");
  return (
    <ul ref={listRef} className={cn("overflow-y-auto", FLEX_1, MIN_W_0)} aria-label={t("queue")}>
      <OpenUnits
        units={units}
        agentStatus={agentStatus}
        activePageByAgent={activePageByAgent}
        effectiveKey={effectiveKey}
        onSelect={onSelect}
        onSelectAgent={onSelectAgent}
        anchorHighlightKey={anchorHighlightKey}
      />
      {units.length === 0 && (
        <li className="px-4 py-6 text-center text-xs text-muted-foreground">
          {openFailed ? t("openLoadFailed") : t("openEmpty")}
        </li>
      )}
      {historyFailed && (
        <li className="px-4 py-3 text-center text-2xs text-muted-foreground">
          {t("historyUnavailable")}
        </li>
      )}
      {resolved.length > 0 && (
        <li>
          <button
            type="button"
            onClick={() => setResolvedOpen((o) => !o)}
            aria-expanded={resolvedOpen}
            className={cn("w-full items-center gap-1.5 px-4 pt-3 pb-1 text-2xs font-medium uppercase tracking-wide text-muted-foreground/70 hover:text-foreground", FLEX)}
          >
            <ChevronRight className={cn("size-3 transition-transform", resolvedOpen && "rotate-90")} aria-hidden />
            {/* No count badge — user ruling 2026-08-09 #1096: the word alone. */}
            {t("resolved")}
          </button>
        </li>
      )}
      {resolvedOpen &&
        resolved.map((n) => (
          <NoticeListRow
            key={resolvedKey(n.id)}
            priority={n.priority}
            blocking={false}
            title={n.title}
            agentLabel={fmtLabel(n.agent_label, n.agent_id)}
            agentId={n.agent_id}
            agentStatus={agentStatus.get(n.agent_id) ?? null}
            muted
            createdAt={n.created_at}
            selected={effectiveKey === resolvedKey(n.id)}
            onSelect={() => onSelect(resolvedKey(n.id))}
            onSelectAgent={onSelectAgent}
          />
        ))}
      {resolvedOpen && hasNextPage && (
        <li>
          <button
            type="button"
            onClick={fetchNextPage}
            disabled={isFetchingNextPage}
            className="w-full px-4 py-2 text-center text-2xs text-muted-foreground hover:text-foreground disabled:opacity-50"
          >
            {isFetchingNextPage ? t("loading") : t("showMore")}
          </button>
        </li>
      )}
    </ul>
  );
}

// The grouped open list: one header row per task subtree with its entries
// indented beneath it; loose entries (agent owns no task) render flat.

export function OpenUnits({
  units,
  agentStatus,
  activePageByAgent,
  effectiveKey,
  onSelect,
  onSelectAgent,
  anchorHighlightKey,
}: {
  units: QueueUnit<OpenItem>[];
  agentStatus: Map<number, PublicAgentStatus>;
  activePageByAgent: Map<number, PageRow>;
  effectiveKey: string | null;
  onSelect: (key: string) => void;
  onSelectAgent?: (agentId: number | null) => void;
  anchorHighlightKey: string | null;
}) {
  const t = useTranslations("fleet.inboxPanel");
  return (
    <>
      {units.map((u) => {
        const rows = u.items.map((it) => {
          const key = openKey(it.notice.id);
          return (
            <NoticeListRow
              key={key}
              noticeKey={key}
              priority={it.notice.priority}
              blocking={it.notice.blocking}
              title={it.notice.title}
              agentLabel={it.agentLabel}
              agentId={it.agentId}
              agentStatus={agentStatus.get(it.agentId) ?? null}
              agentPage={activePageByAgent.get(it.agentId)}
              createdAt={it.notice.created_at}
              indent={u.root !== null}
              selected={effectiveKey === key}
              onSelect={() => onSelect(key)}
              onSelectAgent={onSelectAgent}
              anchorHighlighted={anchorHighlightKey === key}
            />
          );
        });
        if (u.root === null) {
          return <Fragment key={`loose-${u.items[0].notice.id}`}>{rows}</Fragment>;
        }
        return (
          <Fragment key={`task-${u.root.id}`}>
            <li
              className={cn("items-center gap-2 px-4 pt-3 pb-1", FLEX)}
              aria-label={t("task", { id: u.root.id, title: u.root.title })}
            >
              <span className={cn("truncate text-2xs font-medium text-muted-foreground", MIN_W_0)}>
                #{u.root.id} {u.root.title}
              </span>
              <span className="shrink-0 rounded-full bg-muted px-1.5 font-mono text-2xs tabular-nums text-foreground">
                {u.items.length}
              </span>
            </li>
            {rows}
          </Fragment>
        );
      })}
    </>
  );
}
