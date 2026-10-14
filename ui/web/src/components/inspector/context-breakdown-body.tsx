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

// API kind → message key (contextBreakdown.categories.*). An unknown kind
// keeps its raw name at render — fail-visible (the P4-5 review condition).
const CATEGORY_MESSAGE_KEY: Record<string, string> = {
  system_prompt: "categories.system_prompt",
  compact_summary: "categories.compact_summary",
  cluster_memory: "categories.cluster_memory",
  agent_memory: "categories.agent_memory",
  // context_note (system-note markers) + automation (machine/framework wakeups)
  // merge into one display row "System notes" (user ruling 2026-08-04) — the API
  // keeps the two kinds; only the legend merges them.
  system_notes: "categories.system_notes",
  user_input: "categories.user_input",
  agent_messages: "categories.agent_messages",

  reasoning: "categories.reasoning",
  output: "categories.output",
  tool_call: "categories.tool_call",
  tool_response: "categories.tool_response",
};

/** Lets the categories that stand for a kind of timeline block be clicked to highlight it there. */
export interface CategoryHighlight {
  /** The category whose blocks are highlighted now, if any. */
  active: string | null;
  /** Whether the category has blocks on the timeline (a row without them is not a button). */
  has: (category: string) => boolean;
  onToggle: (category: string) => void;
}

/** The HTTP status of a gateway answer (an `ApiError`), null for any other error. */
function httpStatus(error: unknown): number | null {
  const status = error instanceof Error ? (error as { status?: unknown }).status : undefined;
  return typeof status === "number" ? status : null;
}

/** The breakdown as a standalone card — the composer panel's body rendered
 * inline (run page, task #4023 P4-3), chart-side above the timeline. Demo
 * parity: the pilot-w1 card caps at 520px (`#cbd` in its stylesheet).
 *
 * Without `at` it shows the agent's current context. With `at` (the run timeline's point: a
 * message index, null while the agent has made no request) it shows what the LLM request at or
 * after that message held, titled with its session and time. */
export function ContextBreakdownCard({
  agentId,
  at,
  categoryHighlight,
}: {
  agentId: number;
  at?: number | null;
  categoryHighlight?: CategoryHighlight;
}) {
  const t = useTranslations("contextBreakdown");
  const pointed = at !== undefined;
  const point = useQuery({
    queryKey: ["run-timeline-context", agentId, at] as const,
    queryFn: () => api.getRunTimelineContext(agentId, at ?? 0),
    enabled: pointed && at !== null,
    staleTime: 10_000,
    // Panning moves the point often: the card keeps the last one's numbers until the next arrives.
    placeholderData: keepPreviousData,
  });
  // The gateway answers 404 when the agent has made no LLM request: that is an empty point, not a failure.
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

/** The breakdown itself, fetched lazily per agent. Shared by the composer's
 * anchored panel (`ContextButton`) and the run page's card
 * (`ContextBreakdownCard`). Thresholds are read from the response — the
 * endpoint mirrors the token-usage values (`resolve_context_budget`), so the
 * body carries no live meter props; the collapsed `ContextMeter` keeps its
 * own live values. */
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
    // This body mounts when the breakdown popover opens — pull fresh on every
    // open instead of showing a cached snapshot until the staleTime lapses.
    refetchOnMount: "always",
  });
  return <BreakdownQuery query={query} categoryHighlight={categoryHighlight} />;
}

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
    // The failure renders inside the panel (no toast, no dialog), with the
    // actual error so a broken endpoint is diagnosable from the UI. api.ts
    // prefixes every non-OK response with "HTTP <status>: …", which separates
    // a server-side failure from never reaching the gateway at all.
    const message = errMsg(error);
    // An ApiError is a gateway answer whatever its message (a JSON `detail` replaces the "HTTP <status>" prefix).
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

function BreakdownContent({
  data,
  categoryHighlight,
}: {
  data: ContextBreakdownResponse;
  categoryHighlight?: CategoryHighlight;
}) {
  const t = useTranslations("contextBreakdown");
  // The anchor the percentages are relative to: the real/anchored input total
  // (0 while no LLM request has run).
  const total = data.total_input_tokens;
  const categoryName = (kind: string) => {
    const messageKey = CATEGORY_MESSAGE_KEY[kind];
    return messageKey ? t(messageKey as Parameters<typeof t>[0]) : kind;
  };
  // The legend's display rows. `context_note` (system-note markers) and
  // `automation` (machine/framework wakeup messages) merge into one "System
  // notes" row (user ruling 2026-08-04 — both are system-injected content and
  // would otherwise render as two identically-named rows); the API keeps the
  // two kinds, only the legend merges them. The merged row participates in the
  // share sort like any other — descending, stable, so equal shares keep the
  // backend's canonical CATEGORY_ORDER.
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
  // Thresholds come from the response itself (the endpoint mirrors the
  // token-usage values) — see ContextBreakdownResponse's field docs.
  const hasThresholds = data.max_input_tokens > 0 && data.hard_compact_tokens > 0;
  // The whole section block is collapsible like a single section row — chevron
  // disclosure, collapsed by default so a tall breakdown stays compact.
  const [sectionsOpen, setSectionsOpen] = useState(false);

  // The endpoint's tolerated empty shape (no checkpoint yet / checkpoint read
  // failure): explicit no-data copy instead of a bare "0 tokens" readout.
  if (total <= 0 && data.categories.length === 0) {
    return (
      <p data-testid="context-breakdown-empty" className="text-muted-foreground text-xs">
        {t("empty")}
      </p>
    );
  }

  return (
    <div className={cn("gap-2.5", FLEX, FLEX_COL)}>
      {/* Occupancy summary */}
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

      {/* Category legend. */}
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

      {/* System-prompt sections — a recursive tree: any section over the
          gateway's split threshold carries `children`, collapsed by default and
          drilled in on click. */}
      {data.sections.length > 0 ? (
        <div className="border-t border-border pt-2">
          {/* The section block's own disclosure — the same chevron button as a
              section row's, collapsed by default (the rows below only render
              once expanded). */}
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
