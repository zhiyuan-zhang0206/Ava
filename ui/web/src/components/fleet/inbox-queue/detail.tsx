"use client";

import { ArrowLeft } from "lucide-react";

import { ChatMarkdown } from "@/components/markdown";
import { OpenNoticeDetail } from "@/components/open-notice-detail";
import { formatRelativeTime } from "@/lib/sidebar";
import type { PublicAgentStatus, NoticeItem, PageRow } from "@/lib/types";

import { fmtLabel, openKey, type OpenItem } from "./keys";
import { AgentContextHeader, PriorityBadge } from "./rows";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0 } from "@/lib/layout";
import { cn } from "@/lib/utils";

export function CompactDetail({
  label,
  onBack,
  children,
}: {
  label: string;
  onBack: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className={cn("h-full", FLEX, FLEX_COL, MIN_H_0)}>
      <div className={cn("shrink-0 items-center gap-2 border-b border-border px-4 py-2", FLEX)}>
        <button
          type="button"
          onClick={onBack}
          className="p-1 rounded text-muted-foreground hover:text-foreground hover:bg-sidebar-accent"
          aria-label="Back to list"
        >
          <ArrowLeft className="size-4" />
        </button>
        <span className="text-xs text-muted-foreground">{label}</span>
      </div>
      {children}
    </div>
  );
}

export function OpenDetail({
  item,
  openItems,
  agentStatus,
  agentPage,
  onAdvance,
  onSelectAgent,
}: {
  item: OpenItem;
  openItems: OpenItem[];
  agentStatus: PublicAgentStatus | null;
  agentPage?: PageRow;
  onAdvance: (key: string | null) => void;
  onSelectAgent?: (agentId: number | null) => void;
}) {
  const idx = openItems.findIndex((it) => it.notice.id === item.notice.id);

  // Move the selection to the neighbour at `idx + dir`, if any (arrow cycling).
  const cycle = (dir: -1 | 1) => {
    const j = idx + dir;
    if (j < 0 || j >= openItems.length) return;
    const t = openItems[j];
    onAdvance(openKey(t.notice.id));
    onSelectAgent?.(t.agentId);
  };

  // After a resolve, advance to the next open notice (or the previous if this was
  // last). Computed from the CURRENT list — the just-resolved item is still
  // present at this instant; the SSE drops it a beat later.
  const advanceAfterResolve = () => {
    const next =
      idx + 1 < openItems.length ? openItems[idx + 1] : idx - 1 >= 0 ? openItems[idx - 1] : null;
    onAdvance(next ? openKey(next.notice.id) : null);
    if (next) onSelectAgent?.(next.agentId);
  };

  return (
    <div className={cn("h-full", FLEX, FLEX_COL, MIN_H_0)}>
      <div className={cn("overflow-y-auto px-4 py-3", FLEX_1)}>
        <AgentContextHeader
          agentLabel={item.agentLabel}
          agentStatus={agentStatus}
          agentPage={agentPage}
          createdAt={item.notice.created_at}
        />
        <OpenNoticeDetail
          agentId={item.agentId}
          notice={item.notice}
          autoFocus
          onResolved={advanceAfterResolve}
          onCycle={cycle}
        />
      </div>
    </div>
  );
}


export function ResolvedDetail({
  notice,
  agentStatus,
}: {
  notice: NoticeItem;
  agentStatus: PublicAgentStatus | null;
}) {
  const label =
    notice.resolution === "dismissed" ? "Dismissed" : notice.resolution === "read" ? "Read" : "Your answer";
  return (
    <div className={cn("h-full", FLEX, FLEX_COL, MIN_H_0)}>
      <div className={cn("overflow-y-auto px-4 py-3", FLEX_1)}>
        <AgentContextHeader
          agentLabel={fmtLabel(notice.agent_label, notice.agent_id)}
          agentStatus={agentStatus}
          createdAt={notice.created_at}
        />
        <div className={cn("items-center gap-2", FLEX)}>
          <PriorityBadge priority={notice.priority} muted />
          <span className="text-[10px] text-muted-foreground">{notice.require_response ? "Decision" : "FYI"}</span>
        </div>
        <h3 className="mt-2 text-sm font-medium break-words">{notice.title}</h3>
        {notice.content && (
          <div className="mt-3 text-xs text-foreground/90">
            <ChatMarkdown content={notice.content} />
          </div>
        )}
        {/* The resolution reads as the next turn of the same exchange, so it
            follows the agent's text directly instead of being pinned to the
            bottom of the panel with a gap of blank space in between. */}
        <div className="mt-4 border-t border-border pt-3">
          <div className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground/70">
            {label}
            {notice.resolved_at ? ` · ${formatRelativeTime(notice.resolved_at)}` : ""}
          </div>
          {notice.reply && (
            <p className="mt-1 whitespace-pre-wrap break-words text-xs text-foreground/80">{notice.reply}</p>
          )}
        </div>
      </div>
    </div>
  );
}
