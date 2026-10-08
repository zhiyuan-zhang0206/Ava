"use client";

// The run timeline's rows on one shared axis (plain time by default, or hybrid: block width follows
// tokens, the space between blocks follows log idle time): lifecycle markers on top,
// then one row per understanding-tree level (topmost first), then layer 0 — the
// message units — at the bottom. All rows share one viewport on the loaded data:
// the wheel / pinch zooms around the cursor, a drag or a horizontal scroll pans,
// and nothing refetches. A single click selects a block, a double-click drills
// into it (the page zooms the viewport to the block's span).

import { useTranslations } from "next-intl";
import { useEffect, useMemo, useRef, useState } from "react";

import type { RunTimelineResponse, RunTimelineUnit } from "@/lib/contracts/types";
import { formatShort } from "@/lib/format/time";
import { cn } from "@/lib/format/utils";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";

import {
  blockClass,
  chainIds,
  firstLine,
  hoverLit,
  axisBox,
  buildAxisMap,
  inboundSources,
  isSelected,
  layoutSpans,
  levelsTopFirst,
  matchesHighlight,
  ADDED_ROW,
  INPUT_ROW,
  UNITS_ROW,
  levelRowId,
  navigate,
  revealView,
  type NavKey,
  MARKER_HIT_PX,
  MARKER_LINE_PX,
  type RowPlacement,
  panView,
  pendingSpans,
  unitColor,
  zoomView,
  unitKey,
  type AxisMode,
  type BlockClass,
  type Highlight,
  type Hover,
  type Selection,
  type Viewport,
} from "./timeline-model";
import { RunTimelineAxis } from "./run-timeline-axis";
import { ContextSizeRow } from "./run-timeline-context-row";
import { RunTimelineLegend } from "./run-timeline-legend";
import { RowShell } from "./run-timeline-row-shell";
import { readoutText, requestReadout } from "./run-timeline-readout";

const NODE_LABEL_CHARS = 80;
// The opacity of everything a highlight does not name.
const FADED = "opacity-[0.12]";
// Track width assumed until the first measurement.
const DEFAULT_TRACK_PX = 1000;
// Height of one marker lane's hit area; markers stacked at one instant take one lane each.
const MARKER_LANE_PX = 5;
const LEVEL_ROW_PX = 32;
const UNIT_ROW_PX = 24;
// A pointer must travel this far before a press becomes a pan (below it, it is a click).
const DRAG_THRESHOLD_PX = 4;
const WHEEL_ZOOM_RATE = 0.0015;
const PINCH_ZOOM_RATE = 0.01;

// The hatching of a stretch the level above has not summarized yet.
const PENDING_HATCH =
  "repeating-linear-gradient(135deg, transparent 0 4px, color-mix(in srgb, currentColor 14%, transparent) 4px 5px)";

function boxStyle(box: { left: number; width: number }) {
  return { left: `${box.left}%`, width: `max(${box.width}%, 3px)` };
}

/** Position of a block: its body, or for a marker the hit strip of its lane (the line itself is a child). */
function placeStyle(place: RowPlacement): React.CSSProperties {
  if (!place.marker) return { left: place.left, width: place.width };
  return {
    left: place.left - MARKER_HIT_PX / 2,
    width: MARKER_HIT_PX,
    top: place.lane * MARKER_LANE_PX,
    height: MARKER_LANE_PX + 1,
    zIndex: 1 + place.lane,
  };
}

/** The visible line of a marker: the full row height, drawn inside its (smaller) hit strip. */
function MarkerLine({
  place,
  rowPx,
  color,
  strong,
}: {
  place: RowPlacement;
  rowPx: number;
  color: string;
  strong: boolean;
}) {
  return (
    <span
      aria-hidden="true"
      data-testid="run-timeline-marker-line"
      className="pointer-events-none absolute rounded-[1px]"
      style={{
        top: -place.lane * MARKER_LANE_PX,
        height: rowPx,
        left: (MARKER_HIT_PX - MARKER_LINE_PX) / 2,
        width: MARKER_LINE_PX,
        background: color,
        boxShadow: strong ? "0 0 0 1px var(--foreground)" : undefined,
      }}
    />
  );
}

