"use client";

// /insights/run/[agentId] — run→turn→call timeline visualization.
//
// One agent's session as a three-level waterfall:
//   run   = the whole window (default: the session route from context
//           initialization to the latest compact — user ruling; adjustable
//           via the from/to inputs).
//   turn  = one row on each panel: wall-clock duration on the TIME panel,
//           absolute input tokens on the TOKEN panel.
//   call  = expand a turn to see its LLM call + execs.
//
// Layout follows the user's 5-point design ruling:
//   - TWO panels with INDEPENDENT axes (time axis / token axis each have
//     their own linear scale — they are deliberately NOT aligned);
//   - corresponding nodes across the two panels are connected by dashed
//     vertical connectors;
//   - the token axis shows the token itself (absolute count), input vs
//     output color-differentiated, no cache/input/output split;
//   - the default window is the complete initialize→compact session route.
//
// Zoom levels resize the visible window; the session-route button restores
// the default. Panning is done through the from/to inputs (drag is left to
// a later iteration — this page is read-first).

import { useQuery } from "@tanstack/react-query";
import { MessageSquare, RotateCcw } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "@/lib/api";
import type { RunTimelineResponse, RunTimelineRow } from "@/lib/types";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0 } from "@/lib/layout";
import { cn } from "@/lib/utils";

const ZOOM_LEVELS = [
  { label: "30m", hours: 0.5 },
  { label: "1h", hours: 1 },
  { label: "6h", hours: 6 },
  { label: "12h", hours: 12 },
  { label: "24h", hours: 24 },
] as const;

const PANEL_HEIGHT = 64;
const CONNECTOR_COLOR = "#94a3b8";

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function fmtDur(s: number): string {
  if (s >= 3600) return `${(s / 3600).toFixed(1)}h`;
  if (s >= 60) return `${(s / 60).toFixed(1)}m`;
  return `${s.toFixed(0)}s`;
}

