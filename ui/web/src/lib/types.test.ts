// projectAgentStatus must keep every field an AgentRow declares. Detail rows
// (`/api/agents/<id>`) carry `notices_awaiting_response` instead of
// `awaiting_response_count`, so they take the hand-rebuilt branch — which used
// to silently drop `availability`, making the selected-agent availability strip
// read `undefined` and hide itself in every live state (task #4723).

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
  const restartingCard = {
    agent_id: 8,
    spawner: "user",
    fork_source_agent_id: null,
    status: "restarting",
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
    open_impersonation_session_id: null,
  } as unknown as WireAgentCard;
  expect(projectAgentStatus(restartingCard).availability?.reason).toBe("launch_unreachable");
});
