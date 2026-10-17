"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useState } from "react";

import { ContextBreakdownCard, type CategoryHighlight } from "@/components/inspector/context-breakdown-body";
import { AgentViewToolbar } from "@/components/run-timeline/agent-view/agent-view-toolbar";
import { AgentPending } from "@/components/run-timeline/agent-view/agent-view-group";
import { useTimelineWindows } from "@/components/run-timeline/agent-view/use-timeline-windows";
import type { AgentSelection } from "@/components/run-timeline/agent-view/agent-view-nav";
import { NodeDetail, UnitDetail } from "@/components/run-timeline/run-timeline-detail";
import { LinkDetail } from "@/components/run-timeline/run-timeline-link-detail";
import { LinkGroupDetail } from "@/components/run-timeline/run-timeline-link-group-detail";
import { LINK_KINDS, MAX_ARROWS, resolveLinks, type LinkKind } from "@/components/run-timeline/model/timeline-links";
import { RunTimelineRows, type AgentEntry } from "@/components/run-timeline/run-timeline-rows";
import { RunTimelineWorkspace } from "@/components/run-timeline/run-timeline-workspace";
import { PageHeader } from "@/components/shell/page-header";
import {
  categoryClass,
  classCategory,
  clampViewport,
  MIN_VIEW_MS,
  contextPoint,
  levelsTopFirst,
  nodeAncestors,
  nodeChildren,
  viewportOf,
  type Highlight,
  type Viewport,
} from "@/components/run-timeline/model/timeline-model";
import type { UnitHeights } from "@/components/run-timeline/canvas/run-timeline-paint";
import type { RowOptions } from "@/components/run-timeline/model/timeline-nav";
import { api } from "@/lib/transport/api";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0 } from "@/lib/layout/layout";
import { formatAbsolute } from "@/lib/format/time";
import { cn } from "@/lib/format/utils";

function parseAgents(segment: string): number[] | null {
  const ids: number[] = [];
  for (const part of decodeURIComponent(segment).split(",")) {
    const id = Number(part);
    if (part.trim() === "" || !Number.isInteger(id) || id < 0) return null;
    if (!ids.includes(id)) ids.push(id);
  }
  return ids;
}

const NO_AGENTS: number[] = [];

const pathFor = (ids: readonly number[]) => `/insights/run/${ids.join(",")}`;

