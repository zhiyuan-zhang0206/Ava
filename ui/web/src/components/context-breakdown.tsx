"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronRight, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import {
  CONTEXT_METER_WIDTH_CLASS,
  ContextMeter,
  formatTokens,
  resolveContextMeterWidth,
} from "@/components/context-meter";
import { api } from "@/lib/api";
import { errMsg } from "@/lib/errors";
import type { ContextBreakdownResponse, ContextSection } from "@/lib/types";
import { useUserSettings } from "@/lib/use-user-settings";
import { cn } from "@/lib/utils";
import { FLEX, FLEX_1, FLEX_COL, MIN_W_0 } from "@/lib/layout";

// Fixed, distinguishable colors per context category (a closed set). Mid-tones
// that read on both the light and dark popover surface.
const CATEGORY_COLOR: Record<string, string> = {
  system_prompt: "#6366f1",
  compact_summary: "#a855f7",
  cluster_memory: "#0ea5e9",
  agent_memory: "#14b8a6",
  context_note: "#64748b",
  system_notes: "#64748b",
  user_input: "#22c55e",
  agent_messages: "#84cc16",
  automation: "#f97316",
  reasoning: "#ec4899",
  output: "#3b82f6",
  tool_call: "#f59e0b",
  tool_response: "#ef4444",
};

const CATEGORY_LABEL: Record<string, string> = {
  system_prompt: "System prompt",
  compact_summary: "Compact summary",
  cluster_memory: "Cluster memory",
  agent_memory: "Agent memory",
  // context_note (system-note markers) + automation (machine/framework wakeups)
  // merge into one display row "System notes" (user ruling 2026-08-04) — the API
  // keeps the two kinds; only the legend merges them.
  system_notes: "System notes",
  user_input: "User input",
  agent_messages: "Agent messages",

  reasoning: "Reasoning",
  output: "Text output",
  tool_call: "Tool calls",
  tool_response: "Tool responses",
};

function categoryColor(kind: string): string {
  return CATEGORY_COLOR[kind] ?? "#94a3b8";
}

function categoryLabel(kind: string): string {
  return CATEGORY_LABEL[kind] ?? kind;
}

export interface ContextButtonProps {
  /** The active agent, or null. Without one there is nothing to break down, so
   * the readout renders non-interactively. */
  agentId: number | null;
  /** Controlled expansion state. The composer owns it (single popover state)
   * so at most one of its upward popovers — this panel / the slash-command
   * dropdown — is open at a time: opening one closes the other. */
  open: boolean;
  onOpenChange: (open: boolean) => void;
  contextTokens: number;
  maxContextTokens: number;
  softCompactTokens: number;
  hardCompactTokens: number;
}

/** The composer's context readout as a button: the inline `ContextMeter` gauge
 * that, when clicked, expands the breakdown panel in place (lazy-loaded on
 * expand).
 *
 * Deliberately NOT a modal: the panel is absolutely positioned against the
 * composer's meta row (composer.tsx marks that row `relative` as the anchor —
 * same contract as SlashAutocomplete's dropdown) and grows *upward*
 * (`bottom-full`) over the bottom of the timeline. The composer below stays
 * fully visible and interactive, nothing reflows, and the rest of the page is
 * never blocked. Escape, an outside click, the close button, or clicking the
 * meter again collapses it. The open state is controlled by the composer
 * (single popover state — mutually exclusive with the slash dropdown). */
