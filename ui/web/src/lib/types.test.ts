// projectAgentStatus must keep every field an AgentRow declares. Detail rows
// (`/api/agents/<id>`) carry `notices_awaiting_response` instead of
// `awaiting_response_count`, so they take the hand-rebuilt branch — which used
// to silently drop `availability`, making the selected-agent availability strip
// read `undefined` and hide itself in every live state (task #4723).
//
// The impersonated-status cases below lock the rule that only an `active`
// lease means the agent is actually taken over — a `requested`/`accepted`
// lease still runs the native agent until activation reaches its next safe
// boundary (see base/agents/observation/roster.py's open_impersonation LATERAL join).

import { expect, it } from "vitest";

import { projectAgentStatus } from "./types";
import type { WireAgentCard, WireAgentRow } from "./types";

const availability = {
  reason: "launch_unreachable",
  observed_at: "2026-09-01T00:00:00Z",
  evidence_at: "2026-09-01T00:00:00Z",
  admission_outcome: null,
};

it("keeps availability through the detail-row rebuild", () => {
  const detailRow = {
    agent_id: 7,
    spawner: "user",
    fork_source_agent_id: null,
    fork_source_checkpoint_id: null,
    status: "idling",
    pid: 100,
    spawned_at: "2026-09-01T00:00:00Z",
    started_at: "2026-09-01T00:00:00Z",
    last_active_at: "2026-09-01T00:00:00Z",
    last_inbound_at: "2026-09-01T00:00:00Z",
    label: "detail row",
    machine: "test-host",
    supports_vision: true,
    liveness_state: "unknown",
    last_probe_at: null,
    availability,
    observation: null,
    notices_awaiting_response: [],
    unread_notice_count: 0,
    heartbeat_paused_until: null,
  } as unknown as WireAgentRow;
  expect(projectAgentStatus(detailRow).availability?.reason).toBe("launch_unreachable");
});

it("keeps availability through a rebuild a remapped card status triggers", () => {
  const impersonatedCard = {
    agent_id: 8,
    spawner: "user",
    fork_source_agent_id: null,
    status: "idling",
    pid: 100,
    spawned_at: "2026-09-01T00:00:00Z",
    started_at: "2026-09-01T00:00:00Z",
    last_active_at: "2026-09-01T00:00:00Z",
    last_inbound_at: "2026-09-01T00:00:00Z",
    label: "card",
    machine: "test-host",
    supports_vision: true,
    liveness_state: "unknown",
    availability,
    observation: null,
    awaiting_response_count: 0,
    highest_notice_priority: null,
    unread_notice_count: 0,
    heartbeat_paused_until: null,
    open_impersonation_session_id: 8,
    open_impersonation_status: "active",
  } as unknown as WireAgentCard;
  expect(projectAgentStatus(impersonatedCard).status).toBe("impersonated");
  expect(projectAgentStatus(impersonatedCard).availability?.reason).toBe("launch_unreachable");
});

function card(overrides: Record<string, unknown>): WireAgentCard {
  return {
    agent_id: 9,
    spawner: "user",
    fork_source_agent_id: null,
    status: "running",
    pid: 100,
    spawned_at: "2026-09-01T00:00:00Z",
    started_at: "2026-09-01T00:00:00Z",
    last_active_at: "2026-09-01T00:00:00Z",
    last_inbound_at: "2026-09-01T00:00:00Z",
    label: "card",
    machine: "test-host",
    supports_vision: true,
    liveness_state: "unknown",
    availability: null,
    observation: null,
    awaiting_response_count: 0,
    highest_notice_priority: null,
    unread_notice_count: 0,
    heartbeat_paused_until: null,
    open_impersonation_session_id: null,
    open_impersonation_status: null,
    ...overrides,
  } as unknown as WireAgentCard;
}

it("an active lease projects the card to impersonated", () => {
  const active = card({
    status: "running",
    open_impersonation_session_id: 7,
    open_impersonation_status: "active",
  });
  expect(projectAgentStatus(active).status).toBe("impersonated");
});

it("a requested (not yet active) lease does not project to impersonated", () => {
  const requested = card({
    status: "idling",
    open_impersonation_session_id: 7,
    open_impersonation_status: "requested",
  });
  expect(projectAgentStatus(requested).status).toBe("idling");
});

it("a terminated agent stays terminated even with a stale active lease flag", () => {
  const terminated = card({
    status: "terminated",
    open_impersonation_session_id: 7,
    open_impersonation_status: "active",
  });
  expect(projectAgentStatus(terminated).status).toBe("terminated");
});

it("a detail row with no lease field at all never projects to impersonated", () => {
  const detailRow = {
    agent_id: 10,
    spawner: "user",
    fork_source_agent_id: null,
    fork_source_checkpoint_id: null,
    status: "running",
    pid: 100,
    spawned_at: "2026-09-01T00:00:00Z",
    started_at: "2026-09-01T00:00:00Z",
    last_active_at: "2026-09-01T00:00:00Z",
    last_inbound_at: "2026-09-01T00:00:00Z",
    label: "detail row",
    machine: "test-host",
    supports_vision: true,
    liveness_state: "unknown",
    last_probe_at: null,
    availability: null,
    observation: null,
    notices_awaiting_response: [],
    unread_notice_count: 0,
    heartbeat_paused_until: null,
  } as unknown as WireAgentRow;
  expect(projectAgentStatus(detailRow).status).toBe("running");
});