function toLocalInput(iso: string): string {
  // datetime-local expects the LOCAL wall clock; ISO-Z must be converted or
  // the field shows/edits a time shifted by the UTC offset (QA N1).
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtTime(iso: string): string {
  // Asia/Shanghai is the user's timezone; format via the browser locale.
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

/** Pure linear scale helper — the page renders its own SVG axes (no chart
 *  library; the fleet graph's d3 is force/zoom, not scales). */
function scale(domainMin: number, domainMax: number, rangeMin: number, rangeMax: number) {
  const span = domainMax - domainMin || 1;
  return (v: number) => rangeMin + ((v - domainMin) / span) * (rangeMax - rangeMin);
}

function useRunTimeline(agentId: number, from: string | null, to: string | null) {
  return useQuery({
    queryKey: ["run-timeline", agentId, from, to] as const,
    queryFn: () => api.getRunTimeline(agentId, { from: from ?? undefined, to: to ?? undefined, limit: 2000 }),
    // Params arrive as a Promise — never fire with the NaN placeholder id.
    enabled: Number.isFinite(agentId),
    staleTime: 30_000,
  });
}

export default function RunTimelinePage({ params }: { params: Promise<{ agentId: string }> }) {
  const [resolved, setResolved] = useState<{ agentId: string } | null>(null);
  useEffect(() => {
    let cancelled = false;
    params
      .then((p) => { if (!cancelled) setResolved(p); })
      .catch(() => undefined);
    return () => { cancelled = true; };
  }, [params]);

  const agentId = resolved ? Number(resolved.agentId) : Number.NaN;
  const valid = resolved !== null && Number.isFinite(agentId);

  const [from, setFrom] = useState<string | null>(null); // session route default
  const [to, setTo] = useState<string | null>(null);
  const [zoomHours, setZoomHours] = useState<number | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);

  const { data, error, isFetching, refetch } = useRunTimeline(agentId, from, to);

  // Zoom applies a window centered on the latest turn (like the prototype:
  // the window END is fixed at the data end).
  const effectiveFrom = useMemo(() => {
    if (zoomHours == null) return from;
    const end = data && data.rows.length > 0 ? new Date(data.rows[data.rows.length - 1].end) : new Date();
    const start = new Date(end.getTime() - zoomHours * 3600_000);
    return start.toISOString();
  }, [zoomHours, from, data]);
  const effectiveTo = useMemo(() => {
    if (zoomHours == null) return to;
    const end = data && data.rows.length > 0 ? new Date(data.rows[data.rows.length - 1].end) : new Date();
    return end.toISOString();
  }, [zoomHours, to, data]);

  const resetSessionRoute = useCallback(() => {
    setFrom(null);
    setTo(null);
    setZoomHours(null);
    setExpanded(null);
  }, []);

  useEffect(() => {
    document.title = resolved ? `Run timeline — agent #${resolved.agentId}` : "Run timeline";
  }, [resolved]);

  if (!valid) {
    return (
      <div className={cn(FLEX, FLEX_1, MIN_H_0, "items-center justify-center text-sm text-muted-foreground")}>
        Invalid agent id
      </div>
    );
  }

  return (
    <div className={cn(FLEX, FLEX_1, MIN_H_0, FLEX_COL)}>
      <header className={cn("shrink-0 items-center gap-2 border-b border-border px-4 py-2", FLEX)}>
        <h1 className="text-sm font-semibold">Run timeline</h1>
        <span className="font-mono text-xs text-muted-foreground">agent #{agentId}</span>
        <div className={cn(FLEX_1)} />
        <Link
          href="/"
          className={cn("items-center gap-1.5 rounded px-2 py-1 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-foreground", FLEX)}
          aria-label="Back to agents"
        >
          <MessageSquare className="size-4 shrink-0" aria-hidden />
          <span className="hidden sm:inline">Back to agents</span>
        </Link>
      </header>

      <div className={cn("shrink-0 items-center gap-2 border-b border-border px-4 py-2 text-xs", FLEX, "flex-wrap")}>
        <span className="text-muted-foreground">Window</span>
        <input
          type="datetime-local"
          value={from ? toLocalInput(from) : ""}
          onChange={(e) => { setFrom(e.target.value ? new Date(e.target.value).toISOString() : null); setZoomHours(null); }}
          aria-label="Window start"
          className="rounded border border-border bg-transparent px-1.5 py-0.5 font-mono text-[11px]"
        />
        <span className="text-muted-foreground">→</span>
        <input
          type="datetime-local"
          value={to ? toLocalInput(to) : ""}
          onChange={(e) => { setTo(e.target.value ? new Date(e.target.value).toISOString() : null); setZoomHours(null); }}
          aria-label="Window end"
          className="rounded border border-border bg-transparent px-1.5 py-0.5 font-mono text-[11px]"
        />
        <button
          type="button"
          onClick={resetSessionRoute}
          className="inline-flex items-center gap-1 rounded border border-border px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground hover:text-foreground"
          aria-label="Reset to session route"
        >
          <RotateCcw className="size-3" aria-hidden />
          Session route
        </button>
        <span className="mx-1 text-muted-foreground/50">|</span>
        <div className="inline-flex gap-1">
          {ZOOM_LEVELS.map((z) => (
            <button
              key={z.label}
              type="button"
              onClick={() => setZoomHours(zoomHours === z.hours ? null : z.hours)}
              className={cn(
                "rounded border px-1.5 py-0.5 font-mono text-[11px]",
                zoomHours === z.hours
                  ? "border-foreground bg-accent text-foreground"
                  : "border-border text-muted-foreground hover:text-foreground",
              )}
              aria-pressed={zoomHours === z.hours}
            >
              {z.label}
            </button>
          ))}
        </div>
        {isFetching ? <span className="text-muted-foreground">…</span> : null}
        {error ? (
          <span className="text-destructive" role="alert">
            Failed to load
            <button type="button" onClick={() => void refetch()} className="ml-2 underline">Retry</button>
          </span>
        ) : null}
      </div>

      <div className={cn("overflow-y-auto", FLEX_1, MIN_H_0)}>
        {data ? (
          <RunTimelineBody
            agentId={agentId}
            data={data}
            from={effectiveFrom}
            to={effectiveTo}
            expanded={expanded}
            onToggleTurn={setExpanded}
          />
        ) : error ? null : (
          <div className="p-6 text-sm text-muted-foreground">Loading…</div>
        )}
      </div>
    </div>
  );
}

function RunTimelineBody({
  agentId,
  data,
  from,
  to,
  expanded,
  onToggleTurn,
}: {
  agentId: number;
  data: RunTimelineResponse;
  from: string | null;
  to: string | null;
  expanded: number | null;
  onToggleTurn: (turn: number | null) => void;
}) {
  const rows = data.rows;
  const meta = data.meta;

  const [windowFrom, windowTo] = useMemo(() => {
    let lo = from
      ? new Date(from).getTime()
      : data.window_from
        ? new Date(data.window_from).getTime()
        : rows.length > 0
          ? new Date(rows[0].start).getTime()
          : Number.NaN;
    let hi = to
      ? new Date(to).getTime()
      : data.window_to
        ? new Date(data.window_to).getTime()
        : rows.length > 0
          ? new Date(rows[rows.length - 1].end).getTime()
          : Number.NaN;
    // The explicit window (from/to or zoom) is authoritative: zooming must
    // narrow the panels, so never widen the range back to the rows' span.
    // Only the NaN fallback (no data, no window) degrades to a unit range.
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) {
      if (rows.length > 0) {
        lo = new Date(rows[0].start).getTime();
        hi = new Date(rows[rows.length - 1].end).getTime();
      } else {
        return [0, 1];
      }
    }
    if (lo >= hi) {
      return [0, 1];
    }
    return [lo, hi];
  }, [rows, from, to, data.window_from, data.window_to]);

  // ── independent axes ──
  // TIME panel: wall clock, linear over [windowFrom, windowTo].
  // TOKEN panel: absolute tokens, linear over [0, max in_total] — its OWN
  // scale, deliberately not aligned with the time axis (user ruling).
  const timeScale = useMemo(() => scale(windowFrom, windowTo, 0, 1000), [windowFrom, windowTo]);
  // Token axis domain: [min in_total, max in_total] with 5% padding. The
  // session's context is near-constant (cache-dominated), so a 0-anchored
  // axis would crush every bar into the right edge; the labels stay ABSOLUTE
  // token counts either way (user ruling: token itself, absolute quantity).
  const tokenVals = rows.map((r) => r.llm?.in_total ?? 0);
  const maxTokens = useMemo(() => Math.max(...tokenVals, 1), [tokenVals]);
  const minTokens = useMemo(() => Math.min(...tokenVals, maxTokens), [tokenVals, maxTokens]);
  const tokenPad = Math.max((maxTokens - minTokens) * 0.05, 1);
  const tokenLo = Math.max(minTokens - tokenPad, 0);
  const tokenHi = maxTokens + tokenPad;
  const tokenScale = useMemo(() => scale(tokenLo, tokenHi, 0, 1000), [tokenLo, tokenHi]);

  const totalCost = meta.cost_usd;

  return (
    <div className="mx-auto max-w-5xl space-y-4 px-6 py-6">
      {/* Run header — meta stats */}
      <div className="rounded-lg border border-border p-3 font-mono text-xs">
        <div className={cn("mb-1.5 items-baseline justify-between gap-2", FLEX)}>
          <span className="text-sm font-semibold">
            Run — agent #{agentId}
          </span>
          <span className="text-muted-foreground">
            {rows.length > 0
              ? `${fmtTime(rows[0].start)} → ${fmtTime(rows[rows.length - 1].end)}`
              : "no turns in window"}
          </span>
        </div>
        <div className={cn("flex-wrap gap-x-4 gap-y-1 text-muted-foreground", FLEX)}>
          <span title="Turns in window">{meta.n_turns} turns</span>
          <span title="Wall-clock span of the window">{fmtDur(meta.wall_span_s)} wall</span>
          <span title="Sum of active turn durations">{fmtDur(meta.active_s)} active</span>
          <span title="Absolute input tokens">Σin {fmtTokens(meta.tokens_in)}</span>
          <span title="Absolute output tokens">Σout {fmtTokens(meta.tokens_out)}</span>
          <span title="Total cost at usage-time prices">${totalCost.toFixed(3)}</span>
          {meta.n_exec_failed > 0 ? (
            <span className="text-destructive" title="Failed executions">⚠ {meta.n_exec_failed} exec failed</span>
          ) : null}
          {meta.n_compact > 0 ? (
            <span title="Compactions in window">🧹 {meta.n_compact} compact</span>
          ) : null}
          {meta.truncated ? (
            <span className="text-amber-600" title="Row cap hit — narrow the window">truncated</span>
          ) : null}
        </div>
        {meta.warnings != null && meta.warnings.length > 0 ? (
          <div className="mt-1.5 space-y-0.5 text-[11px] text-amber-600" role="note">
            {meta.warnings.map((w) => (
              <div key={w}>⚠ {w}</div>
            ))}
          </div>
        ) : null}
        {data.boundaries.initialize_at != null || data.boundaries.compact_at != null ? (
          <div className="mt-1.5 text-[11px] text-muted-foreground/80">
            Session route:{" "}
            {data.boundaries.initialize_at != null ? `initialize ${fmtTime(data.boundaries.initialize_at)}` : "initialize —"}
            {" → "}
            {data.boundaries.compact_at != null ? `compact ${fmtTime(data.boundaries.compact_at)}` : "compact — (open session)"}
          </div>
        ) : null}
      </div>

      {rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">No turns in this window.</p>
      ) : (
        <DualPanelWaterfall
          rows={rows}
          windowFrom={windowFrom}
          windowTo={windowTo}
          timeScale={timeScale}
          tokenScale={tokenScale}
          tokenLo={tokenLo}
          tokenHi={tokenHi}
          compactAt={data.boundaries.compact_at}
          expanded={expanded}
          onToggleTurn={onToggleTurn}
        />
      )}
    </div>
  );
}

