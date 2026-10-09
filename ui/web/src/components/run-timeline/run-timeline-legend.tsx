"use client";

// The legend under the run timeline: one button per kind of block. Pressing one highlights every
// block of that kind (the rest fades); pressing it again clears. While an inbound kind is
// highlighted and its blocks come from several senders, a select narrows the highlight to one.

import { useTranslations } from "next-intl";

import { FLEX } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

import { BLOCK_CLASSES, classColor, type BlockClass, type Highlight } from "./model/timeline-model";

export function RunTimelineLegend({
  highlight,
  onHighlight,
  classLabel,
  sources,
  sourceLabel,
}: {
  highlight: Highlight | null;
  onHighlight: (highlight: Highlight | null) => void;
  classLabel: Record<BlockClass, string>;
  /** The distinct senders of the highlighted inbound kind. */
  sources: readonly string[];
  sourceLabel: (source: string) => string;
}) {
  const t = useTranslations("runTimeline");
  return (
    <>
      <ul
        aria-label={t("legendLabel")}
        data-testid="run-timeline-legend"
        className={cn(FLEX, "flex-wrap gap-x-1 gap-y-1 pl-[88px] text-[10px] text-muted-foreground")}
      >
        {BLOCK_CLASSES.map((kind) => {
          const on = highlight?.cls === kind;
          return (
            <li key={kind}>
              <button
                type="button"
                aria-pressed={on}
                data-testid={`run-timeline-legend-${kind}`}
                title={t("legendToggle", { kind: classLabel[kind] })}
                onClick={() => onHighlight(on ? null : { cls: kind, source: null })}
                className={cn(
                  FLEX,
                  "items-center gap-1 rounded px-1.5 py-0.5 hover:bg-muted hover:text-foreground",
                  on && "bg-muted text-foreground ring-1 ring-foreground/50",
                )}
              >
                <span
                  aria-hidden="true"
                  className="inline-block h-2.5 w-2.5 rounded-sm"
                  style={{ background: classColor(kind) }}
                />
                {classLabel[kind]}
              </button>
            </li>
          );
        })}
      </ul>
      {highlight !== null && sources.length > 1 ? (
        <label className={cn(FLEX, "items-center gap-1.5 pl-[88px] text-[10px] text-muted-foreground")}>
          {t("legendSource")}
          <select
            data-testid="run-timeline-source-select"
            value={highlight.source ?? ""}
            onChange={(event) => onHighlight({ cls: highlight.cls, source: event.target.value || null })}
            className="rounded border border-border bg-background px-1 py-0.5 text-[10px] text-foreground"
          >
            <option value="">{t("sourceAll")}</option>
            {sources.map((source) => (
              <option key={source} value={source}>
                {sourceLabel(source)}
              </option>
            ))}
          </select>
        </label>
      ) : null}
    </>
  );
}
