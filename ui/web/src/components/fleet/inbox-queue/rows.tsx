"use client";

import { AppWindow } from "lucide-react";
import { useTranslations } from "next-intl";

import { STATUS_DOT } from "@/components/agent-row";
import { PRIORITY_BG } from "@/lib/notices";
import { formatRelativeTime } from "@/lib/sidebar";
import type { PublicAgentStatus, OpenNotice, PageRow } from "@/lib/types";
import { cn } from "@/lib/utils";
import { FLEX, FLEX_1, MIN_W_0 } from "@/lib/layout";

export function PriorityBadge({ priority, muted }: { priority: OpenNotice["priority"]; muted?: boolean }) {
  return (
    <span
      className={cn(
        "shrink-0 rounded px-1 font-mono text-2xs font-semibold text-white",
        muted ? "bg-muted-foreground/40" : PRIORITY_BG[priority],
      )}
    >
      {priority}
    </span>
  );
}


export function AgentStatusDot({ status }: { status: PublicAgentStatus | null }) {
  if (!status) return null;
  return (
    <span
      className={cn("size-1.5 rounded-full shrink-0", STATUS_DOT[status])}
    />
  );
}

// A direct link to an agent's live page (ava.ui.show), opened in a new tab — the
// inbox's entry point to what the agent is showing. Icon-only on a queue row,
// icon + title in a detail header. Stops click propagation so opening the page
// never also selects the row it sits on.

export function AgentPageLink({
  page,
  showLabel,
  className,
}: {
  page: PageRow;
  showLabel?: boolean;
  className?: string;
}) {
  const t = useTranslations("fleet.inboxPanel");
  return (
    <a
      href={page.url}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(e) => e.stopPropagation()}
      aria-label={t("openLivePage", { title: page.title ?? page.name })}
      className={cn(
        "inline-flex items-center gap-1 text-muted-foreground hover:text-primary",
        className,
      )}
    >
      <AppWindow className="size-3.5 shrink-0" aria-hidden />
      {showLabel && <span className="truncate">{page.title ?? page.name}</span>}
    </a>
  );
}


export function NoticeListRow({
  priority,
  blocking,
  title,
  agentLabel,
  agentId,
  agentStatus,
  agentPage,
  createdAt,
  muted,
  indent,
  selected,
  onSelect,
  onSelectAgent,
  noticeKey,
  anchorHighlighted,
}: {
  priority: OpenNotice["priority"];
  blocking: boolean;
  title: string;
  agentLabel: string;
  agentId: number;
  agentStatus: PublicAgentStatus | null;
  agentPage?: PageRow;
  createdAt?: string;
  muted?: boolean;
  indent?: boolean;
  selected: boolean;
  onSelect: () => void;
  onSelectAgent?: (agentId: number | null) => void;
  noticeKey?: string;
  anchorHighlighted?: boolean;
}) {
  const t = useTranslations("fleet.inboxPanel");
  return (
    <li
      data-testid="inbox-row"
      data-notice-key={noticeKey}
      data-anchor-highlighted={anchorHighlighted ? "true" : undefined}
      className={cn(
        "items-stretch border-b border-border/50 transition-colors",
        selected && "bg-sidebar-accent",
        anchorHighlighted && "bg-primary/10 ring-1 ring-inset ring-primary/50",
        FLEX
      )}
    >
      <button
        type="button"
        onClick={() => {
          onSelect();
          onSelectAgent?.(agentId);
        }}
        className={cn(
          "text-left px-4 py-2.5 items-center gap-2 hover:bg-sidebar-accent/50",
          indent && "pl-7",
          muted && "opacity-55",
          MIN_W_0, FLEX_1, FLEX
        )}
      >
        <PriorityBadge priority={priority} muted={muted} />
        {blocking && (
          <span className="shrink-0 rounded bg-destructive/10 px-1 text-2xs font-medium uppercase text-destructive">
            {t("blocking")}
          </span>
        )}
        <span className={cn("truncate text-xs", MIN_W_0, FLEX_1)}>{title}</span>
        <span className={cn("max-w-[45%] items-center gap-1.5 font-sans text-2xs text-muted-foreground", FLEX, MIN_W_0)}>
          <AgentStatusDot status={agentStatus} />
          <span className={cn("truncate", MIN_W_0)}>
            {agentLabel}
          </span>
          {createdAt ? (
            <span className="shrink-0 font-mono">· {formatRelativeTime(createdAt)}</span>
          ) : null}
        </span>
      </button>
      {agentPage && <AgentPageLink page={agentPage} className="shrink-0 px-2.5" />}
    </li>
  );
}

// The detail header identifying the agent (status + label + relative time). The
// notice's own content and reply controls come from the shared
// <OpenNoticeDetail> below it.

export function AgentContextHeader({
  agentLabel,
  agentStatus,
  agentPage,
  createdAt,
}: {
  agentLabel: string;
  agentStatus: PublicAgentStatus | null;
  agentPage?: PageRow;
  createdAt?: string;
}) {
  return (
    <div className="mb-3">
      <div className={cn("items-center gap-2", FLEX)}>
        <AgentStatusDot status={agentStatus} />
        <span className={cn("truncate font-sans text-2xs text-muted-foreground", MIN_W_0)}>
          {agentLabel}
        </span>
        {createdAt ? (
          <span className="shrink-0 font-mono text-2xs text-muted-foreground">
            · {formatRelativeTime(createdAt)}
          </span>
        ) : null}
        {agentPage && (
          <AgentPageLink page={agentPage} showLabel className={cn("ml-auto text-2xs", MIN_W_0)} />
        )}
      </div>
    </div>
  );
}