/** The two independent-axis panels + dashed connectors + turn expansion. */
function DualPanelWaterfall({
  rows,
  windowFrom,
  windowTo,
  timeScale,
  tokenScale,
  tokenLo,
  tokenHi,
  compactAt,
  expanded,
  onToggleTurn,
}: {
  rows: RunTimelineRow[];
  windowFrom: number;
  windowTo: number;
  timeScale: (v: number) => number;
  tokenScale: (v: number) => number;
  tokenLo: number;
  tokenHi: number;
  compactAt: string | null;
  expanded: number | null;
  onToggleTurn: (turn: number | null) => void;
}) {
  // Panel geometry: both panels are PANEL_HEIGHT tall; the connector band
  // between them is CONNECTOR_BAND tall. Bar y centers align across panels.
  const connectorBand = 40;

  // Bars: min visual width so sub-second turns stay visible.
  const MIN_BAR_PX = 1.5;
  const bars = useMemo(
    () =>
      rows.map((r) => {
        const t0 = new Date(r.start).getTime();
        const t1 = new Date(r.end).getTime();
        const xTime = timeScale(t0);
        const wTime = Math.max(timeScale(t1) - timeScale(t0), MIN_BAR_PX);
        const tokens = r.llm?.in_total ?? 0;
        // Token bars start at the axis minimum (tokenScale(tokenLo) === 0)
        // and grow with the turn's absolute input count — the left-anchored
        // bar's right edge marks the token value on the absolute axis.
        const xTok = 0;
        const wTok = Math.max(tokenScale(tokens), MIN_BAR_PX);
        return { row: r, xTime, wTime, xTok, wTok };
      }),
    [rows, timeScale, tokenScale],
  );

  const [hovered, setHovered] = useState<number | null>(null);

  return (
    <div className="space-y-1">
      {/* TIME panel */}
      <PanelLabel>Time — wall clock</PanelLabel>
      <svg
        role="img"
        aria-label="Time panel — each bar is one turn, width is wall-clock duration"
        data-testid="time-panel"
        className="w-full rounded-lg border border-border bg-background"
        viewBox={`0 0 1000 ${PANEL_HEIGHT + 18}`}
        preserveAspectRatio="none"
      >
        {/* idle backdrop: light gray behind gaps */}
        {bars.map((b, idx) => {
          const prevEnd = idx > 0 ? new Date(bars[idx - 1].row.end).getTime() : windowFrom;
          const gapStart = timeScale(prevEnd);
          const gapEnd = b.xTime;
          if (gapEnd - gapStart <= 0.5) return null;
          return (
            <rect
              key={`gap-${idx}`}
              x={gapStart}
              y={0}
              width={gapEnd - gapStart}
              height={PANEL_HEIGHT}
              className="fill-muted/50"
            />
          );
        })}
        {bars.map((b) => {
          const row = b.row;
          const anomaly = row.anomalies.length > 0;
          return (
            <g key={row.turn} transform={`translate(${b.xTime} 0)`}>
              <rect
                x={0}
                y={PANEL_HEIGHT / 2 - 10}
                width={Math.max(b.wTime, 1)}
                height={20}
                rx={2}
                fill={row.ok ? "#64748b" : "#dc2626"}
                stroke={anomaly ? "#dc2626" : "none"}
                strokeWidth={anomaly ? 1.5 : 0}
                className="cursor-pointer"
                onMouseEnter={() => setHovered(row.turn)}
                onMouseLeave={() => setHovered(null)}
                onClick={() => onToggleTurn(expanded === row.turn ? null : row.turn)}
              />
              {row.tags.includes("restart") ? (
                <line x1={0} y1={0} x2={0} y2={PANEL_HEIGHT} stroke="#6366f1" strokeWidth={2} strokeDasharray="3 2" />
              ) : null}
              {row.tags.some((t) => t.startsWith("compact")) ? (
                <line x1={0} y1={0} x2={0} y2={PANEL_HEIGHT} stroke="#a855f7" strokeWidth={2} strokeDasharray="2 2" />
              ) : null}
            </g>
          );
        })}
        {/* compact boundary line — drawn from boundaries (the compact sits
            at the session-route window end, which the row tags miss because
            Loki's range end is exclusive; QA W4) */}
        {compactAt != null ? (
          (() => {
            const x = timeScale(new Date(compactAt).getTime());
            if (x < 0 || x > 1000) return null;
            return (
              <g>
                <line x1={x} y1={0} x2={x} y2={PANEL_HEIGHT} stroke="#a855f7" strokeWidth={2} strokeDasharray="2 2" />
                <text x={x} y={10} fontSize={9} fill="#a855f7" textAnchor="end">
                  compact
                </text>
              </g>
            );
          })()
        ) : null}
        {/* axis ticks: window start / mid / end */}
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line x1={f * 1000} y1={PANEL_HEIGHT} x2={f * 1000} y2={PANEL_HEIGHT + 6} stroke="#94a3b8" />
            <text x={f * 1000} y={PANEL_HEIGHT + 15} fontSize={9} fill="#94a3b8" textAnchor={f === 0 ? "start" : f === 1 ? "end" : "middle"}>
              {fmtTime(new Date(windowFrom + (windowTo - windowFrom) * f).toISOString())}
            </text>
          </g>
        ))}
      </svg>

      {/* dashed connectors between corresponding nodes — ordinal (row i ↔ row i),
          straight vertical lines, no diagonal fan (QA W6) */}
      <svg
        role="img"
        aria-label="Connectors between the time panel and token panel"
        data-testid="connectors"
        className="w-full"
        viewBox={`0 0 1000 ${connectorBand}`}
        preserveAspectRatio="none"
      >
        {bars.map((b, idx) => {
          if (idx % 4 !== 0) return null; // keep the band legible for dense runs
          const x = ((idx + 0.5) / Math.max(bars.length, 1)) * 1000;
          return (
            <line
              key={`c-${b.row.turn}`}
              x1={x}
              y1={0}
              x2={x}
              y2={connectorBand}
              stroke={CONNECTOR_COLOR}
              strokeWidth={0.5}
              strokeDasharray="2 2"
              opacity={0.45}
            />
          );
        })}
      </svg>