// meiva:note
// The URL preserves agents in addition order without duplicates and the last agent cannot be removed. Message indices belong to their snapshot: replacing it invalidates the selection before effects run. Block selection and arrow selection exclude each other. The context card follows the selected agent and selected message/node, otherwise the last request in view of the first loaded agent. The page owns legend highlighting so zoom and pan retain it.
// meiva:link ui/web/src/docs/frontend-components/run-timeline/window-reads.meiva.okf.md
export default function AgentViewPage({ params }: { params: Promise<{ agents: string }> }) {
  const t = useTranslations("runTimeline");

  const [agentIds, setAgentIds] = useState<number[] | null>(null);
  const [paramsResolved, setParamsResolved] = useState(false);
  const [picked, setPicked] = useState<{ value: AgentSelection; snapshot: string | null | undefined } | null>(null);

  const [highlight, setHighlight] = useState<Highlight | null>(null);

  const [viewport, setViewport] = useState<Viewport | null>(null);

  const [linkKeys, setLinkKeys] = useState<readonly string[]>([]);
  const setLinkKey = (key: string | null) => setLinkKeys(key === null ? [] : [key]);
  const [linkKinds, setLinkKinds] = useState<ReadonlySet<LinkKind>>(() => new Set(LINK_KINDS));
  const [showUser, setShowUser] = useState(true);
  const [showOther, setShowOther] = useState(true);
  const [maxArrows, setMaxArrows] = useState<number>(MAX_ARROWS);
  const [levels, setLevels] = useState<number | null>(null);

  const [contextSize, setContextSize] = useState(false);
  const [unitHeights, setUnitHeights] = useState<UnitHeights>("tokens");
  const options: RowOptions = useMemo(() => ({ levels, contextSize }), [levels, contextSize]);

  useEffect(() => {
    let cancelled = false;
    params
      .then(({ agents }) => {
        if (!cancelled) {
          setAgentIds(parseAgents(agents));
          setParamsResolved(true);
        }
      })
      .catch(() => {
        if (!cancelled) setParamsResolved(true);
      });
    return () => {
      cancelled = true;
    };
  }, [params]);

  const ids = agentIds ?? NO_AGENTS;
  const { reads, retry, initial } = useTimelineWindows(ids, viewport);

  const selection = picked !== null && picked.snapshot === initial.at(ids.indexOf(picked.value.agent))?.data?.snapshot ? picked.value : null;
  const setSelection = (next: AgentSelection | null) => setPicked(next === null ? null : {
    value: next, snapshot: initial.at(ids.indexOf(next.agent))?.data?.snapshot,
  });
  const setAgents = (next: number[]) => {
    setAgentIds(next);

    setViewport(null);
    window.history.replaceState(null, "", pathFor(next));
  };
  const addAgent = (id: number) => {
    if (!ids.includes(id)) setAgents([...ids, id]);
  };
  const removeAgent = (id: number) => {
    if (ids.length <= 1) return;
    setLinkKey(null);
    if (selection?.agent === id) setSelection(null);
    setAgents(ids.filter((other) => other !== id));
  };

  const entries: AgentEntry[] = useMemo(
    () =>
      ids.map((id, i): AgentEntry => {
        const read = reads.at(i);
        if (read?.data !== undefined) return { id, status: "loaded", data: read.data };
        return { id, status: read?.isError ? "failed" : "loading" };
      }),
    [ids, reads],
  );
  const loaded = useMemo(
    () => entries.flatMap((entry) => (entry.status === "loaded" ? [{ id: entry.id, data: entry.data }] : [])),
    [entries],
  );

  const linkWindow = useMemo(() => {
    const spans = loaded.map(({ data }) => viewportOf(data.window));
    return spans.length === 0
      ? null
      : { from: new Date(Math.min(...spans.map((w) => w.from))).toISOString(), to: new Date(Math.max(...spans.map((w) => w.to))).toISOString() };
  }, [loaded]);
  const linksRead = useQuery({
    queryKey: ["run-timeline-links", [...ids].sort((a, b) => a - b).join(","), linkWindow?.from, linkWindow?.to],
    queryFn: () => api.getRunTimelineLinks(ids, linkWindow as { from: string; to: string }),
    enabled: linkWindow !== null,
  });
  const links = useMemo(
    () => resolveLinks(linksRead.data?.links ?? [], new Set(ids), new Map(loaded.map(({ id, data }) => [id, data]))),
    [linksRead.data, ids, loaded],
  );
  const selectedLinks = links.filter((l) => linkKeys.includes(l.key));

  if (paramsResolved && agentIds === null) {
    return (
      <main id="main-content">
        <p className="p-6 font-mono text-sm text-destructive">{t("invalidAgent")}</p>
      </main>
    );
  }

  const windows = initial.flatMap(({ data }) => data === undefined ? [] : [viewportOf(data.lifetime ?? data.window)]);
  const initialWindows = initial.flatMap(({ data }) => data === undefined ? [] : [viewportOf(data.window)]);
  const base: Viewport | null =
    windows.length === 0
      ? null
      : { from: Math.min(...windows.map((w) => w.from)), to: Math.max(...windows.map((w) => w.to)) };
  const firstView = initialWindows.length === 0 ? null : {
    from: Math.min(...initialWindows.map((w) => w.from)), to: Math.max(...initialWindows.map((w) => w.to)),
  };
  const view = base && viewport ? clampViewport(viewport, base) : firstView;

  const dataOf = (id: number) => loaded.find((agent) => agent.id === id)?.data;

  const focus = selection !== null && dataOf(selection.agent) !== undefined ? selection.agent : loaded.at(0)?.id;
  const focusData = focus === undefined ? undefined : dataOf(focus);
  const mine = selection !== null && selection.agent === focus ? selection.selection : null;
  const selectedNode =
    mine?.kind === "node" ? focusData?.nodes.find((node) => node.id === mine.id) : undefined;
  const selectedUnit =
    mine?.kind === "unit"
      ? focusData?.units.find(
          (unit) => unit.i0 === mine.i0 && unit.i1 === mine.i1 && unit.kind === mine.unitKind,
        )
      : undefined;

  const contextAt = focusData && view && (focusData.units_omitted === 0 || selectedNode !== undefined || selectedUnit !== undefined)
    ? contextPoint(mine, focusData.nodes, focusData.units, view) : undefined;
  const categoryHighlight: CategoryHighlight = {
    active: highlight === null ? null : classCategory(highlight.cls),
    has: (category) => categoryClass(category) !== null,
    onToggle: (category) => {
      const cls = categoryClass(category);
      if (cls === null) return;
      setHighlight(highlight?.cls === cls ? null : { cls, source: null });
    },
  };
  const maxLevels = Math.max(0, ...loaded.map(({ data }) => levelsTopFirst(data.nodes).length));

  const focusLink = (key: string) => {
    const target = links.find((l) => l.key === key);
    if (target === undefined || view === null) return;
    const times = selectedLinks.map((l) => Date.parse(l.link.ts));
    const width = Math.min(Math.max((Math.max(...times) - Math.min(...times)) * 20, MIN_VIEW_MS), view.to - view.from);
    const at = Date.parse(target.link.ts);
    if (base !== null) setViewport(clampViewport({ from: at - width / 2, to: at + width / 2 }, base));
    setSelection(null);
    setLinkKeys([key]);
  };
  const select = (agent: number) => (id: string) => setSelection({ agent, selection: { kind: "node", id } });

  const main = (
    <>
      <AgentViewToolbar
        agentIds={ids}
        onAdd={addAgent}
        levels={levels}
        maxLevels={maxLevels}
        onLevels={setLevels}
        contextSize={contextSize}
        onContextSize={setContextSize}
        unitHeights={unitHeights}
        onUnitHeights={setUnitHeights}
        showUser={showUser}
        onShowUser={(on) => {
          setShowUser(on);
          if (!on) setLinkKey(null);
        }}
        showOther={showOther}
        onShowOther={(on) => {
          setShowOther(on);
          if (!on) setLinkKey(null);
        }}
        maxArrows={maxArrows}
        onMaxArrows={setMaxArrows}
      />
      {reads.map((read, i) => read.preparing ? (
        <p key={ids[i]} role="status" className="text-xs text-muted-foreground">{t("preparingHistory", { id: ids[i] })}</p>
      ) : null)}
      {base && view ? (
        <>
          <p className="text-xs text-muted-foreground" data-testid="run-timeline-window">
            {t("windowSummary", {
              from: formatAbsolute(new Date(view.from).toISOString()),
              to: formatAbsolute(new Date(view.to).toISOString()),
              nodes: loaded.reduce((sum, { data }) => sum + data.nodes.length, 0),
              units: loaded.reduce((sum, { data }) => sum + data.units.length + data.units_omitted, 0),
            })}
          </p>
          {reads.map((read, i) => read.pending ? (
            <p key={ids[i]} role="status" className="text-xs text-muted-foreground">{t("windowLoading", { id: ids[i] })}</p>
          ) : read.windowError ? (
            <p key={ids[i]} role="alert" className="text-xs text-destructive">
              {t("windowFailed", { id: ids[i] })} <button type="button" onClick={() => retry(ids[i])}>{t("retry")}</button>
            </p>
          ) : null)}
          <RunTimelineRows
            entries={entries}
            base={base}
            view={view}
            onView={setViewport}
            selection={selection}
            onSelect={(next) => {
              setLinkKey(null);
              setSelection(next);
            }}
            highlight={highlight}
            onHighlight={setHighlight}
            options={options}
            unitHeights={unitHeights}
            onRemove={ids.length > 1 ? removeAgent : null}
            onRetry={retry}
            links={links}
            showUser={showUser}
            onShowUser={(on) => {
              setShowUser(on);
              if (!on) setLinkKey(null);
            }}
            showOther={showOther}
            onShowOther={(on) => {
              setShowOther(on);
              if (!on) setLinkKey(null);
            }}
            linkKinds={linkKinds}
            onToggleLinkKind={(kind) =>
              setLinkKinds((kinds) => {
                const next = new Set(kinds);
                if (!next.delete(kind)) next.add(kind);
                return next;
              })
            }
            linkKeys={linkKeys}
            maxArrows={maxArrows}
            onSelectLinks={(keys) => {
              setSelection(null);
              setLinkKeys(keys);
            }}
          />
          {linksRead.isError ? (
            <p role="alert" className="text-xs text-destructive" data-testid="run-timeline-links-failed">
              {t("linksFailed")}
            </p>
          ) : null}
        </>
      ) : (
        <div className="space-y-3">
          {entries.map((entry) => (
            <AgentPending
              key={entry.id}
              agentId={entry.id}
              failed={entry.status === "failed"}
              onRetry={retry}
              onRemove={ids.length > 1 ? removeAgent : null}
            />
          ))}
        </div>
      )}

      {focus !== undefined && contextAt !== undefined ? (
        <ContextBreakdownCard agentId={focus} at={contextAt} snapshot={focusData?.snapshot ?? undefined} categoryHighlight={categoryHighlight} />
      ) : null}
    </>
  );

  const side = selectedLinks.length > 1 ? (
    <LinkGroupDetail links={selectedLinks} onSelect={focusLink} />
  ) : selectedLinks.length === 1 ? (
    <LinkDetail key={selectedLinks[0].key} resolved={selectedLinks[0]} snapshot={dataOf(selectedLinks[0].to.agent)?.snapshot ?? undefined} onAddAgent={addAgent} />
  ) : selectedNode ? (
    <NodeDetail
      key={`n${focus}-${selectedNode.id}`}
      agentId={focus ?? 0}
      snapshot={focusData?.snapshot ?? undefined}
      node={selectedNode}
      ancestors={focusData ? nodeAncestors(selectedNode, focusData.nodes) : []}
      childNodes={focusData ? nodeChildren(selectedNode, focusData.nodes) : []}
      onSelectNode={focus === undefined ? () => undefined : select(focus)}
    />
  ) : selectedUnit ? (
    <UnitDetail
      key={`u${focus}-${selectedUnit.kind}${selectedUnit.i0}-${selectedUnit.i1}`}
      agentId={focus ?? 0}
      snapshot={focusData?.snapshot ?? undefined}
      unit={selectedUnit}
      parent={focusData?.nodes.find((node) => node.id === selectedUnit.parent) ?? null}
      onSelectNode={focus === undefined ? () => undefined : select(focus)}
    />
  ) : (
    <p className="text-sm text-muted-foreground">{t("detailEmpty")}</p>
  );

  return (
    <main id="main-content" className={cn(FLEX, FLEX_1, MIN_H_0, FLEX_COL)}>
      <PageHeader title={t("title")} backHref="/insights" backLabel={t("backToInsights")} />
      <RunTimelineWorkspace main={main} side={side} />
    </main>
  );
}
