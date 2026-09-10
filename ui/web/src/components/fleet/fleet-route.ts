// Fleet-page route parameters — the deep links into the supervision surface.
//
// `?agent_id=` anchors an agent's notice row (reveal only — selection stays
// untouched), `?notice=` opens one notice's detail (the inspector's
// notification button, task #2909), `?task=` selects a task and reveals the
// Tasks view. Ids parse defensively: a malformed value routes nowhere rather
// than to a wrong row.

/** A positive-int id, or null when the parameter is absent or malformed. */
function routeId(raw: string | null): number | null {
  const parsed = raw === null ? Number.NaN : Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

export interface FleetRouteIds {
  agentId: number | null;
  noticeId: number | null;
  taskId: number | null;
}

export function readFleetRouteIds(search: URLSearchParams): FleetRouteIds {
  return {
    agentId: routeId(search.get("agent_id")),
    noticeId: routeId(search.get("notice")),
    taskId: routeId(search.get("task")),
  };
}
