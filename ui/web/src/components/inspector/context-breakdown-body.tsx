"use client";

import { keepPreviousData, useQuery, type UseQueryResult } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { api } from "@/lib/transport/api";
import { categoryColor } from "@/lib/context-colors";
import { errMsg } from "@/lib/contracts/errors";
import { formatShort } from "@/lib/format/time";
import { approx, formatTokens } from "@/lib/format/format-number";
import type { ContextBreakdownResponse } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { FLEX, FLEX_1, FLEX_COL } from "@/lib/layout/layout";

import { SectionRows } from "./context-sections";

const CATEGORY_MESSAGE_KEY: Record<string, string> = {
  system_prompt: "categories.system_prompt",
  compact_summary: "categories.compact_summary",
  cluster_memory: "categories.cluster_memory",
  agent_memory: "categories.agent_memory",

  system_notes: "categories.system_notes",
  user_input: "categories.user_input",
  agent_messages: "categories.agent_messages",

  reasoning: "categories.reasoning",
  output: "categories.output",
  tool_call: "categories.tool_call",
  tool_response: "categories.tool_response",
};

export interface CategoryHighlight {

  active: string | null;

  has: (category: string) => boolean;
  onToggle: (category: string) => void;
}

function httpStatus(error: unknown): number | null {
  const status = error instanceof Error ? (error as { status?: unknown }).status : undefined;
  return typeof status === "number" ? status : null;
}

// meiva:ref ui/web/src/docs/frontend-components/context-breakdown.meiva.okf.md#context-point-and-failure
export function ContextBreakdownCard({
  agentId,
  at,
  snapshot,
  categoryHighlight,
}: {
  agentId: number;
  at?: number | null;
  snapshot?: string;
  categoryHighlight?: CategoryHighlight;
}) {
  const t = useTranslations("contextBreakdown");
  const pointed = at !== undefined;
  const point = useQuery({
    queryKey: ["run-timeline-context", agentId, at, snapshot] as const,
    queryFn: () => snapshot === undefined ? api.getRunTimelineContext(agentId, at ?? 0) : api.getRunTimelineContext(agentId, at ?? 0, snapshot),
    enabled: pointed && at !== null,
    staleTime: 10_000,

    placeholderData: keepPreviousData,
  });

  const noRequest = httpStatus(point.error) === 404;
  const title = point.data
    ? t("pointTitle", {
        session: point.data.session + 1,
        sessions: point.data.sessions,
        time: formatShort(point.data.ts),
      })
    : t("title");
  return (
    <section
      data-testid="context-breakdown-card"
      aria-label={t("title")}
      className="max-w-[520px] rounded border border-border bg-card p-4"
    >
      <h2 className="mb-1 text-sm font-semibold" data-testid="context-breakdown-heading">
        {title}
      </h2>
      <p data-testid="context-breakdown-subtitle" className="mb-2 text-muted-foreground text-xs">
        {pointed ? t("pointSubtitle") : t("subtitle")}
      </p>
      {!pointed ? (
        <ContextBreakdownBody agentId={agentId} categoryHighlight={categoryHighlight} />
      ) : at === null || noRequest ? (
        <p data-testid="context-breakdown-empty" className="text-muted-foreground text-xs">
          {t("pointEmpty")}
        </p>
      ) : (
        <BreakdownQuery query={point} categoryHighlight={categoryHighlight} />
      )}
    </section>
  );
}

// meiva:ref ui/web/src/docs/frontend-components/context-breakdown.meiva.okf.md#context-point-and-failure
export function ContextBreakdownBody({
  agentId,
  categoryHighlight,
}: {
  agentId: number;
  categoryHighlight?: CategoryHighlight;
}) {
  const query = useQuery({
    queryKey: ["context-breakdown", agentId] as const,
    queryFn: () => api.getContextBreakdown(agentId),
    staleTime: 10_000,

    refetchOnMount: "always",
  });
  return <BreakdownQuery query={query} categoryHighlight={categoryHighlight} />;
}

// meiva:ref ui/web/src/docs/frontend-components/context-breakdown.meiva.okf.md#context-point-and-failure
function BreakdownQuery({
  query,
  categoryHighlight,
}: {
  query: UseQueryResult<ContextBreakdownResponse>;
  categoryHighlight?: CategoryHighlight;
}) {
  const t = useTranslations("contextBreakdown");
  const { data, isPending, isError, error, refetch } = query;

  if (isPending) {
    return (
      <p data-testid="context-breakdown-loading" className="text-muted-foreground text-xs">
        {t("loading")}
      </p>
    );
  }
  if (isError) {

    const message = errMsg(error);

    const isHttp = httpStatus(error) !== null || message.startsWith("HTTP ");
    return (
      <div className={cn("items-start gap-2", FLEX, FLEX_COL)}>
        <p data-testid="context-breakdown-error" className="text-destructive text-xs">
          {isHttp
            ? t("failedHttp", { message })
            : t("failedUnreachable", { message })}
        </p>
        <button
          type="button"
          data-testid="context-breakdown-retry"
          onClick={() => void refetch()}
          className="rounded border border-border px-2 py-0.5 text-xs hover:bg-accent focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          {t("retry")}
        </button>
      </div>
    );
  }
  return <BreakdownContent data={data} categoryHighlight={categoryHighlight} />;
}

