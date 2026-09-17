"use client";

// /insights/compare — the multi-agent run-timeline view (task #3735, user
// ruling B): 2–3 agents' lanes stacked on one shared [from, to] window, with
// message arrows for agent-to-agent deliveries. Entered from a single view's
// "Compare agents" button or directly via ?agents=<id>,<id>[,<id>]; a cold or
// malformed entry lands on the selector instead. The lanes themselves live in
// _view.tsx (this file owns parsing, the shell, and the selector).

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type ReactNode, useEffect, useMemo, useState } from "react";

import { COMPARE_LANE_HUES } from "@/components/run-timeline/compare-arrows";
import { buttonVariants } from "@/components/ui/button";
import { compareHref } from "@/lib/compare-links";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0 } from "@/lib/layout";
import { useFleetAgents } from "@/lib/use-fleet-agents";
import { cn } from "@/lib/utils";

import { CompareView } from "./_view";

// KEEP (task #3696 exception inventory): the 2–3 lane bound is the v1 compare
// scope (user ruling B, notice #2682) — >3 lanes wait for the P3b full-fleet
// selector and its layout/performance work.
const MIN_COMPARE_AGENTS = 2;
const MAX_COMPARE_AGENTS = 3;

export interface CompareSelection {
  ids: number[];
  /** Some token is not a valid non-negative integer id. */
  invalid: boolean;
  /** The raw query value, prefilled into the selector for repair. */
  raw: string;
}

/** Parse the `agents` query value: order-preserving, de-duplicated; a
 *  malformed token flags the selection so the page falls back to the selector
 *  instead of guessing at the intent. */
export function parseCompareAgents(raw: string): CompareSelection {
  const ids: number[] = [];
  let invalid = false;
  for (const token of raw.split(",")) {
    const trimmed = token.trim();
    if (trimmed === "") continue;
    const value = Number(trimmed);
    if (!Number.isInteger(value) || value < 0) {
      invalid = true;
      continue;
    }
    if (!ids.includes(value)) ids.push(value);
  }
  return { ids, invalid, raw };
}

export function isComparableView(selection: CompareSelection): boolean {
  return (
    !selection.invalid &&
    selection.ids.length >= MIN_COMPARE_AGENTS &&
    selection.ids.length <= MAX_COMPARE_AGENTS
  );
}

export default function ComparePage({
  searchParams,
}: {
  searchParams: Promise<{ agents?: string | string[] }>;
}) {
  const [selection, setSelection] = useState<CompareSelection | null>(null);
  // Display names ride the shared live roster (no extra fetch); terminated
  // agents outside it fall back to their "#id" in the labels.
  const roster = useFleetAgents();
  const names = useMemo(
    () => new Map(roster.map((agent) => [agent.agent_id, agent.label])),
    [roster],
  );

  useEffect(() => {
    let cancelled = false;
    searchParams
      .then((params) => {
        const raw = typeof params.agents === "string" ? params.agents : "";
        if (!cancelled) setSelection(parseCompareAgents(raw));
      })
      .catch(() => {
        if (!cancelled) setSelection(parseCompareAgents(""));
      });
    return () => {
      cancelled = true;
    };
  }, [searchParams]);

  if (selection === null) return <CompareShell>{null}</CompareShell>;
  if (!isComparableView(selection)) {
    return (
      <CompareShell>
        <CompareSelector initialValue={selection.raw} names={names} />
      </CompareShell>
    );
  }
  return (
    <CompareShell chips={<CompareChips agents={selection.ids} names={names} />}>
      <CompareView agents={selection.ids} names={names} />
    </CompareShell>
  );
}

function CompareShell({ chips, children }: { chips?: ReactNode; children: ReactNode }) {
  const t = useTranslations("runTimeline");
  return (
    <main id="main-content" className={cn(FLEX, FLEX_1, MIN_H_0, FLEX_COL)}>
      <header className={cn("items-center gap-3 border-b border-border px-4 py-2", FLEX)}>
        <Link href="/insights" className={buttonVariants({ size: "sm", variant: "ghost" })}>
          {t("backToInsights")}
        </Link>
        <h1 className="truncate text-sm font-semibold">{t("compareTitle")}</h1>
        {chips ? <div className={cn(FLEX, "flex-wrap items-center gap-1")}>{chips}</div> : null}
      </header>
      <div className="overflow-y-auto">
        <div className="mx-auto max-w-6xl space-y-4 p-6">{children}</div>
      </div>
    </main>
  );
}

function CompareChips({
  agents,
  names,
}: {
  agents: number[];
  names: Map<number, string | null>;
}) {
  const t = useTranslations("runTimeline");
  return (
    <>
      {agents.map((agentId, index) => {
        const name = names.get(agentId);
        return (
          <Link
            key={agentId}
            href={`/insights/run/${agentId}`}
            title={t("laneOpenSingle")}
            className={cn(
              FLEX,
              "items-center gap-1.5 rounded border border-border px-2 py-0.5 text-xs hover:bg-muted",
            )}
          >
            <span
              className="size-2 shrink-0 rounded-full"
              style={{ background: COMPARE_LANE_HUES[index % COMPARE_LANE_HUES.length] }}
              aria-hidden
            />
            <span className="truncate">{name ?? `#${agentId}`}</span>
            {name ? (
              <span className="font-mono text-[10px] text-muted-foreground">#{agentId}</span>
            ) : null}
          </Link>
        );
      })}
    </>
  );
}

function CompareSelector({
  initialValue,
  names,
}: {
  initialValue: string;
  names: Map<number, string | null>;
}) {
  const t = useTranslations("runTimeline");
  const router = useRouter();
  const [value, setValue] = useState(initialValue);
  const selection = parseCompareAgents(value);
  const complete = isComparableView(selection);
  return (
    <form
      className="space-y-3 rounded border border-border bg-card p-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (complete) router.push(compareHref(selection.ids));
      }}
    >
      <p className="text-xs text-muted-foreground">{t("compareSelectorDescription")}</p>
      <label className="grid max-w-md gap-1 text-xs text-muted-foreground">
        {t("compareAgentsLabel")}
        <input
          aria-label={t("compareAgentsLabel")}
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder="405, 228"
          className="rounded border border-border bg-background px-2 py-1 font-mono text-xs text-foreground"
        />
      </label>
      {selection.ids.length > 0 ? (
        <div className={cn(FLEX, "flex-wrap items-center gap-1")}>
          {selection.ids.map((agentId) => (
            <span key={agentId} className="rounded border border-border px-2 py-0.5 font-mono text-xs">
              {names.get(agentId) ?? `#${agentId}`} · #{agentId}
            </span>
          ))}
        </div>
      ) : null}
      <p className="text-xs text-muted-foreground">{t("compareAgentsHint")}</p>
      {value.trim() !== "" && !complete ? (
        <p role="alert" className="text-xs text-destructive">
          {t("compareSelectorInvalid")}
        </p>
      ) : null}
      <button type="submit" disabled={!complete} className={buttonVariants({ size: "sm" })}>
        {t("compareOpen")}
      </button>
    </form>
  );
}
