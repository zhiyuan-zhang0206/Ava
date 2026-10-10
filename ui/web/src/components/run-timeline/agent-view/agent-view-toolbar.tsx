"use client";

// The agent view's controls: add an agent by id, how many understanding-tree levels to draw, and
// which context bars.

import { useTranslations } from "next-intl";
import { useState } from "react";

import { buttonVariants } from "@/components/ui/button";
import { FLEX } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

import type { UnitHeights } from "../canvas/run-timeline-paint";
import { MAX_ARROWS_CAP, parseArrowLimit } from "../model/timeline-links";

const HEIGHT_OPTIONS: readonly UnitHeights[] = ["equal", "tokens"];
const FIELD = "rounded border border-border bg-background px-2 py-1 font-mono text-xs text-foreground";

export function AgentViewToolbar({
  agentIds,
  onAdd,
  levels,
  maxLevels,
  onLevels,
  contextSize,
  onContextSize,
  unitHeights,
  onUnitHeights,
  showUser,
  onShowUser,
  showOther,
  onShowOther,
  maxArrows,
  onMaxArrows,
}: {
  agentIds: readonly number[];
  onAdd: (agent: number) => void;
  /** How many levels are drawn, counted from the topmost; null draws them all. */
  levels: number | null;
  /** The most levels any loaded agent has. */
  maxLevels: number;
  onLevels: (levels: number | null) => void;
  contextSize: boolean;
  onContextSize: (on: boolean) => void;
  unitHeights: UnitHeights;
  onUnitHeights: (heights: UnitHeights) => void;
  /** Whether the arrows between agents (and the Other agents group) are shown. */
  /** The User group and the arrows with the user; the Other agents group and the arrows with agents outside the view. */
  showUser: boolean;
  onShowUser: (on: boolean) => void;
  showOther: boolean;
  onShowOther: (on: boolean) => void;
  /** The most arrows shown at once: the last valid number typed. */
  maxArrows: number;
  onMaxArrows: (max: number) => void;
}) {
  const t = useTranslations("runTimeline");
  const [draft, setDraft] = useState("");
  const [limitDraft, setLimitDraft] = useState(String(maxArrows));
  const limitInvalid = parseArrowLimit(limitDraft.trim()) === null;
  const parsed = Number(draft);
  const valid = draft.trim() !== "" && Number.isInteger(parsed) && parsed >= 0 && !agentIds.includes(parsed);
  const heightLabel: Record<UnitHeights, string> = { equal: t("heightEqual"), tokens: t("heightTokens") };
  return (
    <div className={cn(FLEX, "flex-wrap items-end gap-x-4 gap-y-2")} data-testid="agent-view-toolbar">
      <form
        className={cn(FLEX, "items-end gap-2")}
        onSubmit={(event) => {
          event.preventDefault();
          if (!valid) return;
          onAdd(parsed);
          setDraft("");
        }}
      >
        <label className="grid gap-1 text-xs text-muted-foreground">
          {t("addAgentLabel")}
          <input
            type="number"
            min="0"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            data-testid="agent-view-add-input"
            className={cn(FIELD, "w-28")}
          />
        </label>
        <button
          type="submit"
          disabled={!valid}
          data-testid="agent-view-add"
          className={cn(buttonVariants({ size: "sm" }), "h-7")}
        >
          {t("addAgent")}
        </button>
      </form>
      <label className="grid gap-1 text-xs text-muted-foreground" title={t("levelsTitle")}>
        {t("levelsLabel")}
        <select
          value={levels === null ? "all" : String(levels)}
          onChange={(event) => onLevels(event.target.value === "all" ? null : Number(event.target.value))}
          data-testid="agent-view-levels"
          className={FIELD}
        >
          <option value="all">{t("levelsAll")}</option>
          {Array.from({ length: maxLevels + 1 }, (_, count) => (
            <option key={count} value={count}>
              {count}
            </option>
          ))}
        </select>
      </label>
      <label className="grid gap-1 text-xs text-muted-foreground" title={t("heightTitle")}>
        {t("heightLabel")}
        <select
          value={unitHeights}
          onChange={(event) => {
            const next = HEIGHT_OPTIONS.find((option) => option === event.target.value);
            if (next === undefined) throw new Error(`unknown messages height option: ${event.target.value}`);
            onUnitHeights(next);
          }}
          data-testid="agent-view-heights"
          className={FIELD}
        >
          {HEIGHT_OPTIONS.map((option) => (
            <option key={option} value={option}>
              {heightLabel[option]}
            </option>
          ))}
        </select>
      </label>
      <label className={cn(FLEX, "items-center gap-1.5 pb-1 text-xs text-muted-foreground")}>
        <input
          type="checkbox"
          checked={contextSize}
          onChange={(event) => onContextSize(event.target.checked)}
          data-testid="agent-view-context-size"
        />
        {t("contextSizeLabel")}
      </label>
      <label className={cn(FLEX, "items-center gap-1.5 pb-1 text-xs text-muted-foreground")}>
        <input
          type="checkbox"
          checked={showUser}
          onChange={(event) => onShowUser(event.target.checked)}
          data-testid="agent-view-interactions-user"
        />
        {t("userInteractionsLabel")}
      </label>
      <label className={cn(FLEX, "items-center gap-1.5 pb-1 text-xs text-muted-foreground")}>
        <input
          type="checkbox"
          checked={showOther}
          onChange={(event) => onShowOther(event.target.checked)}
          data-testid="agent-view-interactions-other"
        />
        {t("otherInteractionsLabel")}
      </label>
      <label className="grid gap-1 text-xs text-muted-foreground" title={t("maxArrowsTitle")}>
        {t("maxArrowsLabel")}
        <input
          type="text"
          inputMode="numeric"
          value={limitDraft}
          aria-invalid={limitInvalid}
          aria-describedby={limitInvalid ? "agent-view-max-arrows-error" : undefined}
          onChange={(event) => {
            const text = event.target.value.trim();
            setLimitDraft(event.target.value);
            // Only a valid number takes effect; an invalid one leaves the last valid limit in force.
            const parsed = parseArrowLimit(text);
            if (parsed !== null) onMaxArrows(parsed);
          }}
          data-testid="agent-view-max-arrows"
          className={cn(FIELD, "w-20", limitInvalid && "border-destructive text-destructive")}
        />
        {limitInvalid ? (
          <span id="agent-view-max-arrows-error" role="alert" className="text-destructive" data-testid="agent-view-max-arrows-error">
            {t("maxArrowsInvalid", { max: MAX_ARROWS_CAP })}
          </span>
        ) : null}
      </label>
    </div>
  );
}
