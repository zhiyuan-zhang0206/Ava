// Shared d3-force layout hook — used by the fleet Graph View, Task Graph,
// and Memory Graph so all three views share the same simulation engine.
// Each view passes its own node/edge signature, force params, and a radius
// function; the hook owns the simulation lifecycle and exposes settled
// positions as React state.

"use client";

import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type ForceCenter,
  type ForceCollide,
  type ForceLink,
  type ForceManyBody,
  type ForceX,
  type ForceY,
  type Simulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "d3-force";
import { useEffect, useMemo, useRef, useState } from "react";

import type { ForceParams } from "@/components/fleet/force-controls";

// ── Generic simulation types ──

export interface SimNode extends SimulationNodeDatum {
  id: number | string;
  r: number;
}

export interface SimLink extends SimulationLinkDatum<SimNode> {
  source: number | string | SimNode;
  target: number | string | SimNode;
  /** Stable view-owned identity for per-link force accessors. */
  key?: string;
}

export interface Pos {
  x: number;
  y: number;
}

export interface LayoutBox {
  placed: { node: SimNode; p: Pos }[];
  minX: number;
  minY: number;
  w: number;
  h: number;
}

export interface UseForceLayoutResult {
  positions: Map<number | string, Pos>;
  layout: LayoutBox | null;
}

export interface ForceLayoutOptions {
  // Distance beyond which the many-body charge is ignored (d3 default:
  // Infinity). Uncapped repulsion is a global force: with no springs between
  // disconnected components, every node pushes every other one across the whole
  // canvas and only the (deliberately weak) origin gravity pulls back, so
  // separate subtrees drift far apart. Capping it makes repulsion a local
  // anti-overlap force and lets gravity close the gaps between components —
  // without raising gravity, which would drown the Repulsion knob entirely.
  // A fixed per-view constant, applied at build; not a user-tunable param.
  chargeDistanceMax?: number;
  /** Optional per-link spring strength; the view owns the mapping. */
  linkStrength?: (link: SimLink) => number;
}

/**
 * Maintain a d3-force simulation for an arbitrary node/edge graph.
 *
 * The simulation rebuilds only when the node/edge SET changes (via a
 * string signature), so status/weight refreshes flow through render
 * without restarting the layout. Force params and node radii are read from
 * refs so the live-update effect can adjust them without a full rebuild.
 */