export function ContextButton(props: ContextButtonProps) {
  const { agentId, open, onOpenChange, ...meter } = props;
  const buttonRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const { settings } = useUserSettings();
  const barWidthClassName =
    CONTEXT_METER_WIDTH_CLASS[resolveContextMeterWidth(settings["display.context_meter_width"])];

  // Escape / outside pointer-down collapse the panel. Document-level listeners
  // exist only while open; the closing outside click still lands on its target
  // (non-modal — nothing is swallowed). An Escape a lower layer already
  // consumed (defaultPrevented — e.g. the composer closing the slash dropdown)
  // is skipped so one keypress never closes two layers.
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.defaultPrevented) onOpenChange(false);
    };
    const onPointerDown = (e: PointerEvent) => {
      const t = e.target as Node;
      if (panelRef.current?.contains(t) || buttonRef.current?.contains(t)) return;
      onOpenChange(false);
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("pointerdown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("pointerdown", onPointerDown);
    };
  }, [open, onOpenChange]);

  if (agentId == null) {
    return <ContextMeter {...meter} barWidthClassName={barWidthClassName} />;
  }

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        data-testid="context-meter-button"
        onClick={() => onOpenChange(!open)}
        aria-expanded={open}
        className={cn("rounded-sm text-left hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring", MIN_W_0)}
      >
        <ContextMeter {...meter} barWidthClassName={barWidthClassName} />
      </button>
      {open ? (
        // bottom-full anchors the panel's bottom to the meta row's top, so it
        // can only occupy space *above* the composer (over the timeline). The
        // max-height + internal scroll keep a tall breakdown inside the
        // viewport instead of growing past it. Width is shrink-to-fit
        // (no `w-*`): the panel sizes to its content — min-w-80 is a floor so
        // a sparse breakdown isn't a sliver, max-w-[...] a ceiling so one
        // pathologically long category/section name can't push the panel
        // past the viewport. Inside that range nothing truncates; only past
        // the ceiling do the `truncate` rows below ellipsis.
        <div
          ref={panelRef}
          role="region"
          aria-label="Context breakdown"
          data-testid="context-breakdown-panel"
          // overflow-x-hidden is load-bearing, not decorative: per the CSS
          // overflow spec, an element with overflow-y other than "visible"
          // has its overflow-x computed value forced from "visible" to
          // "auto" too — the two axes can't be set independently that way —
          // so overflow-y-auto alone silently made this panel horizontally
          // scrollable once content ran past the max-width ceiling, instead
          // of the intended pure vertical scroll with only the ceiling-
          // exceeding rows truncating in place.
          //
          // whitespace-normal resets an inherited `white-space: nowrap`: the
          // composer's meta row wraps this panel in a `truncate` span (for
          // the *collapsed* meter's own text) — nowrap is an inherited
          // property, so without this reset every paragraph in here that
          // isn't itself `truncate` (the summary line, the description)
          // would refuse to wrap and get silently clipped by overflow-x-
          // hidden instead of flowing across lines like the rest of the
          // panel.
          className="absolute bottom-full left-0 z-50 mb-2 min-w-80 max-w-[min(28rem,90vw)] max-h-[50vh] overflow-x-hidden overflow-y-auto whitespace-normal rounded-md border border-border bg-popover p-2.5 text-popover-foreground shadow-md"
        >
          <div className={cn("mb-2 items-start justify-between gap-2", FLEX)}>
            <div>
              <p className="text-sm font-semibold">Context breakdown</p>
              <p className="text-muted-foreground text-xs">
                How this agent&apos;s context window is spent.
              </p>
            </div>
            <button
              type="button"
              data-testid="context-breakdown-close"
              aria-label="Close context breakdown"
              onClick={() => onOpenChange(false)}
              className="shrink-0 rounded-sm p-0.5 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              <X className="size-3.5" />
            </button>
          </div>
          <ContextBreakdownBody agentId={agentId} {...meter} />
        </div>
      ) : null}
    </>
  );
}