{/* TOKEN panel — one horizontal slice per turn (bars never overlap) */}
      <PanelLabel>Token — absolute input tokens</PanelLabel>
      <svg
        role="img"
        aria-label="Token panel — each row is one turn, bar width is absolute input token count"
        data-testid="token-panel"
        className="w-full rounded-lg border border-border bg-background"
        viewBox={`0 0 1000 ${PANEL_HEIGHT + 18}`}
        preserveAspectRatio="none"
      >
        {bars.map((b, idx) => {
          const row = b.row;
          const outTokens = row.llm?.out_total ?? 0;
          // Per-row slices: every turn gets its own y band, so short bars are
          // never covered by longer ones (QA W5).
          const rowH = PANEL_HEIGHT / Math.max(bars.length, 1);
          const y = idx * rowH;
          const barH = Math.max(rowH - 1, 1);
          const inW = Math.max(b.wTok, 1);
          const outW = Math.max(tokenScale(outTokens), 0.5);
          return (
            <g key={row.turn}>
              <rect
                x={0}
                y={y}
                width={inW}
                height={barH}
                rx={1}
                fill="#2563eb"
                className="cursor-pointer"
                onMouseEnter={() => setHovered(row.turn)}
                onMouseLeave={() => setHovered(null)}
                onClick={() => onToggleTurn(expanded === row.turn ? null : row.turn)}
              >
                <title>{`Turn #${row.turn} · in ${fmtTokens(row.llm?.in_total ?? 0)}`}</title>
              </rect>
              {outTokens > 0 ? (
                <rect
                  x={0}
                  y={y}
                  width={Math.min(outW, inW)}
                  height={Math.min(4, barH)}
                  fill="#10b981"
                  className="cursor-pointer"
                  onMouseEnter={() => setHovered(row.turn)}
                  onMouseLeave={() => setHovered(null)}
                  onClick={() => onToggleTurn(expanded === row.turn ? null : row.turn)}
                />
              ) : null}
              {row.anomalies.length > 0 ? (
                <line x1={0} y1={y} x2={0} y2={y + barH} stroke="#dc2626" strokeWidth={2} />
              ) : null}
            </g>
          );
        })}
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line x1={f * 1000} y1={PANEL_HEIGHT} x2={f * 1000} y2={PANEL_HEIGHT + 6} stroke="#94a3b8" />
            <text x={f * 1000} y={PANEL_HEIGHT + 15} fontSize={9} fill="#94a3b8" textAnchor={f === 0 ? "start" : f === 1 ? "end" : "middle"}>
              {fmtTokens(tokenLo + (tokenHi - tokenLo) * f)}
            </text>
          </g>
        ))}
      </svg>