export function RunTimelineRows({
  data,
  base,
  view,
  onView,
  selection,
  onSelect,
  onDrill,
  highlight,
  onHighlight,
}: {
  data: RunTimelineResponse;
  /** The whole loaded extent: the viewport never leaves it. */
  base: Viewport;
  view: Viewport;
  onView: (view: Viewport) => void;
  selection: Selection | null;
  onSelect: (selection: Selection) => void;
  onDrill: (selection: Selection) => void;
  /** The legend's highlight: every block of one class (or one source) stays lit, the rest fades. */
  highlight: Highlight | null;
  onHighlight: (highlight: Highlight | null) => void;
}) {
  const t = useTranslations("runTimeline");
  const [hover, setHover] = useState<Hover | null>(null);
  const lit = hoverLit(hover, data.nodes, data.units);
  const levels = levelsTopFirst(data.nodes);
  const [mode, setMode] = useState<AxisMode>("time");
  // The row the selection was made in: a request's bar and its message block select the same thing.
  const [navRow, setNavRow] = useState<string | null>(null);
  const choose = (row: string, target: Selection) => {
    setNavRow(row);
    onSelect(target);
  };
  const baseFrom = base.from;
  const baseTo = base.to;
  const axis = useMemo(
    () => buildAxisMap(data.units, { from: baseFrom, to: baseTo }, mode),
    [data.units, baseFrom, baseTo, mode],
  );
  const viewU = axis.viewU(view);
  // A selection lights itself and every ancestor; the rest steps back.
  const chain = chainIds(selection, data.nodes, data.units);
  const dim = selection !== null;
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
  const nav = useRef({ data, view, base, axis, selection, navRow, onSelect, onView });
  useEffect(() => {
    nav.current = { data, view, base, axis, selection, navRow, onSelect, onView };
  });
  // Arrow keys move the selection (see `navigate`) and pan the view to it; editable and resizing controls keep their own arrows.
  useEffect(() => {
    const keys: Partial<Record<string, NavKey>> = { ArrowLeft: "left", ArrowRight: "right", ArrowUp: "up", ArrowDown: "down" };
    const onKey = (event: KeyboardEvent) => {
      const key = keys[event.key];
      if (event.defaultPrevented || key === undefined || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
      const el = event.target instanceof Element ? event.target : null;
      if (el?.closest("input, textarea, select, [contenteditable], [role=textbox], [role=separator], [role=slider], [role=combobox]")) return;
      const s = nav.current;
      const next = navigate(key, s.selection === null ? null : { row: s.navRow, selection: s.selection }, s.data, s.view);
      event.preventDefault();
      if (next === null) return;
      setNavRow(next.row);
      s.onSelect(next.item.selection);
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

  const trackRect = () => chartRef.current?.querySelector("[data-track]")?.getBoundingClientRect();
  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    const track = trackRect();
    if (event.button !== 0 || !track || event.clientX < track.left) return;
    drag.current = { x: event.clientX, view, panning: false, id: event.pointerId };
  };
  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const state = drag.current;
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
  const sources = highlight !== null && (highlight.cls === "human" || highlight.cls === "agent")
    ? inboundSources(data.units, highlight.cls)
    : [];
  const readout = readoutText(hover, {
    data,
    t,
    unitLabel,
    sourceLabel,
  });
  const hoverProps = (target: Hover) => ({
    onMouseEnter: () => setHover(target),
    onMouseLeave: () => setHover(null),
    onFocus: () => setHover(target),
    onBlur: () => setHover(null),
  });

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
      onClickCapture={(event) => {
        if (suppressClick.current) {
          event.stopPropagation();
          event.preventDefault();
        }
      }}
      className="select-none space-y-1.5 rounded-[10px] border border-border bg-card p-3"
    >
      <div className={cn(FLEX, "h-4 items-center gap-2 pl-[88px]")}>
        <p
          data-testid="run-timeline-readout"
          data-hovering={readout === null ? undefined : ""}
          className={cn(MIN_W_0, "grow truncate font-mono text-[10px] text-muted-foreground")}
        >
          {readout ?? t("readoutIdle")}
        </p>
        <button
          type="button"
          data-testid="run-timeline-axis-mode"
          data-mode={mode}
          aria-pressed={mode === "hybrid"}
          title={t("axisModeTitle")}
          onClick={() => setMode(mode === "hybrid" ? "time" : "hybrid")}
          className="shrink-0 rounded border border-border px-1.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
        >
          {mode === "hybrid" ? t("axisHybrid") : t("axisTime")}
        </button>
      </div>

      {data.events.length > 0 ? (
        <RowShell label={t("lifecycleRow")} height="h-5" testId="run-timeline-row-lifecycle">
          {data.events.map((event) => {
            const box = axisBox(axis, event.ts, event.ts, viewU);
            if (box === null) return null;
            const when = formatShort(event.ts);
            return (
              <span
                key={`${event.kind}-${event.ts}`}
                role="img"
                aria-label={t("eventAria", { kind: event.kind, time: when })}
                title={`${event.kind} · ${when}${event.label ? ` · ${event.label}` : ""}`}
                data-testid="run-timeline-event"
                className="absolute top-1 h-3 w-1.5 rounded-sm bg-foreground/60"
                style={{ left: `${box.left}%` }}
              />
            );
          })}
        </RowShell>
      ) : null}

      {levels.map((level) => (
        <RowShell
          key={level}
          label={t("levelRow", { level })}
          height="h-8"
          testId={`run-timeline-row-level-${level}`}
        >
          {pendingSpans(data.nodes, level).map((span) => {
            const box = axisBox(axis, span.from, span.to, viewU);
            if (box === null) return null;
            return (
              <div
                key={`pending-${span.from}`}
                data-testid="run-timeline-pending"
                title={t("pendingTitle")}
                className={cn(
                  "absolute inset-y-0 truncate rounded border border-dashed border-border px-1",
                  "text-[10px] leading-8 text-muted-foreground",
                )}
                style={{ ...boxStyle(box), backgroundImage: PENDING_HATCH }}
              >
                {t("pending")}
              </div>
            );
          })}
          {(() => {
            const levelNodes = data.nodes.filter((node) => node.level === level);
            const places = new Map(
              layoutSpans(
                levelNodes.map((node) => ({ key: node.id, ...axis.nodeSpan(node) })),
                viewU,
                trackPx,
              ).map((place) => [place.key, place]),
            );
            return levelNodes.map((node) => {
              const place = places.get(node.id);
              if (place === undefined) return null;
              const picked = isSelected(selection, { kind: "node", id: node.id });
              const ancestor = !picked && chain.has(node.id);
              const hovered = hover?.kind === "node" && hover.id === node.id;
              const hoverLight = !picked && !ancestor && lit.nodeIds.has(node.id);
              const faded = highlight !== null && !picked;
              const label = firstLine(node.summary, NODE_LABEL_CHARS);
              return (
                <button
                  key={node.id}
                  type="button"
                  aria-label={t("nodeAria", { level, summary: label })}
                  aria-pressed={picked}
                  data-testid="run-timeline-node"
                  data-node-id={node.id}
                  data-highlight={picked ? "self" : ancestor ? "ancestor" : "none"}
                  data-hover={hovered ? "self" : lit.nodeIds.has(node.id) ? "lit" : undefined}
                  data-faded={faded ? "" : undefined}
                  data-marker={place.marker ? "" : undefined}
                  onClick={() => choose(levelRowId(level), { kind: "node", id: node.id })}
                  onDoubleClick={() => onDrill({ kind: "node", id: node.id })}
                  {...hoverProps({ kind: "node", id: node.id })}
                  className={cn(
                    "absolute outline-none focus-visible:ring-2 focus-visible:ring-foreground",
                    place.marker
                      ? ""
                      : "inset-y-0 truncate rounded border px-1 text-left text-[10px] leading-8 border-border bg-primary/20 text-foreground hover:bg-primary/30",
                    !place.marker && picked && "bg-primary/45 ring-2 ring-foreground",
                    !place.marker && ancestor && "bg-primary/40 ring-2 ring-foreground/60",
                    !place.marker && hoverLight && "bg-primary/30 ring-1 ring-foreground/40",
                    !place.marker && hoverLight && hovered && "ring-foreground/70",
                    faded
                      ? FADED
                      : dim && !picked && !ancestor && !hoverLight && "opacity-40",
                  )}
                  style={placeStyle(place)}
                >
                  {place.marker ? (
                    <MarkerLine
                      place={place}
                      rowPx={LEVEL_ROW_PX}
                      strong={picked || ancestor || hoverLight}
                      color={
                        picked || ancestor || hoverLight ? "var(--foreground)" : "color-mix(in srgb, var(--primary) 70%, transparent)"
                      }
                    />
                  ) : (
                    label
                  )}
                </button>
              );
            });
          })()}
        </RowShell>
      ))}

      <RowShell label={t("messagesRow")} height="h-6" testId="run-timeline-row-units">
        {(() => {
          const places = new Map(
            layoutSpans(
              data.units.map((unit) => ({ key: unitKey(unit), ...axis.unitSpan(unit) })),
              viewU,
              trackPx,
            ).map((place) => [place.key, place]),
          );
          return data.units.map((unit) => {
            const key = unitKey(unit);
            const place = places.get(key);
            if (place === undefined) return null;
            const candidate: Selection = {
              kind: "unit",
              i0: unit.i0,
              i1: unit.i1,
              unitKind: unit.kind,
            };
            const picked = isSelected(selection, candidate);
            const hovered = hover?.kind === "unit" && isSelected(hover, candidate);
            const hoverLight = hovered || lit.unitKeys.has(key);
            const matched = highlight !== null && matchesHighlight(unit, highlight);
            const faded = highlight !== null && !matched && !picked;
            return (
              <button
                key={key}
                type="button"
                aria-label={t("unitAria", { kind: unitLabel(unit), preview: unit.preview })}
                aria-pressed={picked}
                data-testid="run-timeline-unit"
                data-unit-kind={unit.kind}
                data-block-class={blockClass(unit)}
                data-highlight={picked ? "self" : "none"}
                data-hover={hovered ? "self" : hoverLight ? "lit" : undefined}
                data-faded={faded ? "" : undefined}
                data-matched={matched ? "" : undefined}
                data-marker={place.marker ? "" : undefined}
                onClick={() => choose(UNITS_ROW, candidate)}
                onDoubleClick={() => onDrill(candidate)}
                {...hoverProps(candidate)}
                className={cn(
                  "absolute outline-none focus-visible:ring-2 focus-visible:ring-foreground",
                  !place.marker && "inset-y-1 rounded-sm",
                  !place.marker && picked && "ring-2 ring-foreground",
                  !place.marker && !picked && hoverLight && (hovered ? "ring-1 ring-foreground/70" : "ring-1 ring-foreground/40"),
                  faded ? FADED : highlight === null && selection !== null && !picked && !hoverLight && "opacity-40",
                )}
                style={place.marker ? placeStyle(place) : { ...placeStyle(place), background: unitColor(unit) }}
              >
                {place.marker ? (
                  <MarkerLine
                    place={place}
                    rowPx={UNIT_ROW_PX}
                    strong={picked || hoverLight}
                    color={picked || hoverLight ? "var(--foreground)" : unitColor(unit)}
                  />
                ) : null}
              </button>
            );
          });
        })()}
      </RowShell>

      {data.requests.length > 0
        ? (["input", "added"] as const).map((metric) => (
            <ContextSizeRow
              key={metric}
              requests={data.requests}
              metric={metric}
              axis={axis}
              viewU={viewU}
              trackPx={trackPx}
              units={data.units}
              selection={selection}
              onSelect={(target) => choose(metric === "input" ? INPUT_ROW : ADDED_ROW, target)}
              onDrill={onDrill}
              hover={hover}
              hoverProps={hoverProps}
              describe={(request) => requestReadout(t, request)}
            />
          ))
        : null}

      <RunTimelineAxis view={view} base={base} onView={onView} axis={axis} trackPx={trackPx} />

      <RunTimelineLegend
        highlight={highlight}
        onHighlight={onHighlight}
        classLabel={classLabel}
        sources={sources}
        sourceLabel={sourceLabel}
      />

      {data.nodes.length === 0 && data.units.length === 0 ? (
        <p className="pt-1 text-xs text-muted-foreground">{t("empty")}</p>
      ) : null}
    </div>
  );
}
