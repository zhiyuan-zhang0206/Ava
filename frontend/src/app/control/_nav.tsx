"use client";

// Left anchor-nav for the two vertical anchor-nav pages (Control and Insights).
// Two levels — section, then its sub-anchors — mirroring the caller's section
// list, with Presets' subs derived dynamically from the presets query cache
// (one link per preset; a no-op on lists without a Presets section, e.g.
// Insights). Clicking jumps the target to the top of the scroll container
// (instant — this is in-page navigation, not a scroll gesture worth animating)
// and writes the URL hash; a scroll-spy highlights whichever anchor is
// currently near the top. On narrow viewports the whole nav collapses into a
// "Jump to…" disclosure. The page-level back-to-conversation link lives in the
// page header (page.tsx), not here.
//
// The sections list, the scroll-container id it spies on, and its aria-label
// are props so one component drives both pages.

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "@/lib/api";
import { useBreakpoint } from "@/lib/breakpoint";
import type { PresetView } from "@/lib/types";

import {
  controlAnchorIds,
  type ControlSectionDef,
  PRESETS_QUERY_KEY,
  presetAnchorId,
} from "./_sections";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0 } from "@/lib/layout";
import { cn } from "@/lib/utils";

// The active anchor is the LAST one (document order = vertical order) whose top
// has scrolled above this line, measured from the scroll container's top edge.
const SPY_OFFSET_PX = 96;

/**
 * The active anchor for a set of document-ordered ids and a threshold `line`:
 * the last id whose top is at/above the line. Because a wrapping parent
 * section's own top sits above its sub-anchors' tops, this advances from the
 * parent (config) to a sub (config-llm) as each sub's top passes the line —
 * which an intersection-set approach can't, since the parent element keeps
 * intersecting the top band throughout. Exported for testing.
 */
export function pickActiveAnchor(
  ids: readonly string[],
  line: number,
  topOf: (id: string) => number | null,
): string {
  let current = ids[0] ?? "";
  for (const id of ids) {
    const top = topOf(id);
    if (top !== null && top <= line) current = id;
  }
  return current;
}

/**
 * The section list the nav renders: the caller's base list with Presets'
 * sub-anchors filled in from the presets cache (one per preset). The query is
 * `enabled: false` — the Presets section owns the fetch; this observer only
 * reads the cache and re-renders when it changes, so the nav adds no request
 * of its own. Lists without a Presets section (Insights) pass through
 * unchanged — the map only rewrites the `presets` entry.
 */
function useControlSections(base: readonly ControlSectionDef[]): ControlSectionDef[] {
  const { data: presets } = useQuery<PresetView[]>({
    queryKey: PRESETS_QUERY_KEY,
    queryFn: api.listPresets,
    enabled: false,
  });
  return useMemo(() => {
    if (!presets || presets.length === 0) return base as ControlSectionDef[];
    return base.map((s) =>
      s.id === "presets"
        ? {
            ...s,
            subs: presets.map((p) => ({ id: presetAnchorId(p.name), label: p.label })),
          }
        : s,
    );
  }, [base, presets]);
}

/**
 * Track which anchor the user has most recently scrolled past. Uses each
 * anchor's own top position rather than IntersectionObserver intersection: a
 * section's wrapping element stays intersecting the top band the whole time any
 * of its content is on screen, so an intersection-set approach can never
 * advance from a parent section to its sub-anchors. Reading positions directly
 * lets a sub-anchor (config-llm …) win over its parent (config).
 */
function useScrollSpy(anchorIds: readonly string[], scrollId: string): string {
  const [activeId, setActiveId] = useState<string>(anchorIds[0] ?? "");

  useEffect(() => {
    const container = document.getElementById(scrollId);
    const compute = () => {
      const base = container ? container.getBoundingClientRect().top : 0;
      const line = base + SPY_OFFSET_PX;
      setActiveId(
        pickActiveAnchor(anchorIds, line, (id) => {
          const el = document.getElementById(id);
          return el ? el.getBoundingClientRect().top : null;
        }),
      );
    };
    compute();
    const target: HTMLElement | Window = container ?? window;
    target.addEventListener("scroll", compute, { passive: true });
    window.addEventListener("resize", compute);
    return () => {
      target.removeEventListener("scroll", compute);
      window.removeEventListener("resize", compute);
    };
  }, [anchorIds, scrollId]);

  return activeId;
}

function scrollToAnchor(id: string) {
  const el = document.getElementById(id);
  if (!el) return;
  // Instant, not smooth: this is in-page anchor navigation (the equivalent of
  // clicking a table of contents), not a scroll gesture the user should watch
  // ride out — an animated scroll here just adds latency before the target is
  // readable.
  el.scrollIntoView({ behavior: "auto", block: "start" });
  // Reflect the target in the URL without a history entry per click.
  history.replaceState(null, "", `#${id}`);
}