function ContextBreakdownBody({
  agentId,
  maxContextTokens,
  softCompactTokens,
  hardCompactTokens,
}: {
  agentId: number;
} & Omit<ContextButtonProps, "agentId" | "contextTokens" | "open" | "onOpenChange">) {
  const { data, isPending, isError, error, refetch } = useQuery({
    queryKey: ["context-breakdown", agentId] as const,
    queryFn: () => api.getContextBreakdown(agentId),
    staleTime: 10_000,
    // This body mounts when the breakdown popover opens — pull fresh on every
    // open instead of showing a cached snapshot until the staleTime lapses.
    refetchOnMount: "always",
  });

  if (isPending) {
    return (
      <p data-testid="context-breakdown-loading" className="text-muted-foreground text-xs">
        Loading…
      </p>
    );
  }
  if (isError) {
    // The failure renders inside the panel (no toast, no dialog), with the
    // actual error so a broken endpoint is diagnosable from the UI. api.ts
    // prefixes every non-OK response with "HTTP <status>: …", which separates
    // a server-side failure from never reaching the gateway at all.
    const message = errMsg(error);
    const isHttp = message.startsWith("HTTP ");
    return (
      <div className={cn("items-start gap-2", FLEX, FLEX_COL)}>
        <p data-testid="context-breakdown-error" className="text-destructive text-xs">
          {isHttp
            ? `Failed to load the context breakdown — the server returned an error: ${message}`
            : `Failed to load the context breakdown — could not reach the gateway: ${message}`}
        </p>
        <button
          type="button"
          data-testid="context-breakdown-retry"
          onClick={() => void refetch()}
          className="rounded border border-border px-2 py-0.5 text-xs hover:bg-accent focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          Retry
        </button>
      </div>
    );
  }
  return (
    <BreakdownContent
      data={data}
      maxContextTokens={maxContextTokens}
      softCompactTokens={softCompactTokens}
      hardCompactTokens={hardCompactTokens}
    />
  );
}

function BreakdownContent({
  data,
  maxContextTokens,
  softCompactTokens,
  hardCompactTokens,
}: {
  data: ContextBreakdownResponse;
  maxContextTokens: number;
  softCompactTokens: number;
  hardCompactTokens: number;
}) {
  // The anchor the percentages are relative to: the provider truth when a call
  // has run, else the chars/4 estimate.
  const total = data.total_input_tokens > 0 ? data.total_input_tokens : data.estimated_total;
  // The legend's display rows. `context_note` (system-note markers) and
  // `automation` (machine/framework wakeup messages) merge into one "System
  // notes" row (user ruling 2026-08-04 — both are system-injected content and
  // would otherwise render as two identically-named rows); the API keeps the
  // two kinds, only the legend merges them. The merged row participates in the
  // share sort like any other — descending, stable, so equal shares keep the
  // backend's canonical CATEGORY_ORDER.
  const categories: { key: string; tokens: number }[] = [];
  let systemNotesTokens = 0;
  for (const c of data.categories) {
    if (c.kind === "context_note" || c.kind === "automation") {
      systemNotesTokens += c.tokens;
    } else {
      categories.push({ key: c.kind, tokens: c.tokens });
    }
  }
  if (systemNotesTokens > 0) {
    categories.push({ key: "system_notes", tokens: systemNotesTokens });
  }
  categories.sort((a, b) => b.tokens - a.tokens);
  const hasThresholds = maxContextTokens > 0 && hardCompactTokens > 0;
  // The whole section block is collapsible like a single section row — chevron
  // disclosure, collapsed by default so a tall breakdown stays compact.
  const [sectionsOpen, setSectionsOpen] = useState(false);

  // The endpoint's tolerated empty shape (no checkpoint yet / checkpoint read
  // failure): explicit no-data copy instead of a bare "0 tokens" readout.
  if (total <= 0 && data.categories.length === 0) {
    return (
      <p data-testid="context-breakdown-empty" className="text-muted-foreground text-xs">
        No context recorded yet — the breakdown appears after the agent&apos;s first turn.
      </p>
    );
  }

  return (
    <div className={cn("gap-2.5", FLEX, FLEX_COL)}>
      {/* Occupancy summary */}
      <p className="text-muted-foreground text-xs tabular-nums" data-testid="context-breakdown-total">
        <span className="block">
          {formatTokens(total)}
          {maxContextTokens > 0 ? ` / ${formatTokens(maxContextTokens)}` : ""} tokens
        </span>
        {hasThresholds ? (
          <span className="block">
            wind-down {formatTokens(softCompactTokens)} · auto-compact{" "}
            {formatTokens(hardCompactTokens)}
          </span>
        ) : null}
      </p>

      {/* Category legend. */}
      <ul className={cn("gap-1", FLEX, FLEX_COL)} data-testid="context-breakdown-categories">
        {categories.map((c) => (
          <li key={c.key} className={cn("items-center gap-2 text-xs", FLEX)}>
            <span
              className="size-2.5 shrink-0 rounded-[2px]"
              style={{ backgroundColor: categoryColor(c.key) }}
            />
            <span className={cn("truncate", FLEX_1)}>{categoryLabel(c.key)}</span>
            <span className="shrink-0 tabular-nums text-muted-foreground">
              {formatTokens(c.tokens)}
              {total > 0 ? ` · ${((c.tokens / total) * 100).toFixed(2)}%` : ""}
            </span>
          </li>
        ))}
      </ul>

      {/* System-prompt sections — a recursive tree: any section over the
          gateway's split threshold carries `children`, collapsed by default and
          drilled in on click. */}
      {data.sections.length > 0 ? (
        <div className="border-t border-border pt-2">
          {/* The section block's own disclosure — the same chevron button as a
              section row's, collapsed by default (the rows below only render
              once expanded). */}
          <div className={cn("mb-1 items-center text-xs font-medium uppercase tracking-wide text-muted-foreground", FLEX)}>
            <button
              type="button"
              data-testid="context-breakdown-sections-toggle"
              onClick={() => setSectionsOpen((o) => !o)}
              aria-expanded={sectionsOpen}
              aria-label={`${sectionsOpen ? "Collapse" : "Expand"} System prompt sections`}
              className="rounded-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              <ChevronRight className={cn("size-3 transition-transform", sectionsOpen && "rotate-90")} />
            </button>
            System prompt sections
          </div>
          {sectionsOpen ? <SectionRows nodes={data.sections} depth={0} /> : null}
        </div>
      ) : null}
    </div>
  );
}

