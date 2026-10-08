// The agent view's selection and keyboard navigation across agents: every agent has its own rows
// (see `timeline-nav`), and the up / down arrows walk from the last row of one agent into the first
// row of the next. Pure functions; no React, no I/O.

import type { RunTimelineResponse } from "@/lib/contracts/types";

import { type AxisMap, type Selection, type Viewport } from "./timeline-model";
import {
  firstInView,
  locate,
  navigate,
  navItems,
  verticalTarget,
  type NavItem,
  type NavKey,
} from "./timeline-nav";

/** What the shared axis offers one agent's rows. */
export type Placer = Pick<AxisMap, "toU" | "unitSpan" | "nodeSpan">;

/** One loaded agent of the view, in the order it is drawn: its data, the rows it draws and its place on the shared axis. */
export interface ViewAgent {
  id: number;
  data: RunTimelineResponse;
  rows: readonly string[];
  place: Placer;
}

/** A selected (or hovered) item and the agent it belongs to: ids of nodes and indices of blocks are only unique within an agent. */
export interface AgentSelection {
  agent: number;
  selection: Selection;
}

export interface AgentCursor extends AgentSelection {
  /** The row the selection was made in (a request sits in two). */
  row: string | null;
}

export interface AgentStep {
  agent: number;
  row: string;
  item: NavItem;
}

const placers = new WeakMap<AxisMap, Map<number, Placer>>();

/** The axis as one agent's rows see it (blocks of different agents share indices); one object per axis and agent, as the row caches key on it. */
export function placerFor(axis: AxisMap, agent: number): Placer {
  let byAgent = placers.get(axis);
  if (byAgent === undefined) {
    byAgent = new Map();
    placers.set(axis, byAgent);
  }
  let place = byAgent.get(agent);
  if (place === undefined) {
    place = {
      toU: axis.toU,
      unitSpan: (unit) => axis.unitSpan(unit, agent),
      nodeSpan: (node) => axis.nodeSpan(node, agent),
    };
    byAgent.set(agent, place);
  }
  return place;
}

/**
 * The next selection of an arrow key over all agents. Within an agent it is `navigate`; up from an
 * agent's first row (down from its last) continues in the previous (next) agent's nearest row, at the
 * item overlapping the current one most on the shared axis. No selection: the first agent that has
 * something in the viewport.
 */
export function navigateAcross(
  key: NavKey,
  current: AgentCursor | null,
  agents: readonly ViewAgent[],
  view: Viewport,
): AgentStep | null {
  const first = () => {
    for (const agent of agents) {
      const found = firstInView(agent.data, agent.place, view, agent.rows);
      if (found !== null) return { agent: agent.id, ...found };
    }
    return null;
  };
  if (current === null) return first();
  const at = agents.findIndex((agent) => agent.id === current.agent);
  const agent = at < 0 ? undefined : agents[at];
  if (agent === undefined) return first();
  const here = locate(current, agent.data, agent.place, agent.rows);
  if (here === null) return first();
  const moved = navigate(key, current, agent.data, agent.place, view, agent.rows);
  if (moved !== null) return { agent: agent.id, ...moved };
  if (key === "left" || key === "right") return null;
  const up = key === "up";
  if (here.row !== (up ? agent.rows.at(0) : agent.rows.at(-1))) return null;
  for (let i = at + (up ? -1 : 1); i >= 0 && i < agents.length; i += up ? -1 : 1) {
    const next = agents[i];
    for (const row of up ? [...next.rows].reverse() : next.rows) {
      const item = verticalTarget(here.items[here.at], navItems(row, next.data, next.place), false);
      if (item !== undefined) return { agent: next.id, row, item };
    }
  }
  return null;
}