{/* legend */}
      <div className={cn("flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground", FLEX)}>
        <span className="inline-flex items-center gap-1"><span className="inline-block size-2.5 rounded-sm bg-[#64748b]" /> turn (time)</span>
        <span className="inline-flex items-center gap-1"><span className="inline-block size-2.5 rounded-sm bg-[#2563eb]" /> input tokens</span>
        <span className="inline-flex items-center gap-1"><span className="inline-block size-2.5 rounded-sm bg-[#10b981]" /> output tokens</span>
        <span className="inline-flex items-center gap-1"><span className="inline-block size-2.5 rounded-sm bg-[#dc2626]" /> failed turn</span>
        <span className="inline-flex items-center gap-1"><span className="inline-block h-2.5 w-2.5 border-l-2 border-[#a855f7]" /> compact</span>
        <span className="inline-flex items-center gap-1"><span className="inline-block h-2.5 w-2.5 border-l-2 border-[#6366f1]" /> restart</span>
      </div>

      {/* expanded turn → call level */}
      {expanded != null ? (
        <TurnDrilldown
          row={bars.find((b) => b.row.turn === expanded)?.row ?? null}
          onClose={() => onToggleTurn(null)}
        />
      ) : null}

      {/* hover tooltip */}
      {hovered != null ? (
        <HoverTooltip row={bars.find((b) => b.row.turn === hovered)?.row ?? null} />
      ) : null}
    </div>
  );
}

