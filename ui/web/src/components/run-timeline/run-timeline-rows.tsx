"use client";

// The agent view's rows: any number of agents on one shared axis (plain time). Each agent is a group
// of rows: one row per understanding-tree level (topmost first), then
// layer 0 — the message units — and the context bars at the bottom. All rows of all agents share one
// viewport on the loaded data: the wheel / pinch zooms around the cursor, a drag or a horizontal
// scroll pans, and nothing refetches. A click selects a block; the arrow keys move the selection,
// down from an agent's last row into the next agent's first.

import { useTranslations } from "next-intl";
import { useEffect, useMemo, useRef, useState } from "react";

import type { RunTimelineResponse, RunTimelineUnit } from "@/lib/contracts/types";
import { formatShort } from "@/lib/format/time";
import { cn } from "@/lib/format/utils";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";

import {
  blockClass,
  hoverLit,
  timeAxis,
  inboundSources,
  levelsTopFirst,
  panView,
  zoomView,
  type BlockClass,
  type Highlight,
  type Viewport,
} from "./model/timeline-model";
import {
  INPUT_ROW,
  UNITS_ROW,
  levelRowId,
  navRowIds,
  SELECTION_LINE_BELOW_PX,
  selectionRoles,
  revealView,
  type NavKey,
  type RowOptions,
} from "./model/timeline-nav";
import { navigateAcross, type AgentSelection, type ViewAgent } from "./agent-view/agent-view-nav";
import { AgentGroupHeader, AgentPending } from "./agent-view/agent-view-group";
import { useLinkKindLabels } from "./agent-view/agent-view-link-labels";
import { OtherAgentsGroup } from "./agent-view/agent-view-other";
import { LinksCanvas, type LinkHit } from "./canvas/run-timeline-links-canvas";
import {
  externalLinks,
  nearestLink,
  nearestUnit,
  selectionMs,
  stepLink,
  type LinkKind,
  type ResolvedLink,
} from "./model/timeline-links";
import { barTop, frameOf, layoutsFor, type RowLayout } from "./model/timeline-canvas-model";
import { RunTimelineAxis } from "./canvas/run-timeline-axis";
import { TrackCanvas } from "./canvas/run-timeline-canvas";
import { paintBars, paintNodes, paintUnits, type PaintState, type RowDeco, type UnitHeights } from "./canvas/run-timeline-paint";
import { RunTimelineLegend } from "./run-timeline-legend";
import { RowShell } from "./run-timeline-row-shell";
import { readoutText } from "./model/run-timeline-readout";

// Track width assumed until the first measurement.
const DEFAULT_TRACK_PX = 1000;
// A pointer must travel this far before a press becomes a pan (below it, it is a click).
const DRAG_THRESHOLD_PX = 4;
const WHEEL_ZOOM_RATE = 0.0015;
const PINCH_ZOOM_RATE = 0.01;
const LEVEL_ROW_PX = 32;
const UNIT_ROW_PX = 40;
const CONTEXT_ROW_PX = 40;
const EMPTY_DATA = { nodes: [], units: [] };

/** One agent of the view: its data once read, else what is shown in its place. */
export type AgentEntry =
  | { id: number; status: "loaded"; data: RunTimelineResponse }
  | { id: number; status: "loading" }
  | { id: number; status: "failed" };