export function ControlNav({
  sections: baseSections,
  scrollId,
  ariaLabel,
}: {
  sections: readonly ControlSectionDef[];
  scrollId: string;
  ariaLabel: string;
}) {
  const t = useTranslations("control.sections");
  // R4 layer 4: one responsive mechanism — breakpoint + conditional render
  // (the old always-mounted desktop rail + mobile disclosure pair hid one
  // with CSS media queries). Switch point preserved from the old CSS `md`
  // rule: the rail shows at >= 768px, the disclosure below it.
  const { isNarrow } = useBreakpoint();
  // A section/sub renders its translated label when a labelKey is present,
  // else its raw English label (data-driven labels like preset names have no
  // key). The type cast is safe: labelKey values are hand-kept in sync with
  // the control.sections catalog (en.json is the canonical English source).
  const labelOf = (labelKey: string | undefined, label: string) =>
    labelKey ? t(labelKey as Parameters<typeof t>[0]) : label;
  const sections = useControlSections(baseSections);
  const anchorIds = useMemo(() => controlAnchorIds(sections), [sections]);
  const activeId = useScrollSpy(anchorIds, scrollId);
  // Mobile disclosure open/closed. Desktop ignores this (nav is always shown).
  const [open, setOpen] = useState(false);

  const onJump = useCallback((id: string) => {
    scrollToAnchor(id);
    setOpen(false);
  }, []);

  // A section is active if it or one of its subs is the current anchor.
  const isSectionActive = (sectionId: string, subIds: string[]) =>
    activeId === sectionId || subIds.includes(activeId);

  const links = (
    <ul className="space-y-0.5">
      {sections.map((section) => {
        const subIds = section.subs?.map((s) => s.id) ?? [];
        const sectionActive = isSectionActive(section.id, subIds);
        return (
          <li key={section.id}>
            <button
              type="button"
              onClick={() => onJump(section.id)}
              aria-current={activeId === section.id ? "true" : undefined}
              className={`block w-full rounded px-2 py-1 text-left text-sm transition-colors ${
                sectionActive
                  ? "font-medium text-foreground"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {labelOf(section.labelKey, section.label)}
            </button>
            {section.subs && (
              <ul className="mt-0.5 space-y-0.5 border-l border-border pl-2 ml-2">
                {section.subs.map((sub) => (
                  <li key={sub.id}>
                    <button
                      type="button"
                      onClick={() => onJump(sub.id)}
                      aria-current={activeId === sub.id ? "true" : undefined}
                      className={`block w-full rounded px-2 py-0.5 text-left text-xs transition-colors ${
                        activeId === sub.id
                          ? "text-foreground"
                          : "text-muted-foreground hover:text-foreground"
                      }`}
                    >
                      {labelOf(sub.labelKey, sub.label)}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </li>
        );
      })}
    </ul>
  );

  // Shown in the mobile disclosure button. The find almost always resolves
  // (activeId seeds to the first anchor); the first section's label is the
  // fallback for the initial frame — both section lists are non-empty.
  const activeSection =
    sections.find((s) => s.id === activeId || s.subs?.some((x) => x.id === activeId)) ??
    sections[0];
  const activeLabel = labelOf(activeSection.labelKey, activeSection.label);

  return (
    <nav
      aria-label={ariaLabel}
      className={cn(
        "shrink-0 border-b border-border",
        !isNarrow ? "border-b-0 border-r w-56 min-h-0" : "",
      )}
    >
      {!isNarrow ? (
        // Desktop: a full-height rail that scrolls its own (long) list. The nav is
        // a flex sibling of the content scroller, already bounded to the row's
        // height (min-h-0 on <nav> lets it shrink to that instead of growing to
        // its content and pushing a document-level scrollbar). h-full + own
        // overflow-y-auto keeps the long two-level list inside the rail — no
        // max-h-screen (which overshoots by the page header and spills below the
        // viewport) and no sticky (the rail is its own column, not scrolling with
        // the content).
        <div className={cn("h-full overflow-y-auto px-3 py-4 gap-3", FLEX, FLEX_COL, MIN_H_0)}>
          {links}
        </div>
      ) : (
        // Mobile: a "Jump to…" disclosure so the nav folds to one row.
        <div className="px-3 py-2">
          <div className={cn("items-center gap-2", FLEX)}>
            <button
              type="button"
              onClick={() => setOpen((v) => !v)}
              aria-expanded={open}
              className={cn("rounded border border-border px-2 py-1 text-left text-sm text-foreground", FLEX_1)}
            >
              {activeLabel}
              <span className="float-right text-muted-foreground">{open ? "▲" : "▼"}</span>
            </button>
          </div>
          {open && <div className="mt-2">{links}</div>}
        </div>
      )}
    </nav>
  );
}
