"use client";

import { useTranslations } from "next-intl";
import { useEffect, useMemo, useRef, useState } from "react";

import type { RunTimelineResponse, RunTimelineUnit } from "@/lib/contracts/types";
import { formatShort } from "@/lib/format/time";
import { externalAgentName, EXTERNAL_AGENT_PREFIX } from "@/lib/format/inbound-source";
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
import { useLinkEndLabel, useLinkKindLabels } from "./agent-view/agent-view-link-labels";
import { LinkRowGroup } from "./agent-view/agent-view-link-group";
import { LinksCanvas, type LinkHit, type LinkHitResult } from "./canvas/run-timeline-links-canvas";
import {
  endIn,
  groupLinks,
  MAX_ARROWS,
  nearestLink,
  nearestUnit,
  selectionMs,
  stepLink,
  type LinkKind,
  type LinkRow,
  type ResolvedLink,
} from "./model/timeline-links";
import { barTop, frameOf, layoutsFor, type RowLayout } from "./model/timeline-canvas-model";
import { RunTimelineAxis } from "./canvas/run-timeline-axis";
import { TrackCanvas } from "./canvas/run-timeline-canvas";
import { paintBars, paintNodes, paintUnits, type PaintState, type RowDeco, type UnitHeights } from "./canvas/run-timeline-paint";
import { RunTimelineLegend } from "./run-timeline-legend";
import { RowShell } from "./run-timeline-row-shell";
import { readoutText } from "./model/run-timeline-readout";

const DEFAULT_TRACK_PX = 1000;

const DRAG_THRESHOLD_PX = 4;
const WHEEL_ZOOM_RATE = 0.0015;
const PINCH_ZOOM_RATE = 0.01;
const LEVEL_ROW_PX = 32;
const UNIT_ROW_PX = 40;
const CONTEXT_ROW_PX = 40;
const EMPTY_DATA = { nodes: [], units: [] };

export type AgentEntry =
  | { id: number; status: "loaded"; data: RunTimelineResponse }
  | { id: number; status: "loading" }
  | { id: number; status: "failed" };

