type FleetHref = "/fleet" | `/fleet?agent_id=${number}`;

export function fleetHref(agentId: number | null): FleetHref {
  return agentId === null ? "/fleet" : `/fleet?agent_id=${agentId}`;
}