export function useForceLayout(
  nodes: readonly SimNode[],
  links: readonly SimLink[],
  params: ForceParams,
  options: ForceLayoutOptions = {},
): UseForceLayoutResult {
  const [positions, setPositions] = useState<Map<number | string, Pos>>(
    () => new Map(),
  );
  const posRef = useRef<Map<number | string, Pos>>(new Map());
  const simRef = useRef<Simulation<SimNode, SimLink> | null>(null);
  const chargeRef = useRef<ForceManyBody<SimNode> | null>(null);
  const centerRef = useRef<ForceCenter<SimNode> | null>(null);
  const collideRef = useRef<ForceCollide<SimNode> | null>(null);
  const linkRef = useRef<ForceLink<SimNode, SimLink> | null>(null);
  const fxRef = useRef<ForceX<SimNode> | null>(null);
  const fyRef = useRef<ForceY<SimNode> | null>(null);

  // Latest force params, read by the build effect (which is keyed on the
  // node/edge signature, not params) so a rebuild always uses the current knobs.
  const paramsRef = useRef(params);
  const optionsRef = useRef(options);
  // Latest node radii by id. The simulation runs on COPIES of the nodes made at
  // build time, so a radius that derives from a live knob (the node-size
  // sliders) would otherwise stay frozen until the node/edge set changed —
  // cards would grow while their collision circles kept the old size.
  const radiiRef = useRef<Map<number | string, number>>(new Map());
  useEffect(() => {
    paramsRef.current = params;
    optionsRef.current = options;
    radiiRef.current = new Map(nodes.map((n) => [n.id, n.r]));
  });

  // Rebuild signature: node ids + edge identities. A stable view-owned key
  // distinguishes parallel links without rebuilding when only their weights
  // (and therefore their live-applied strengths) change.
  const signature = useMemo(() => {
    const nids = [...nodes]
      .map((n) => String(n.id))
      .sort()
      .join(",");
    const es = links
      .map((l) => {
        const s = typeof l.source === "object" ? l.source.id : l.source;
        const t = typeof l.target === "object" ? l.target.id : l.target;
        return `${s}-${t}-${l.key ?? ""}`;
      })
      .sort()
      .join(",");
    return `${nids}|${es}`;
  }, [nodes, links]);

  // Build / rebuild simulation when the node/edge set changes.
  useEffect(() => {
    const fp = paramsRef.current;
    const prior = posRef.current;

    // Drop positions for nodes that are no longer in the graph.
    const liveIds = new Set(nodes.map((n) => n.id));
    for (const id of [...prior.keys()]) {
      if (!liveIds.has(id)) prior.delete(id);
    }

    // Seed sim nodes with prior positions if available.
    const simNodes: SimNode[] = nodes.map((n) => {
      const p = prior.get(n.id);
      return { ...n, x: p?.x, y: p?.y };
    });

    const idSet = new Set(simNodes.map((s) => s.id));
    const simLinks: SimLink[] = links.filter((l) => {
      const s = typeof l.source === "object" ? l.source.id : l.source;
      const t = typeof l.target === "object" ? l.target.id : l.target;
      return idSet.has(s) && idSet.has(t);
    });

    const charge = forceManyBody<SimNode>().strength(-fp.repulsion);
    const distanceMax = optionsRef.current.chargeDistanceMax;
    if (distanceMax != null) charge.distanceMax(distanceMax);
    const collide = forceCollide<SimNode>().radius(
      (d) => d.r + fp.collidePadding,
    );
    const link = forceLink<SimNode, SimLink>(simLinks)
      .id((d) => d.id)
      .distance(fp.linkDistance)
      .strength(optionsRef.current.linkStrength ?? fp.linkStrength);
    const center = forceCenter<SimNode>(0, 0).strength(fp.centerStrength);
    const fx: ForceX<SimNode> | null =
      fp.centerForceX > 0
        ? forceX<SimNode>(0).strength(fp.centerForceX)
        : null;
    const fy: ForceY<SimNode> | null =
      fp.centerForceY > 0
        ? forceY<SimNode>(0).strength(fp.centerForceY)
        : null;
    chargeRef.current = charge;
    centerRef.current = center;
    collideRef.current = collide;
    linkRef.current = link;
    fxRef.current = fx;
    fyRef.current = fy;

    const sim = forceSimulation(simNodes)
      .alphaDecay(fp.alphaDecay)
      .force("charge", charge)
      .force("center", center)
      .force("collide", collide)
      .force("link", link);
    if (fx) sim.force("x", fx);
    if (fy) sim.force("y", fy);

    // Frame counter: throttle React state updates during simulation.
    let frameCount = 0;
    sim.on("tick", () => {
      frameCount++;
      const snap = new Map<number | string, Pos>();
      for (const s of simNodes) {
        if (s.x != null && s.y != null) snap.set(s.id, { x: s.x, y: s.y });
      }
      posRef.current = snap;
      // Throttle to every 2nd frame (~30 fps React renders).
      if (frameCount % 2 !== 0) return;
      setPositions(snap);
    });
    sim.on("end", () => {
      const snap = new Map<number | string, Pos>();
      for (const s of simNodes) {
        if (s.x != null && s.y != null) snap.set(s.id, { x: s.x, y: s.y });
      }
      posRef.current = snap;
      setPositions(snap);
    });
    sim.alpha(0.9).restart();
    simRef.current = sim;

    return () => {
      sim.stop();
    };
  }, [signature]); // eslint-disable-line react-hooks/exhaustive-deps

  // Live-apply tunable params without rebuilding.
  useEffect(() => {
    const sim = simRef.current;
    if (!sim) return;
    chargeRef.current?.strength(-params.repulsion);
    centerRef.current?.strength(params.centerStrength);
    collideRef.current?.radius(
      (d) => (radiiRef.current.get(d.id) ?? d.r) + params.collidePadding,
    );
    linkRef.current
      ?.distance(params.linkDistance)
      .strength(options.linkStrength ?? params.linkStrength);
    if (params.centerForceX > 0) {
      if (!fxRef.current) {
        fxRef.current = forceX<SimNode>(0).strength(params.centerForceX);
        sim.force("x", fxRef.current);
      } else {
        fxRef.current.strength(params.centerForceX);
      }
    } else if (fxRef.current) {
      sim.force("x", null);
      fxRef.current = null;
    }
    if (params.centerForceY > 0) {
      if (!fyRef.current) {
        fyRef.current = forceY<SimNode>(0).strength(params.centerForceY);
        sim.force("y", fyRef.current);
      } else {
        fyRef.current.strength(params.centerForceY);
      }
    } else if (fyRef.current) {
      sim.force("y", null);
      fyRef.current = null;
    }
    sim.alphaDecay(params.alphaDecay);
    sim.alpha(0.3).restart();
  }, [params, options.linkStrength]);

  // Compute the settled layout bounding box for a fit-to-content viewBox.
  const layout = useMemo((): LayoutBox | null => {
    const placed: { node: SimNode; p: Pos }[] = [];
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const n of nodes) {
      const p = positions.get(n.id);
      if (!p) continue;
      placed.push({ node: n, p });
      minX = Math.min(minX, p.x - n.r);
      minY = Math.min(minY, p.y - n.r);
      maxX = Math.max(maxX, p.x + n.r);
      maxY = Math.max(maxY, p.y + n.r);
    }
    if (placed.length === 0) return null;
    const pad = params.zoomPadding;
    minX -= pad;
    minY -= pad;
    maxX += pad;
    maxY += pad;
    const w = Math.max(maxX - minX, 300);
    const h = Math.max(maxY - minY, 300);
    return { placed, minX, minY, w, h };
  }, [nodes, positions, params.zoomPadding]);

  return { positions, layout };
}