/** One level of the section tree. `depth` drives the indent (inline padding, so
 * no dynamically-composed Tailwind class); the top level carries the testid the
 * panel test keys on. */
function SectionRows({ nodes, depth }: { nodes: ContextSection[]; depth: number }) {
  return (
    <ul
      className={cn("gap-0.5", FLEX, FLEX_COL)}
      data-testid={depth === 0 ? "context-breakdown-sections" : undefined}
    >
      {nodes.map((node, i) => (
        <SectionRow key={`${node.name}-${i}`} node={node} depth={depth} />
      ))}
    </ul>
  );
}

/** A single section row. A node with `children` gets a disclosure toggle; a
 * leaf gets none — its label starts at the same left edge as sibling chevrons
 * (no spacer). The chevron sits flush against the label (no gap). `min-w-0` +
 * `truncate` keep even deep indentation from forcing horizontal scroll. */
function SectionRow({ node, depth }: { node: ContextSection; depth: number }) {
  const [open, setOpen] = useState(false);
  const children = node.children ?? [];
  const hasChildren = children.length > 0;
  return (
    <li>
      <div className={cn("items-center text-xs", FLEX)} style={{ paddingLeft: depth * 12 }}>
        {hasChildren ? (
          <button
            type="button"
            onClick={() => setOpen((o) => !o)}
            aria-expanded={open}
            aria-label={`${open ? "Collapse" : "Expand"} ${node.name}`}
            className="shrink-0 rounded-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            <ChevronRight className={cn("size-3 transition-transform", open && "rotate-90")} />
          </button>
        ) : null}
        <span className={cn("truncate text-muted-foreground", MIN_W_0, FLEX_1)}>{node.name}</span>
        <span className="ml-1 shrink-0 tabular-nums text-muted-foreground">
          {formatTokens(node.tokens)}
        </span>
      </div>
      {open && hasChildren ? <SectionRows nodes={children} depth={depth + 1} /> : null}
    </li>
  );
}
