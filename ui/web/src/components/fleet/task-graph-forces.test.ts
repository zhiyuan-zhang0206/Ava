// TaskGraph force-parameter contract — pins that the SHARED force defaults
// (FORCE_DEFAULTS, the Agent Graph's band) actually lay out square task nodes
// sanely. The task graph now renders through the same ForceGraph component as
// the agent graph — same defaults, same slider surface (FORCE_GROUPS), same
// physics. Task squares are smaller than the old 180×58 cards (side = 2r, r ∈
// [18, 26]), so the old "repulsion dead zone below ~1000" problem is gone:
// the collide floor (~63px centre-to-centre) sits far under the default
// repulsion 360, so the knob has real authority across the whole band.
//
// This is a pure d3-force spec (no React): it mirrors useForceLayout's force
// construction (charge + center + collide + link, no distanceMax cap) and
// settles a fixed tree, then measures spread. The mirror MUST stay in sync with
// useForceLayout — in particular the absence of a forceManyBody().distanceMax()
// cap. d3-force settles deterministically here (phyllotaxis seed, no coincident
// nodes → no jiggle randomness), so the numbers are reproducible.

import {
  forceCenter, forceCollide, forceLink, forceManyBody,
  forceSimulation, forceX, forceY,
  type SimulationLinkDatum, type SimulationNodeDatum,
} from "d3-force";
import { describe, expect, it } from "vitest";

import { FORCE_DEFAULTS, FORCE_GROUPS } from "./force-controls";

interface N extends SimulationNodeDatum { id: number; r: number }
type L = SimulationLinkDatum<N>;

// Square collide radius: half-diagonal of a side-2r square = r·√2 (mirrors
// ForceGraph's simNodes). forceCollide radius adds the collide padding.
function collideR(r: number): number {
  return r * Math.SQRT2 + FORCE_DEFAULTS.collidePadding;
}
// The rendered radius band, as in ForceGraph.radiusOf (score normalized to the
// graph max; leaves score 0 → minR).
function radiusOf(score: number, maxScore: number): number {
  const ratio = maxScore > 0 ? score / maxScore : 0;
  return FORCE_DEFAULTS.nodeSizeMin + (FORCE_DEFAULTS.nodeSizeMax - FORCE_DEFAULTS.nodeSizeMin) * Math.sqrt(ratio);
}
const FLOOR = 2 * collideR(radiusOf(0, 1)); // two min-size squares at contact

// A 24-node tree: root → 4 branches → 3 children each → a few grandchildren.
// Enough breadth that repulsion (not just link/collide) drives the extent.
function tree(): { id: number; parent: number | null }[] {
  const nodes: { id: number; parent: number | null }[] = [{ id: 0, parent: null }];
  let next = 1;
  const kids = (parent: number, n: number) => {
    for (let i = 0; i < n; i++) nodes.push({ id: next++, parent });
  };
  kids(0, 4); // 1..4
  for (let b = 1; b <= 4; b++) kids(b, 3); // 5..16
  kids(5, 2); kids(6, 2); kids(9, 2); kids(12, 2); // 17..24
  return nodes;
}

// Mean nearest-neighbour centre distance after the layout settles under
// `repulsion` (overridden). Mirrors useForceLayout: same forces, no distanceMax.
function meanNearestNeighbour(repulsion: number): number {
  const t = tree();
  // Descendant counts → score, exactly like TaskGraph's subtreeDescendants.
  const desc = new Map<number, number>(t.map((n) => [n.id, 0]));
  const parentOf = new Map<number, number>();
  for (const n of t) if (n.parent != null) parentOf.set(n.id, n.parent);
  for (const n of t) {
    let cur = n.id;
    while (parentOf.has(cur)) {
      const p = parentOf.get(cur)!;
      desc.set(p, (desc.get(p) ?? 0) + 1);
      cur = p;
    }
  }
  const maxScore = Math.max(...desc.values(), 1);
  const nodes: N[] = t.map((n) => ({ id: n.id, r: collideR(radiusOf(desc.get(n.id) ?? 0, maxScore)) }));
  const ids = new Set(nodes.map((n) => n.id));
  const links: L[] = t
    .filter((n) => n.parent != null && ids.has(n.parent))
    .map((n) => ({ source: n.parent!, target: n.id }));

  const sim = forceSimulation<N>(nodes)
    .alphaDecay(FORCE_DEFAULTS.alphaDecay)
    .force("charge", forceManyBody<N>().strength(-repulsion))
    .force("center", forceCenter<N>(0, 0).strength(FORCE_DEFAULTS.centerStrength))
    .force("collide", forceCollide<N>().radius((d) => d.r + FORCE_DEFAULTS.collidePadding))
    .force("link", forceLink<N, L>(links).id((d) => d.id).distance(FORCE_DEFAULTS.linkDistance).strength(FORCE_DEFAULTS.linkStrength));
  if (FORCE_DEFAULTS.centerForceX > 0) sim.force("x", forceX<N>(0).strength(FORCE_DEFAULTS.centerForceX));
  if (FORCE_DEFAULTS.centerForceY > 0) sim.force("y", forceY<N>(0).strength(FORCE_DEFAULTS.centerForceY));

  sim.stop();
  sim.alpha(0.9);
  for (let i = 0; i < 600 && sim.alpha() > sim.alphaMin(); i++) sim.tick();

  let sum = 0;
  for (const a of nodes) {
    let nn = Infinity;
    for (const b of nodes) {
      if (a === b) continue;
      const d = Math.hypot((a.x ?? 0) - (b.x ?? 0), (a.y ?? 0) - (b.y ?? 0));
      if (d < nn) nn = d;
    }
    sum += nn;
  }
  return sum / nodes.length;
}

function sliderMax(key: string): number {
  for (const g of FORCE_GROUPS)
    for (const s of g.sliders) if (s.key === key) return s.max;
  throw new Error(`no slider for ${key}`);
}

describe("TaskGraph square layout under the shared defaults", () => {
  it("the shared default already spreads squares well above the collide floor", () => {
    const nn = meanNearestNeighbour(FORCE_DEFAULTS.repulsion);
    expect(nn).toBeGreaterThan(FLOOR * 1.1);
  });

  it("cranking repulsion to the shared slider max spreads squares substantially further", () => {
    const atDefault = meanNearestNeighbour(FORCE_DEFAULTS.repulsion);
    const atMax = meanNearestNeighbour(sliderMax("repulsion"));
    expect(atMax).toBeGreaterThan(atDefault * 1.25);
  });

  it("regression: the knob stays live across the whole shared band (no collide dead zone)", () => {
    // The old task cards (180×58) had a collide floor ~205px, so repulsion
    // below ~1000 was a dead zone. Squares (floor ~63px) put the entire shared
    // band (default 360 → max 2000) above the floor — turning the knob up must
    // keep spreading the board at every step, not just at the top.
    const atZero = meanNearestNeighbour(0);
    const atMid = meanNearestNeighbour(1000);
    const atMax = meanNearestNeighbour(sliderMax("repulsion"));
    expect(atMid).toBeGreaterThan(atZero * 1.15);
    expect(atMax).toBeGreaterThan(atMid * 1.15);
  });
});
