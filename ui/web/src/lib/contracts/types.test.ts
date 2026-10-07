// Exhaustive wire fixtures force optional schema additions into projection tests.
// Future optional extensions survive remapping without another allowlist.
import { expect, it } from "vitest";
import { projectAgentStatus } from "./types";
import type { WireAgentCard, WireAgentRow } from "./types";

const availability: NonNullable<WireAgentRow["availability"]> = {
  reason: "launch_unreachable", observed_at: "2026-09-01T00:00:00Z",
  evidence_at: "2026-09-01T00:00:00Z", admission_outcome: null,
};
const detail: Required<WireAgentRow> = {
  last_launch_attempt_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  agent_id: 7, spawner: "user", fork_source_agent_id: null,
  fork_source_checkpoint_id: "detail-only-checkpoint", status: "idling", pid: 100,
  spawned_at: "2026-09-01T00:00:00Z", started_at: "2026-09-01T00:00:00Z",
  last_active_at: "2026-09-01T00:00:00Z", last_inbound_at: "2026-09-01T00:00:00Z",
  label: "detail row", machine: "test-host", supports_vision: true,
  liveness_state: "unknown", last_probe_at: "2026-09-01T00:00:00Z",
  availability, observation: null, notices_awaiting_response: [],
  unread_notice_count: 2, heartbeat_paused_until: null,
};
const wireCard: Required<WireAgentCard> = {
  agent_id: 9, spawner: "user", fork_source_agent_id: null, status: "running", pid: 100,
  spawned_at: "2026-09-01T00:00:00Z", started_at: "2026-09-01T00:00:00Z",
  last_active_at: "2026-09-01T00:00:00Z", last_inbound_at: "2026-09-01T00:00:00Z",
  label: "card", machine: "test-host", supports_vision: true, liveness_state: "unknown",
  availability, observation: { runtime_owner: "unknown" }, awaiting_response_count: 3,
  highest_notice_priority: "P2", unread_notice_count: 2, heartbeat_paused_until: null,
  open_impersonation_session_id: null, open_impersonation_status: null,
};
function card(overrides: Partial<WireAgentCard>): Required<WireAgentCard> {
  return { ...wireCard, ...overrides };
}

it("keeps every card field and input identity when status is unchanged", () => {
  const input = card({ status: "idling", open_impersonation_status: "requested" });
  expect(projectAgentStatus(input)).toBe(input);
});
it("keeps optional extensions and every card field when an active lease remaps status", () => {
  const input: Required<WireAgentCard> & { future_optional?: string } = {
    ...card({ open_impersonation_session_id: 8, open_impersonation_status: "active" }),
    future_optional: "card-extension",
  };
  const before = structuredClone(input);
  const projected = projectAgentStatus(input);
  expect(projected).toEqual({ ...input, status: "impersonated" });
  expect(projected).not.toBe(input);
  expect(input).toEqual(before);
  expect(projected.availability?.reason).toBe("launch_unreachable");
});
it("keeps optional detail extensions while dropping detail-only fields and aggregating notices", () => {
  const notices: WireAgentRow["notices_awaiting_response"] = (["P3", "P1", "P2"] as const).map(
    (priority, index) => ({
      id: index, title: `notice ${index}`, content: "selected-detail-only body", priority,
      require_response: true, blocking: false, created_at: "2026-09-01T00:00:00Z",
    }),
  );
  const input: Required<WireAgentRow> & { future_optional?: string } = {
    ...detail, future_optional: "detail-extension", notices_awaiting_response: notices,
  };
  const before = structuredClone(input);
  const projected = projectAgentStatus(input);
  const { notices_awaiting_response: _notices, fork_source_checkpoint_id: _fork, last_probe_at: _probe, ...shared } = input;
  expect(projected).toEqual({
    ...shared, awaiting_response_count: 3, highest_notice_priority: "P1",
    open_impersonation_session_id: null, open_impersonation_status: null,
  });
  for (const excluded of ["notices_awaiting_response", "fork_source_checkpoint_id", "last_probe_at"]) {
    expect(projected).not.toHaveProperty(excluded);
  }
  expect(input).toEqual(before);
  expect(projected.availability?.reason).toBe("launch_unreachable");
});
it("a requested or accepted lease preserves the native status and card identity", () => {
  for (const phase of ["requested", "accepted"] as const) {
    const input = card({ status: "idling", open_impersonation_session_id: 7, open_impersonation_status: phase });
    expect(projectAgentStatus(input)).toBe(input);
    expect(projectAgentStatus(input).status).toBe("idling");
  }
});
it("a terminated agent stays terminated even with a stale active lease flag", () => {
  const input = card({ status: "terminated", open_impersonation_session_id: 7, open_impersonation_status: "active" });
  expect(projectAgentStatus(input)).toBe(input);
  expect(projectAgentStatus(input).status).toBe("terminated");
});
it("a detail without a lease retains native status and the empty-notice aggregate", () => {
  const projected = projectAgentStatus({ ...detail, status: "running" });
  expect(projected.status).toBe("running");
  expect(projected.awaiting_response_count).toBe(0);
  expect(projected.highest_notice_priority).toBeNull();
  expect(projected.open_impersonation_session_id).toBeNull();
  expect(projected.open_impersonation_status).toBeNull();
});
