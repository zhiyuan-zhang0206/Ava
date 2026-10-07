"use client";

// The run timeline's rows on one shared time axis: lifecycle markers on top,
// then one row per understanding-tree level (topmost first), then layer 0 — the
// message units — at the bottom. All rows share one viewport on the loaded data:
// the wheel / pinch zooms around the cursor, a drag or a horizontal scroll pans,
// and nothing refetches. A single click selects a block, a double-click drills
// into it (the page zooms the viewport to the block's span).

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import type { RunTimelineResponse, RunTimelineUnit } from "@/lib/types";
import { formatShort } from "@/lib/time";
import { FLEX, MIN_W_0, OVERFLOW_HIDDEN } from "@/lib/layout";
import { cn } from "@/lib/utils";

import { buttonVariants } from "@/components/ui/button";

import {
  BLOCK_CLASSES,
  axisTicks,
  blockClass,
  chainIds,
  classColor,
  firstLine,
  isSelected,
  layoutRow,
  levelsTopFirst,
  MARKER_HIT_PX,
  MARKER_LINE_PX,
  type RowPlacement,
  panViewport,
  pendingSpans,
  spanBox,
  unitColor,
  viewportWindow,
  zoomViewport,
  type BlockClass,
  type Selection,
  type Viewport,
} from "./timeline-model";

const NODE_LABEL_CHARS = 80;
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
const BUTTON_ZOOM = 0.5;

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