// meiva:ref ui/web/src/docs/frontend-components/run-timeline/axis-and-keyboard.meiva.okf.md#row-gesture-and-navigation
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
  showUser,
  onShowUser,
  showOther,
  onShowOther,
  linkKinds,
  onToggleLinkKind,
  linkKeys,
  onSelectLinks,
  maxArrows = MAX_ARROWS,
}: {

  entries: readonly AgentEntry[];

  base: Viewport;
  view: Viewport;
  onView: (view: Viewport) => void;
  selection: AgentSelection | null;
  onSelect: (selection: AgentSelection) => void;

  highlight: Highlight | null;
  onHighlight: (highlight: Highlight | null) => void;
  options: RowOptions;

  unitHeights: UnitHeights;

  onRemove: ((agent: number) => void) | null;
  onRetry: (agent: number) => void;

  links: readonly ResolvedLink[];

  showUser: boolean;
  onShowUser: (on: boolean) => void;
  showOther: boolean;
  onShowOther: (on: boolean) => void;
  linkKinds: ReadonlySet<LinkKind>;
  onToggleLinkKind: (kind: LinkKind) => void;

  linkKeys: readonly string[];
  onSelectLinks: (keys: readonly string[]) => void;

  maxArrows?: number;
}) {
  const linkKey = linkKeys.length === 1 ? linkKeys[0] : null;
  const onSelectLink = (key: string) => onSelectLinks([key]);
  const t = useTranslations("runTimeline");
  const linkLabels = useLinkKindLabels();
  const endLabel = useLinkEndLabel();
  const [hover, setHover] = useState<AgentSelection | null>(null);

  const [hoverArrow, setHoverArrow] = useState<LinkHitResult | null>(null);
  const hoverArrowKey = useRef<string | null>(null);
  const hoverArrowTo = (next: LinkHitResult | null) => {
    if ((next?.key ?? null) === hoverArrowKey.current) return;
    hoverArrowKey.current = next?.key ?? null;
    setHoverArrow(next);
  };
  const hoverKeys = useMemo(() => new Set(hoverArrow?.members ?? []), [hoverArrow]);
  const selectedKeys = useMemo(() => new Set(linkKeys), [linkKeys]);

  const overItem = useRef(false);
  const hitRef = useRef<LinkHit>(() => null);
  const shownLinks = useMemo(
    () =>
      links.filter(
        (l) => linkKinds.has(l.kind) && (showUser || endIn(l, "user") === undefined) && (showOther || endIn(l, "other") === undefined),
      ),
    [showUser, showOther, links, linkKinds],
  );

  const groups = useMemo(() => {
    const grouped = groupLinks(shownLinks);
    return ([
      { row: "user", links: grouped.user },
      { row: "other", links: grouped.other },
    ] as const).filter((g) => g.links.length > 0);
  }, [shownLinks]);
  const linkCounts = useMemo(() => {
    const counts = new Map<LinkKind, number>();
    for (const l of links) counts.set(l.kind, (counts.get(l.kind) ?? 0) + 1);
    return counts;
  }, [links]);

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

  const pending = useRef<Viewport | null>(null);
  useEffect(() => {
    const wanted = pending.current;
    if (wanted && (view.from !== wanted.from || view.to !== wanted.to)) {

      live.current = { ...live.current, base, onView, axis };
      return;
    }
    pending.current = null;
    live.current = { base, view, onView, axis };
  });
  const [trackPx, setTrackPx] = useState(DEFAULT_TRACK_PX);

  const layouts = new Map(agents.map((agent) => [agent.id, layoutsFor(agent.data, axis, viewU, trackPx, agent.rows)]));
  const selected = selection === null ? undefined : byId.get(selection.agent);
  const roles = selectionRoles(
    selection === null || selected === undefined ? null : { row: navRow, selection: selection.selection },
    selected?.data ?? EMPTY_DATA,
    selected?.rows,
  );

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
  const nav = useRef({ agents, view, base, axis, selection, navRow, onSelect, onView, groups, linkKey, onSelectLink });
  useEffect(() => {
    nav.current = { agents, view, base, axis, selection, navRow, onSelect, onView, groups, linkKey, onSelectLink };
  });

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

      const at = s.linkKey === null ? -1 : s.groups.findIndex((g) => g.links.some((l) => l.key === s.linkKey));
      if (s.linkKey !== null && at >= 0) {
        event.preventDefault();
        const group = s.groups[at];
        const here = group.links.find((l) => l.key === s.linkKey);
        const hereMs = here === undefined ? 0 : (endIn(here, group.row)?.ms ?? 0);
        const goTo = (row: LinkRow, links: readonly ResolvedLink[], ms: number) => {
          const to = nearestLink(links, row, ms);
          const found = links.find((l) => l.key === to);
          if (to === null || found === undefined) return;
          s.onSelectLink(to);
          reveal(endIn(found, row)?.ms ?? ms);
        };
        if (key === "left" || key === "right") {
          const to = stepLink(group.links, s.linkKey, key);
          const found = group.links.find((l) => l.key === to);
          if (to !== null && found !== undefined) {
            s.onSelectLink(to);
            reveal(endIn(found, group.row)?.ms ?? hereMs);
          }
        } else if (key === "down") {
          const next = s.groups.at(at + 1);
          if (next !== undefined) goTo(next.row, next.links, hereMs);
        } else if (at > 0) {
          const prev = s.groups[at - 1];
          goTo(prev.row, prev.links, hereMs);
        } else {
          const last = s.agents.at(-1);
          const target = last === undefined ? null : nearestUnit(last.data, hereMs);
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

      if (el !== null && chartRef.current?.contains(el) && el instanceof HTMLElement) el.blur();
      setHover(null);
      if (next === null) {

        const last = s.agents.at(-1);
        const here = key === "down" && s.selection !== null && last?.id === s.selection.agent ? selectionMs(last.data, s.selection.selection) : null;
        const first = s.groups.at(0);
        const to = here === null || first === undefined ? null : nearestLink(first.links, first.row, here);
        const found = first?.links.find((l) => l.key === to);
        if (to !== null && found !== undefined && first !== undefined) {
          s.onSelectLink(to);
          reveal(endIn(found, first.row)?.ms ?? here ?? 0);
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

      pending.current = next;
      live.current = { ...live.current, view: next };
      live.current.onView(next);
    };
    chart.addEventListener("wheel", onWheel, { passive: false });
    return () => chart.removeEventListener("wheel", onWheel);
  }, []);

  const onLinks = useRef(onSelectLinks);
  useEffect(() => {
    onLinks.current = onSelectLinks;
  });
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const onClick = (event: MouseEvent) => {
      if (overItem.current || (event.target instanceof Element && event.target.closest("[data-testid=run-timeline-link-tick]"))) return;
      const hit = hitRef.current(event.clientX, event.clientY);
      if (hit !== null) onLinks.current(hit.members);
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

    if (state === null && !(event.target instanceof Element && event.target.closest("[data-testid=run-timeline-link-tick]"))) {
      const hit = overItem.current ? null : hitRef.current(event.clientX, event.clientY);
      hoverArrowTo(hit);
      event.currentTarget.style.cursor = hit === null ? "" : "pointer";
    }
    const track = trackRect();
    if (!state || !track || track.width <= 0) return;
    const dx = event.clientX - state.x;
    if (!state.panning) {
      if (Math.abs(dx) < DRAG_THRESHOLD_PX) return;
      state.panning = true;

      event.currentTarget.setPointerCapture(state.id);
    }
    onView(panView(axis, state.view, base, -dx / track.width));
  };
  const endDrag = () => {
    if (drag.current?.panning) {

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
  const sourceLabel = (source: string) => {
    if (source.startsWith("agent:")) return t("sourceFromAgent", { id: source.slice("agent:".length) });
    if (source.startsWith(EXTERNAL_AGENT_PREFIX)) return t("sourceFromExternalAgent", { name: externalAgentName(source) });
    return source;
  };
  const sources =
    highlight !== null && (highlight.cls === "human" || highlight.cls === "agent")
      ? [...new Set(agents.flatMap((agent) => inboundSources(agent.data.units, highlight.cls as "human" | "agent")))].sort()
      : [];

  const describe = (target: AgentSelection | null) => {
    const owner = target === null ? undefined : byId.get(target.agent);
    if (target === null || owner === undefined) return null;
    const line = readoutText(target.selection, { data: owner.data, t, unitLabel, sourceLabel });
    return line !== null && agents.length > 1 ? t("readoutAgent", { id: target.agent, line }) : line;
  };
  const hoveredLinks = hoverArrow === null ? [] : links.filter((l) => hoverKeys.has(l.key));
  const readout = (() => {
    if (hoveredLinks.length === 0) return describe(hover);
    const first = hoveredLinks[0];

    const same = (pick: (l: ResolvedLink) => number | null, label: string | null) => (hoveredLinks.every((l) => pick(l) === pick(first)) ? endLabel(pick(first)) : (label ?? ""));
    const common = {
      kind: linkLabels[first.kind],
      from: same((l) => l.link.sender, t("otherAgents")),
      to: same((l) => l.link.receiver, t("otherAgents")),
    };
    if (hoveredLinks.length === 1) return t("readoutLink", { ...common, time: formatShort(first.link.ts) });
    const times = hoveredLinks.map((l) => Date.parse(l.link.ts));
    return t("readoutLinkGroup", {
      ...common,
      count: hoveredLinks.length,
      start: formatShort(new Date(Math.min(...times)).toISOString()),
      end: formatShort(new Date(Math.max(...times)).toISOString()),
    });
  })();

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

  const spoken = describe(selection);
  const empty = loaded.every(({ data }) => data.nodes.length === 0 && data.units.length === 0 && data.units_omitted === 0);

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
          {data.units_omitted > 0 ? (
            <p className={cn(FLEX, "h-full items-center text-xs text-muted-foreground")} data-testid="run-timeline-zoom-details">
              {t("zoomForDetails", { count: data.units_omitted })}
            </p>
          ) : canvasFor(agent, UNITS_ROW, UNIT_ROW_PX, (p, layout) =>
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
      onPointerLeave={() => hoverArrowTo(null)}
      onClickCapture={(event) => {
        if (suppressClick.current) {
          event.stopPropagation();
          event.preventDefault();
        }
      }}
      className="relative select-none space-y-3 rounded-[10px] border border-border bg-card p-3"
    >
      <LinksCanvas links={shownLinks} selectedKeys={selectedKeys} hoverKeys={hoverKeys} axis={axis} view={view} maxArrows={maxArrows} hitRef={hitRef} />
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

      {(["user", "other"] as const)
        .filter((row) => (row === "user" ? showUser : showOther))
        .map((row) => (
          <LinkRowGroup
            key={row}
            variant={row}
            links={groupLinks(shownLinks)[row]}
            axis={axis}
            viewU={viewU}
            selectedKey={linkKey}
            onHover={(key) => hoverArrowTo(key === null ? null : { key, members: [key] })}
            onSelect={onSelectLink}
            onClose={() => (row === "user" ? onShowUser(false) : onShowOther(false))}
          />
        ))}

      <RunTimelineAxis view={view} base={base} onView={onView} axis={axis} />

      <RunTimelineLegend
        highlight={highlight}
        onHighlight={onHighlight}
        classLabel={classLabel}
        sources={sources}
        sourceLabel={sourceLabel}
        linkKinds={linkKinds}
        linkLabels={linkLabels}
        linkCounts={linkCounts}
        onToggleLinkKind={onToggleLinkKind}
      />

      {empty ? <p className="pt-1 text-xs text-muted-foreground">{t("empty")}</p> : null}
    </div>
  );
}