function PanelLabel({ children }: { children: React.ReactNode }) {
  return <div className="text-[10px] tracking-wide text-muted-foreground">{children}</div>;
}

function HoverTooltip({ row }: { row: RunTimelineRow | null }) {
  if (!row) return null;
  return (
    <div className="rounded border border-border bg-popover p-2 font-mono text-[11px] shadow-md">
      <div className="font-semibold">Turn #{row.turn}</div>
      <div>{fmtTime(row.start)} → {fmtTime(row.end)} · {fmtDur(row.active_s)}</div>
      {row.llm ? (
        <div>
          in {fmtTokens(row.llm.in_total)} / out {fmtTokens(row.llm.out_total)} · {row.llm.model}
          {row.llm.cost_usd != null ? ` · $${row.llm.cost_usd.toFixed(5)}` : ""}
        </div>
      ) : (
        <div className="text-muted-foreground">no llm call</div>
      )}
      {row.anomalies.length > 0 ? (
        <div className="text-destructive">{row.anomalies.join(" · ")}</div>
      ) : null}
      <div className="text-muted-foreground">trace {row.trace_id.slice(0, 12)}…</div>
    </div>
  );
}

/** Call level — the turn's LLM call + execs. */
function TurnDrilldown({ row, onClose }: { row: RunTimelineRow | null; onClose: () => void }) {
  if (!row) return null;
  return (
    <div className="rounded-lg border border-border p-3 font-mono text-xs">
      <div className={cn("mb-2 items-center justify-between", FLEX)}>
        <span className="font-semibold">Turn #{row.turn} — calls</span>
        <button type="button" onClick={onClose} aria-label="Close turn detail" className="text-muted-foreground hover:text-foreground">
          ✕
        </button>
      </div>
      <table className="w-full border-collapse text-[11px]">
        <thead>
          <tr className="text-left text-muted-foreground">
            <th className="py-0.5 pr-2 font-medium">kind</th>
            <th className="py-0.5 pr-2 font-medium">detail</th>
            <th className="py-0.5 pr-2 font-medium">tokens</th>
            <th className="py-0.5 font-medium">status</th>
          </tr>
        </thead>
        <tbody>
          {row.llm ? (
            <tr>
              <td className="py-0.5 pr-2">llm</td>
              <td className="py-0.5 pr-2">{row.llm.model}</td>
              <td className="py-0.5 pr-2">
                in {fmtTokens(row.llm.in_total)} / out {fmtTokens(row.llm.out_total)}
                {row.llm.reasoning > 0 ? ` (reason ${fmtTokens(row.llm.reasoning)})` : ""}
              </td>
              <td className="py-0.5 pr-2">{row.llm.latency_ms >= 1000 ? `${(row.llm.latency_ms / 1000).toFixed(1)}s` : `${row.llm.latency_ms.toFixed(0)}ms`}</td>
              <td className="py-0.5">{row.llm.cost_usd != null ? `$${row.llm.cost_usd.toFixed(5)}` : "unpriced"}</td>
            </tr>
          ) : (
            <tr>
              <td className="py-0.5 pr-2 text-muted-foreground" colSpan={5}>no llm call</td>
            </tr>
          )}
          {row.execs.map((e, i) => (
            <tr key={i}>
              <td className="py-0.5 pr-2">exec</td>
              <td className="py-0.5 pr-2">{e.tool}</td>
              <td className="py-0.5 pr-2">—</td>
              <td className="py-0.5">{e.ok ? "ok" : "failed"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {row.anomalies.length > 0 ? (
        <div className="mt-2 text-destructive">⚠ {row.anomalies.join(" · ")}</div>
      ) : null}
    </div>
  );
}
