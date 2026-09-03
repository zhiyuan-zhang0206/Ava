import type { components } from "./types-generated";

type Observation = components["schemas"]["AgentObservation"];

/** Deadlines concern observations, never lifecycle or execution progress. */
export function observationText(value: Observation | null | undefined, now = Date.now()): string {
  const at = value?.machine_probe_at ? Date.parse(value.machine_probe_at) : NaN;
  const until = value?.machine_probe_valid_until ? Date.parse(value.machine_probe_valid_until) : NaN;
  const machine = Number.isFinite(at) && Number.isFinite(until)
    ? `${until > now ? "fresh" : "stale"} (${Math.max(0, Math.floor((now - at) / 1000))}s ago)`
    : "unknown";
  const lease = value?.runtime_lease_expires_at ? Date.parse(value.runtime_lease_expires_at) : NaN;
  const runtimeLease = Number.isFinite(lease) ? (lease > now ? "unexpired" : "expired") : "unknown";
  return `Machine probe: ${machine}; runtime lease: ${runtimeLease}; runtime owner: unknown`;
}