// meiva:ref ui/web/src/docs/frontend-components/context-breakdown.meiva.okf.md#context-category-display
function BreakdownContent({
  data,
  categoryHighlight,
}: {
  data: ContextBreakdownResponse;
  categoryHighlight?: CategoryHighlight;
}) {
  const t = useTranslations("contextBreakdown");

  const total = data.total_input_tokens;
  const categoryName = (kind: string) => {
    const messageKey = CATEGORY_MESSAGE_KEY[kind];
    return messageKey ? t(messageKey as Parameters<typeof t>[0]) : kind;
  };

  const categories: { key: string; tokens: number; estimated: boolean }[] = [];
  let systemNotesTokens = 0;
  let systemNotesEstimated = false;
  for (const c of data.categories) {
    if (c.kind === "context_note" || c.kind === "automation") {
      systemNotesTokens += c.tokens;
      systemNotesEstimated ||= c.estimated;
    } else {
      categories.push({ key: c.kind, tokens: c.tokens, estimated: c.estimated });
    }
  }
  if (systemNotesTokens > 0) {
    categories.push({ key: "system_notes", tokens: systemNotesTokens, estimated: systemNotesEstimated });
  }
  categories.sort((a, b) => b.tokens - a.tokens);

  const hasThresholds = data.max_input_tokens > 0 && data.hard_compact_tokens > 0;

  const [sectionsOpen, setSectionsOpen] = useState(false);

  if (total <= 0 && data.categories.length === 0) {
    return (
      <p data-testid="context-breakdown-empty" className="text-muted-foreground text-xs">
        {t("empty")}
      </p>
    );
  }

  return (
    <div className={cn("gap-2.5", FLEX, FLEX_COL)}>

      <p className="text-muted-foreground text-xs tabular-nums" data-testid="context-breakdown-total">
        <span className="block">
          {approx(data.estimated)}
          {formatTokens(total)}
          {data.max_input_tokens > 0 ? ` / ${formatTokens(data.max_input_tokens)}` : ""}{" "}
          {t("tokensUnit")}
        </span>
        {hasThresholds ? (
          <span className="block">
            {t("thresholds", {
              soft: formatTokens(data.soft_compact_tokens),
              hard: formatTokens(data.hard_compact_tokens),
            })}
          </span>
        ) : null}
      </p>

      <ul className={cn("gap-1", FLEX, FLEX_COL)} data-testid="context-breakdown-categories">
        {categories.map((c) => {
          const row = (
            <>
              <span
                className="size-2.5 shrink-0 rounded-[2px]"
                style={{ backgroundColor: categoryColor(c.key) }}
              />
              <span className={cn("truncate", FLEX_1, "text-left")}>{categoryName(c.key)}</span>
              <span className="shrink-0 tabular-nums text-muted-foreground">
                {approx(c.estimated)}
                {formatTokens(c.tokens)}
                {total > 0 ? ` · ${((c.tokens / total) * 100).toFixed(2)}%` : ""}
              </span>
            </>
          );
          const clickable = categoryHighlight?.has(c.key) === true;
          const active = categoryHighlight?.active === c.key;
          return clickable ? (
            <li key={c.key} className="text-xs">
              <button
                  type="button"
                  aria-pressed={active}
                  title={t("categoryToggle", { category: categoryName(c.key) })}
                  data-testid={`context-breakdown-category-${c.key}`}
                  onClick={() => categoryHighlight.onToggle(c.key)}
                  className={cn(
                    "w-full items-center gap-2 rounded px-1 py-0.5 hover:bg-muted focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
                    FLEX,
                    active && "bg-muted ring-1 ring-foreground/50",
                  )}
                >
                  {row}
              </button>
            </li>
          ) : (
            <li key={c.key} className={cn("items-center gap-2 px-1 py-0.5 text-xs", FLEX)}>
              {row}
            </li>
          );
        })}
      </ul>

      {data.sections.length > 0 ? (
        <div className="border-t border-border pt-2">

          <div className={cn("mb-1 items-center text-[11px] font-medium uppercase tracking-wide text-muted-foreground", FLEX)}>
            <button
              type="button"
              data-testid="context-breakdown-sections-toggle"
              onClick={() => setSectionsOpen((o) => !o)}
              aria-expanded={sectionsOpen}
              aria-label={
              sectionsOpen
                ? t("collapse", { target: t("sections") })
                : t("expand", { target: t("sections") })
            }
              className="rounded-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              <ChevronRight className={cn("size-3 transition-transform", sectionsOpen && "rotate-90")} />
            </button>
            {t("sections")}
          </div>
          {sectionsOpen ? <SectionRows nodes={data.sections} depth={0} /> : null}
        </div>
      ) : null}
    </div>
  );
}