export function RunTimelineRows({
  entries,
  base,
  view,
  onView,
  selection,
  onSelect,
  highlight,
  onHighlight,
  options,
  unitHeights,
  onRemove,
  onRetry,
  links,
  interactions,
  linkKinds,
  onToggleLinkKind,
  linkKey,
  onSelectLink,
}: {
  /** The agents, top to bottom; at least one is loaded. */
  entries: readonly AgentEntry[];
  /** The whole loaded extent of every agent: the viewport never leaves it. */
  base: Viewport;
  view: Viewport;
  onView: (view: Viewport) => void;
  selection: AgentSelection | null;
  onSelect: (selection: AgentSelection) => void;
  /** The legend's highlight: every block of one class (or one source) stays lit, the rest fades. */
  highlight: Highlight | null;
  onHighlight: (highlight: Highlight | null) => void;
  options: RowOptions;
  /** How the Messages row draws a block's height. */
  unitHeights: UnitHeights;
  /** Removes an agent from the view; null while it is the only one. */
  onRemove: ((agent: number) => void) | null;
  onRetry: (agent: number) => void;
  /** Every arrow between agents the view can resolve; the kinds that are off are not drawn. */
  links: readonly ResolvedLink[];
  /** The Interactions switch: off draws no arrows, no legend for them and no Other agents group. */
  interactions: boolean;
  linkKinds: ReadonlySet<LinkKind>;
  onToggleLinkKind: (kind: LinkKind) => void;
  /** The selected arrow. */
  linkKey: string | null;
  onSelectLink: (key: string) => void;
}) {
  const t = useTranslations("runTimeline");
  const linkLabels = useLinkKindLabels();
  const [hover, setHover] = useState<AgentSelection | null>(null);
  const [hoverLink, setHoverLink] = useState<string | null>(null);
  // Whether the pointer is over a block or node: an item wins over an arrow drawn across it.
  const overItem = useRef(false);
  const hitRef = useRef<LinkHit>(() => null);
  const shownLinks = useMemo(() => (interactions ? links.filter((l) => linkKinds.has(l.link.kind)) : []), [interactions, links, linkKinds]);
  const externals = useMemo(() => externalLinks(shownLinks), [shownLinks]);
  const linkCounts = useMemo(() => {
    const counts = new Map<LinkKind, number>();
    for (const l of links) counts.set(l.link.kind, (counts.get(l.link.kind) ?? 0) + 1);
    return counts;
  }, [links]);
  // The row the selection was made in: a block and its bar in the Context size row select the same thing.
  const [navRow, setNavRow] = useState<string | null>(null);
  const choose = (agent: number, row: string, target: AgentSelection["selection"]) => {
    setNavRow(row);
    onSelect({ agent, selection: target });
  };
  const loaded = useMemo(
    () =>
      entries.flatMap((entry) => (entry.status === "loaded" ? [{ id: entry.id, data: entry.data }] : [])),
    [entries],
  );
  const baseFrom = base.from;
  const baseTo = base.to;
  const axis = useMemo(() => timeAxis({ from: baseFrom, to: baseTo }), [baseFrom, baseTo]);
  const agents: ViewAgent[] = useMemo(
    () => loaded.map(({ id, data }) => ({ id, data, rows: navRowIds(data, options) })),
    [loaded, options],
  );
  const byId = new Map(agents.map((agent) => [agent.id, agent]));
  const viewU = axis.viewU(view);
  const chartRef = useRef<HTMLDivElement>(null);
  const live = useRef({ base, view, onView, axis });
  // The view a wheel event produced that React has not rendered yet.
  const pending = useRef<Viewport | null>(null);
  useEffect(() => {
    const wanted = pending.current;
    if (wanted && (view.from !== wanted.from || view.to !== wanted.to)) {
      // A render of an older view: the wheel's own result is still on its way.
      live.current = { ...live.current, base, onView, axis };
      return;
    }
    pending.current = null;
    live.current = { base, view, onView, axis };
  });
  const [trackPx, setTrackPx] = useState(DEFAULT_TRACK_PX);
  // Where the selected items are, per row: drawn as an outlined box in each row and a line through all of them.
  const layouts = new Map(agents.map((agent) => [agent.id, layoutsFor(agent.data, axis, viewU, trackPx, agent.rows)]));
  const selected = selection === null ? undefined : byId.get(selection.agent);
  const roles = selectionRoles(
    selection === null || selected === undefined ? null : { row: navRow, selection: selection.selection },
    selected?.data ?? EMPTY_DATA,
    selected?.rows,
  );
  // The primary item (the cursor's) gets one strong frame; what is linked to it one light frame per row around the whole batch.
  // A frame hugs the drawn item; a primary item narrower than 6 px also gets a hairline.
  const boxesOf = (agent: number, row: string, keys: Iterable<string>) => {
    const boxes = layouts.get(agent)?.get(row)?.boxes;
    return [...keys].flatMap((key) => boxes?.get(key) ?? []);
  };
  const primaryBoxes =
    roles.primary === null || selection === null ? [] : boxesOf(selection.agent, roles.primary.row, [roles.primary.key]);
  const primaryRaw = frameOf(primaryBoxes, trackPx);
  const lineX = primaryRaw !== null && primaryRaw.width < SELECTION_LINE_BELOW_PX ? primaryRaw.left + primaryRaw.width / 2 : null;
  const decoFor = (agent: number, row: string): RowDeco => {
    const mine = selection?.agent === agent;
    const primaryHere = mine && roles.primary?.row === row ? roles.primary.key : null;
    const linkedKeys = mine ? (roles.linked.get(row) ?? new Set<string>()) : new Set<string>();
    return {
      primaryKey: primaryHere,
      linkedKeys,
      primary: primaryHere === null ? null : frameOf(boxesOf(agent, row, [primaryHere]), trackPx),
      linked: frameOf(boxesOf(agent, row, linkedKeys), trackPx),
      lineX,
    };
  };
  useEffect(() => {
    const track = chartRef.current?.querySelector("[data-track]");
    if (!track) return;
    const measure = () => {
      const width = track.getBoundingClientRect().width;
      if (width > 0) setTrackPx(width);
    };
    measure();

    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(track);
    return () => observer.disconnect();
  }, []);
  const nav = useRef({ agents, view, base, axis, selection, navRow, onSelect, onView, externals, linkKey, onSelectLink });
  useEffect(() => {
    nav.current = { agents, view, base, axis, selection, navRow, onSelect, onView, externals, linkKey, onSelectLink };
  });
  // Arrow keys move the selection (see `navigateAcross`) and pan the view to it; editable and resizing controls keep their own arrows.
  useEffect(() => {
    const keys: Partial<Record<string, NavKey>> = { ArrowLeft: "left", ArrowRight: "right", ArrowUp: "up", ArrowDown: "down" };
    const onKey = (event: KeyboardEvent) => {
      const key = keys[event.key];
      if (event.defaultPrevented || key === undefined || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
      const el = event.target instanceof Element ? event.target : null;
      if (el?.closest("input, textarea, select, [contenteditable], [role=textbox], [role=separator], [role=slider], [role=combobox]")) return;
      const s = nav.current;
      const reveal = (ms: number) => {
        const shown = revealView(s.axis, s.view, s.base, ms, ms);
        if (shown !== s.view) s.onView(shown);
      };
      // On the Other agents row the arrows walk its events; up leaves it for the last agent's Messages row.
      if (s.linkKey !== null && s.externals.some((l) => l.key === s.linkKey)) {
        event.preventDefault();
        if (key === "left" || key === "right") {
          const to = stepLink(s.externals, s.linkKey, key);
          const found = s.externals.find((l) => l.key === to);
          if (to !== null && found !== undefined) {
            s.onSelectLink(to);
            reveal(found.from.ms);
          }
        } else if (key === "up") {
          const last = s.agents.at(-1);
          const here = s.externals.find((l) => l.key === s.linkKey);
          const target = last === undefined || here === undefined ? null : nearestUnit(last.data, here.from.ms);
          if (last !== undefined && target !== null) {
            setNavRow(UNITS_ROW);
            s.onSelect({ agent: last.id, selection: target });
          }
        }
        return;
      }
      const next = navigateAcross(
        key,
        s.selection === null ? null : { ...s.selection, row: s.navRow },
        s.agents,
        s.axis,
        s.view,
      );
      event.preventDefault();
      // The clicked block keeps keyboard focus (its focus ring and hover echo) while the selection moves on.
      if (el !== null && chartRef.current?.contains(el) && el instanceof HTMLElement) el.blur();
      setHover(null);
      if (next === null) {
        // Down from the last agent's last row enters the Other agents row, at the event nearest in time.
        const last = s.agents.at(-1);
        const here = key === "down" && s.selection !== null && last?.id === s.selection.agent ? selectionMs(last.data, s.selection.selection) : null;
        const to = here === null ? null : nearestLink(s.externals, here);
        const found = s.externals.find((l) => l.key === to);
        if (to !== null && found !== undefined) {
          s.onSelectLink(to);
          reveal(found.from.ms);
        }
        return;
      }
      setNavRow(next.row);
      s.onSelect({ agent: next.agent, selection: next.item.selection });
      const shown = revealView(s.axis, s.view, s.base, next.item.start, next.item.end);
      if (shown !== s.view) s.onView(shown);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const drag = useRef<{ x: number; view: Viewport; panning: boolean; id: number } | null>(null);
  const suppressClick = useRef(false);

  // The wheel listener is native and non-passive so the page does not scroll under a zoom.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const onWheel = (event: WheelEvent) => {
      const track = chart.querySelector("[data-track]")?.getBoundingClientRect();
      if (!track || track.width <= 0) return;
      const { base: b, view: v, axis: a } = live.current;
      const horizontal = !event.ctrlKey && Math.abs(event.deltaX) > Math.abs(event.deltaY);
      event.preventDefault();
      const rate = event.ctrlKey ? PINCH_ZOOM_RATE : WHEEL_ZOOM_RATE;
      const next = horizontal
        ? panView(a, v, b, event.deltaX / track.width)
        : zoomView(a, v, b, (event.clientX - track.left) / track.width, Math.exp(event.deltaY * rate));
      // Events can arrive faster than React renders: the next one must start from this result.
      pending.current = next;
      live.current = { ...live.current, view: next };
      live.current.onView(next);
    };
    chart.addEventListener("wheel", onWheel, { passive: false });
    return () => chart.removeEventListener("wheel", onWheel);
  }, []);

  // A click on an arrow (where no block is under the pointer) selects it; native, like the wheel, because the chart itself is no control.
  const onLink = useRef(onSelectLink);
  useEffect(() => {
    onLink.current = onSelectLink;
  });
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const onClick = (event: MouseEvent) => {
      if (overItem.current || (event.target instanceof Element && event.target.closest("[data-testid=run-timeline-other-event]"))) return;
      const key = hitRef.current(event.clientX, event.clientY);
      if (key !== null) onLink.current(key);
    };
    chart.addEventListener("click", onClick);
    return () => chart.removeEventListener("click", onClick);
  }, []);

  const trackRect = () => chartRef.current?.querySelector("[data-track]")?.getBoundingClientRect();
  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    const track = trackRect();
    if (event.button !== 0 || !track || event.clientX < track.left) return;
    drag.current = { x: event.clientX, view, panning: false, id: event.pointerId };
  };
  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const state = drag.current;
    // An arrow is under the pointer only where no item is (and not over a marker of the Other agents row, which hovers itself).
    if (state === null && !(event.target instanceof Element && event.target.closest("[data-testid=run-timeline-other-event]"))) {
      const key = overItem.current ? null : hitRef.current(event.clientX, event.clientY);
      setHoverLink(key);
      event.currentTarget.style.cursor = key === null ? "" : "pointer";
    }
    const track = trackRect();
    if (!state || !track || track.width <= 0) return;
    const dx = event.clientX - state.x;
    if (!state.panning) {
      if (Math.abs(dx) < DRAG_THRESHOLD_PX) return;
      state.panning = true;
      // eslint-disable-next-line @typescript-eslint/no-unnecessary-condition -- jsdom has no pointer capture
      event.currentTarget.setPointerCapture?.(state.id);
    }
    onView(panView(axis, state.view, base, -dx / track.width));
  };
  const endDrag = () => {
    if (drag.current?.panning) {
      // The release of a pan would otherwise click the block under the pointer.
      suppressClick.current = true;
      setTimeout(() => {
        suppressClick.current = false;
      }, 0);
    }
    drag.current = null;
  };
  const classLabel: Record<BlockClass, string> = {
    human: t("blockHuman"),
    agent: t("blockAgent"),
    text: t("blockText"),
    thinking: t("blockThinking"),
    call: t("blockCall"),
    output: t("blockOutput"),
    note: t("blockNote"),
  };
  const unitLabel = (unit: Pick<RunTimelineUnit, "kind" | "source">) => classLabel[blockClass(unit)];
  const sourceLabel = (source: string) =>
    source.startsWith("agent:") ? t("sourceFromAgent", { id: source.slice("agent:".length) }) : source;
  const sources =
    highlight !== null && (highlight.cls === "human" || highlight.cls === "agent")
      ? [...new Set(agents.flatMap((agent) => inboundSources(agent.data.units, highlight.cls as "human" | "agent")))].sort()
      : [];
  // The line saying what is hovered (or selected) names its agent when the view holds several.
  const describe = (target: AgentSelection | null) => {
    const owner = target === null ? undefined : byId.get(target.agent);
    if (target === null || owner === undefined) return null;
    const line = readoutText(target.selection, { data: owner.data, t, unitLabel, sourceLabel });
    return line !== null && agents.length > 1 ? t("readoutAgent", { id: target.agent, line }) : line;
  };
  const hovered = hoverLink === null ? undefined : links.find((l) => l.key === hoverLink);
  const readout =
    hovered === undefined
      ? describe(hover)
      : t("readoutLink", {
          kind: linkLabels[hovered.link.kind],
          from: hovered.link.sender,
          to: hovered.link.receiver,
          time: formatShort(hovered.link.ts),
        });
  // The layout of every row (where each item is drawn) is cached per view: hovering and selecting only repaint.
  const paintStateOf = (agent: ViewAgent): PaintState => {
    const mine = hover?.agent === agent.id ? hover.selection : null;
    return {
      selection: selection?.agent === agent.id ? selection.selection : null,
      hover: mine,
      lit: hoverLit(mine, agent.data.nodes, agent.data.units),
      highlight,
    };
  };
  const canvasFor = (
    agent: ViewAgent,
    row: string,
    height: number,
    paint: (p: Parameters<React.ComponentProps<typeof TrackCanvas>["paint"]>[0], layout: RowLayout) => void,
  ) => {
    const layout = layouts.get(agent.id)?.get(row);
    if (layout === undefined) return null;
    return (
      <TrackCanvas
        width={trackPx}
        height={height}
        layout={layout}
        paint={(p) => paint(p, layout)}
        testId={`run-timeline-canvas-${row}`}
        onHit={(key) => {
          const item = key === null ? undefined : layout.items.get(key);
          overItem.current = item !== undefined;
          setHover(item === undefined ? null : { agent: agent.id, selection: item.selection });
        }}
        onChoose={(key) => {
          const item = layout.items.get(key);
          if (item !== undefined) choose(agent.id, row, item.selection);
        }}
      />
    );
  };
  // The selection read aloud: the canvas is decorative, the arrow keys move through every item.
  const spoken = describe(selection);
  const empty = loaded.every(({ data }) => data.nodes.length === 0 && data.units.length === 0);

  const renderAgent = (agent: ViewAgent) => {
    const { data } = agent;
    const paintState = paintStateOf(agent);
    const levels = levelsTopFirst(data.nodes).filter((level) => agent.rows.includes(levelRowId(level)));
    return (
      <section
        key={agent.id}
        aria-label={t("agentGroupAria", { id: agent.id })}
        data-testid={`agent-view-agent-${agent.id}`}
        className="space-y-1.5"
      >
        <AgentGroupHeader agentId={agent.id} onRemove={onRemove} />
        {levels.map((level) => (
          <RowShell
            key={level}
            label={t("levelRow", { level })}
            height="h-8"
            testId={`run-timeline-row-level-${level}`}
          >
            {canvasFor(agent, levelRowId(level), LEVEL_ROW_PX, (p, layout) =>
              paintNodes(p, layout, paintState, decoFor(agent.id, levelRowId(level))),
            )}
          </RowShell>
        ))}

        <RowShell label={t("messagesRow")} height="h-10" testId="run-timeline-row-units">
          {canvasFor(agent, UNITS_ROW, UNIT_ROW_PX, (p, layout) =>
            paintUnits(p, layout, paintState, decoFor(agent.id, UNITS_ROW), unitHeights, barTop(UNITS_ROW, data)),
          )}
        </RowShell>

        {agent.rows.includes(INPUT_ROW) ? (
          <RowShell label={t("contextRow")} height="h-10" testId="run-timeline-row-context">
            {canvasFor(agent, INPUT_ROW, CONTEXT_ROW_PX, (p, layout) =>
              paintBars(p, layout, barTop(INPUT_ROW, data), paintState, decoFor(agent.id, INPUT_ROW)),
            )}
          </RowShell>
        ) : null}
      </section>
    );
  };

  return (
    <div
      ref={chartRef}
      role="group"
      aria-label={t("chartAria")}
      data-testid="run-timeline-chart"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onPointerLeave={() => setHoverLink(null)}
      onClickCapture={(event) => {
        if (suppressClick.current) {
          event.stopPropagation();
          event.preventDefault();
        }
      }}
      className="relative select-none space-y-3 rounded-[10px] border border-border bg-card p-3"
    >
      <LinksCanvas links={shownLinks} selectedKey={linkKey} hoverKey={hoverLink} axis={axis} view={view} hitRef={hitRef} />
      <p role="status" aria-live="polite" data-testid="run-timeline-selection-live" className="sr-only">
        {spoken ?? ""}
      </p>
      <div className={cn(FLEX, "h-4 items-center gap-2 pl-[88px]")}>
        <p
          data-testid="run-timeline-readout"
          data-hovering={readout === null ? undefined : ""}
          className={cn(MIN_W_0, "grow truncate font-mono text-[10px] text-muted-foreground")}
        >
          {readout ?? t("readoutIdle")}
        </p>
      </div>

      {entries.map((entry) => {
        if (entry.status !== "loaded") {
          return <AgentPending key={entry.id} agentId={entry.id} failed={entry.status === "failed"} onRetry={onRetry} onRemove={onRemove} />;
        }
        const agent = byId.get(entry.id);
        return agent === undefined ? null : renderAgent(agent);
      })}

      {interactions ? (
        <OtherAgentsGroup
          links={externals}
          axis={axis}
          viewU={viewU}
          selectedKey={linkKey}
          onHover={setHoverLink}
          onSelect={onSelectLink}
        />
      ) : null}

      <RunTimelineAxis view={view} base={base} onView={onView} axis={axis} />

      <RunTimelineLegend
        highlight={highlight}
        onHighlight={onHighlight}
        classLabel={classLabel}
        sources={sources}
        sourceLabel={sourceLabel}
        interactions={interactions}
        linkKinds={linkKinds}
        linkLabels={linkLabels}
        linkCounts={linkCounts}
        onToggleLinkKind={onToggleLinkKind}
      />

      {empty ? <p className="pt-1 text-xs text-muted-foreground">{t("empty")}</p> : null}
    </div>
  );
}