function RowShell({
  label,
  height,
  testId,
  children,
}: {
  label: string;
  height: string;
  testId: string;
  children: React.ReactNode;
}) {
  return (
    <div className={cn(FLEX, "items-stretch gap-2")} data-testid={testId}>
      <div className="w-20 shrink-0 self-center truncate text-right text-[11px] text-muted-foreground">
        {label}
      </div>
      <div
        data-track=""
        className={cn("relative rounded bg-muted/40 [touch-action:pan-y]", MIN_W_0, OVERFLOW_HIDDEN, height, "grow")}
      >
        {children}
      </div>
    </div>
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
}: {
  data: RunTimelineResponse;
  /** The whole loaded extent: the viewport never leaves it. */
  base: Viewport;
  view: Viewport;
  onView: (view: Viewport) => void;
  selection: Selection | null;
  onSelect: (selection: Selection) => void;
  onDrill: (selection: Selection) => void;
}) {
  const t = useTranslations("runTimeline");
  const levels = levelsTopFirst(data.nodes);
  const visible = viewportWindow(view);
  // A selection lights itself and every ancestor; the rest steps back.
  const chain = chainIds(selection, data.nodes, data.units);
  const dim = selection !== null;
  const ticks = axisTicks(view);
  const chartRef = useRef<HTMLDivElement>(null);
  const live = useRef({ base, view, onView });
  // The view a wheel event produced that React has not rendered yet.
  const pending = useRef<Viewport | null>(null);
  useEffect(() => {
    const wanted = pending.current;
    if (wanted && (view.from !== wanted.from || view.to !== wanted.to)) {
      // A render of an older view: the wheel's own result is still on its way.
      live.current = { ...live.current, base, onView };
      return;
    }
    pending.current = null;
    live.current = { base, view, onView };
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
  const drag = useRef<{ x: number; view: Viewport; panning: boolean; id: number } | null>(null);
  const suppressClick = useRef(false);

  // The wheel listener is native and non-passive so the page does not scroll under a zoom.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const onWheel = (event: WheelEvent) => {
      const track = chart.querySelector("[data-track]")?.getBoundingClientRect();
      if (!track || track.width <= 0) return;
      const { base: b, view: v } = live.current;
      const horizontal = !event.ctrlKey && Math.abs(event.deltaX) > Math.abs(event.deltaY);
      event.preventDefault();
      const rate = event.ctrlKey ? PINCH_ZOOM_RATE : WHEEL_ZOOM_RATE;
      const next = horizontal
        ? panViewport(v, b, event.deltaX / track.width)
        : zoomViewport(v, b, (event.clientX - track.left) / track.width, Math.exp(event.deltaY * rate));
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
    onView(panViewport(state.view, base, -dx / track.width));
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
  const zoomButton = (factor: number) => onView(zoomViewport(view, base, 0.5, factor));
  const atBase = view.from <= base.from && view.to >= base.to;
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
      {data.events.length > 0 ? (
        <RowShell label={t("lifecycleRow")} height="h-5" testId="run-timeline-row-lifecycle">
          {data.events.map((event) => {
            const box = spanBox(event.ts, event.ts, visible);
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
            const box = spanBox(span.from, span.to, visible);
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
              layoutRow(
                levelNodes.map((node) => ({ key: node.id, start: node.start, end: node.end })),
                visible,
                trackPx,
              ).map((place) => [place.key, place]),
            );
            return levelNodes.map((node) => {
              const place = places.get(node.id);
              if (place === undefined) return null;
              const picked = isSelected(selection, { kind: "node", id: node.id });
              const ancestor = !picked && chain.has(node.id);
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
                  data-marker={place.marker ? "" : undefined}
                  onClick={() => onSelect({ kind: "node", id: node.id })}
                  onDoubleClick={() => onDrill({ kind: "node", id: node.id })}
                  className={cn(
                    "absolute outline-none focus-visible:ring-2 focus-visible:ring-foreground",
                    place.marker
                      ? ""
                      : "inset-y-0 truncate rounded border px-1 text-left text-[10px] leading-8 border-border bg-primary/20 text-foreground hover:bg-primary/30",
                    !place.marker && picked && "bg-primary/45 ring-2 ring-foreground",
                    !place.marker && ancestor && "bg-primary/40 ring-2 ring-foreground/60",
                    dim && !picked && !ancestor && "opacity-40",
                  )}
                  style={placeStyle(place)}
                >
                  {place.marker ? (
                    <MarkerLine
                      place={place}
                      rowPx={LEVEL_ROW_PX}
                      strong={picked || ancestor}
                      color={
                        picked || ancestor ? "var(--foreground)" : "color-mix(in srgb, var(--primary) 70%, transparent)"
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
            layoutRow(
              data.units.map((unit) => ({ key: `${unit.kind}-${unit.i0}-${unit.i1}`, start: unit.start, end: unit.end })),
              visible,
              trackPx,
            ).map((place) => [place.key, place]),
          );
          return data.units.map((unit) => {
          const place = places.get(`${unit.kind}-${unit.i0}-${unit.i1}`);
          if (place === undefined) return null;
          const candidate: Selection = {
            kind: "unit",
            i0: unit.i0,
            i1: unit.i1,
            unitKind: unit.kind,
          };
          const picked = isSelected(selection, candidate);
          return (
            <button
              key={`${unit.kind}-${unit.i0}-${unit.i1}`}
              type="button"
              aria-label={t("unitAria", { kind: unitLabel(unit), preview: unit.preview })}
              aria-pressed={picked}
              title={`${unitLabel(unit)} · #${unit.i0}–#${unit.i1}\n${unit.preview}`}
              data-testid="run-timeline-unit"
              data-unit-kind={unit.kind}
              data-block-class={blockClass(unit)}
              data-highlight={picked ? "self" : "none"}
              data-marker={place.marker ? "" : undefined}
              onClick={() => onSelect(candidate)}
              onDoubleClick={() => onDrill(candidate)}
              className={cn(
                "absolute outline-none focus-visible:ring-2 focus-visible:ring-foreground",
                !place.marker && "inset-y-1 rounded-sm",
                !place.marker && picked && "ring-2 ring-foreground",
                dim && !picked && "opacity-40",
              )}
              style={place.marker ? placeStyle(place) : { ...placeStyle(place), background: unitColor(unit) }}
            >
              {place.marker ? (
                <MarkerLine place={place} rowPx={UNIT_ROW_PX} strong={picked} color={picked ? "var(--foreground)" : unitColor(unit)} />
              ) : null}
            </button>
          );
          });
        })()}
      </RowShell>

      <div className={cn(FLEX, "gap-2 text-[10px] text-muted-foreground")}>
        <div className={cn(FLEX, "w-20 shrink-0 justify-end gap-0.5")}>
          <button
            type="button"
            aria-label={t("zoomIn")}
            title={t("zoomIn")}
            onClick={() => zoomButton(BUTTON_ZOOM)}
            className={cn(buttonVariants({ size: "sm", variant: "ghost" }), "h-5 px-1.5 text-xs")}
          >
            +
          </button>
          <button
            type="button"
            aria-label={t("zoomOut")}
            title={t("zoomOut")}
            onClick={() => zoomButton(1 / BUTTON_ZOOM)}
            className={cn(buttonVariants({ size: "sm", variant: "ghost" }), "h-5 px-1.5 text-xs")}
          >
            −
          </button>
          <button
            type="button"
            aria-label={t("zoomReset")}
            title={t("zoomReset")}
            disabled={atBase}
            onClick={() => onView(base)}
            className={cn(buttonVariants({ size: "sm", variant: "ghost" }), "h-5 px-1.5 text-xs")}
          >
            ⤢
          </button>
        </div>
        <div className={cn("relative h-4 grow font-mono tabular-nums", MIN_W_0)} data-testid="run-timeline-axis">
          {ticks.map((tick) => (
            <span
              key={tick.left}
              className="absolute top-0 -translate-x-1/2 whitespace-nowrap"
              style={{ left: `${tick.left}%` }}
            >
              {tick.label}
            </span>
          ))}
        </div>
      </div>

      <ul
        aria-label={t("legendLabel")}
        data-testid="run-timeline-legend"
        className={cn(FLEX, "flex-wrap gap-x-3 gap-y-1 pl-[88px] text-[10px] text-muted-foreground")}
      >
        {BLOCK_CLASSES.map((kind) => (
          <li key={kind} className={cn(FLEX, "items-center gap-1")}>
            <span
              aria-hidden="true"
              className="inline-block h-2.5 w-2.5 rounded-sm"
              style={{ background: classColor(kind) }}
            />
            {classLabel[kind]}
          </li>
        ))}
      </ul>

      {data.nodes.length === 0 && data.units.length === 0 ? (
        <p className="pt-1 text-xs text-muted-foreground">{t("empty")}</p>
      ) : null}
    </div>
  );
}
