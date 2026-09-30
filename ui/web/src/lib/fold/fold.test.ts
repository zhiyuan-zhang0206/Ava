// R4 layer 1 — fold unit tests (Task #1024). The pure reducers + the
// applyEvent dispatch: every snapshot×stream reconciliation rule lives here
// and is locked at the reducer level (the hook-level behavior is covered by
// use-agents / use-agent-pages / use-all-pages / use-notices tests, which now
// drive the real fold through a stubbed EventSource).

import { describe, expect, it, vi } from "vitest";

import type { AgentRow, SystemEvent } from "../types";
import { AGENTS_QUERY_KEY, foldAgents, AGENT_DIRECTORY_QUERY_KEY, AGENT_DETAIL_QUERY_KEY } from "./agents";
import { foldFleetGraph } from "./graph";
import { foldAgainstCache, applyEvent, ALL_PAGES_QUERY_KEY } from "./index";
import { foldNotices, NOTICES_QUERY_KEY, NOTICES_RESOLVED_QUERY_KEY } from "./notices";
import { foldTasks, TASKS_QUERY_KEY } from "./tasks";
import { FLEET_GRAPH_KEY_PREFIX } from "./graph";

const baseAgent: AgentRow = {
  agent_id: 1,
  label: "a",
  status: "running",
  last_active_at: "2026-05-10T00:00:00Z", last_inbound_at: "2026-05-10T00:00:00Z",
  spawner: "user",
  fork_source_agent_id: null,
  pid: 100,
  spawned_at: "2026-05-10T00:00:00Z",
  started_at: "2026-05-10T00:00:01Z",
  machine: "test",
  supports_vision: true,
  awaiting_response_count: 0, highest_notice_priority: null,
  unread_notice_count: 0,
  heartbeat_paused_until: null,
  open_impersonation_session_id: null,
  liveness_state: "online",
};

function pageOpened(over: Partial<SystemEvent> & { name: string }): SystemEvent {
  return {
    role: "page_opened",
    agent_id: 1,
    page_id: 10,
    port: 9100,
    title: over.name,
    url: `http://host/${over.name}`,
    ...over,
  } as SystemEvent;
}

function pageClosed(name: string, agentId = 1): SystemEvent {
  return { role: "page_closed", agent_id: agentId, name };
}

describe("foldAgents", () => {
  it("treats lifecycle state as a hint for authoritative reads", () => {
    const event = { role: "agent_updated", agent_id: 1, snapshot: baseAgent } as unknown as SystemEvent;
    expect(foldAgents(event)).toEqual({ writes: [], invalidations: [
      { key: AGENTS_QUERY_KEY }, { key: AGENT_DIRECTORY_QUERY_KEY }, { key: [...AGENT_DETAIL_QUERY_KEY, 1] },
    ] });
  });
  it.each(["notice_posted", "notice_resolved"] as const)("%s repairs scalar attention without a lifecycle hint", (role) => {
    const event = { role, agent_id: 1, notice_id: 7, priority: "P2", title: "FYI", task_id: null } as SystemEvent;
    const ctx = { getQueryData: () => undefined, setQueryData: vi.fn(), invalidateQueries: vi.fn() };
    applyEvent(ctx, event);
    expect(ctx.invalidateQueries).toHaveBeenCalledWith(AGENTS_QUERY_KEY);
    expect(ctx.invalidateQueries).toHaveBeenCalledWith(AGENT_DIRECTORY_QUERY_KEY);
    expect(ctx.invalidateQueries).toHaveBeenCalledWith([...AGENT_DETAIL_QUERY_KEY, 1]);
  });
  it("cannot accumulate terminated snapshots over an arbitrarily long session", () => {
    const state = { agents: [baseAgent], ancestors: [] };
    const writes = vi.fn();
    const ctx = { getQueryData: () => state, setQueryData: writes, invalidateQueries: vi.fn() };
    for (let id = 2; id <= 10001; id++) {
      applyEvent(ctx, { role: "agent_updated", agent_id: id, snapshot: { ...baseAgent, agent_id: id, status: "terminated" } } as unknown as SystemEvent);
    }
    expect(writes).not.toHaveBeenCalled();
    expect(state).toEqual({ agents: [baseAgent], ancestors: [] });
  });
});

describe("invalidation policies", () => {
  it("notice_posted invalidates only the open queue", () => {
    const o = foldNotices({ role: "notice_posted", notice_id: 1, priority: "P2", title: "t", task_id: null } as unknown as SystemEvent);
    expect(o.invalidations).toEqual([{ key: NOTICES_QUERY_KEY }]);
  });

  it("notice_resolved invalidates the open queue AND the history", () => {
    const o = foldNotices({ role: "notice_resolved", agent_id: 1, notice_id: 1 } as unknown as SystemEvent);
    expect(o.invalidations.map((i) => i.key)).toEqual([NOTICES_QUERY_KEY, NOTICES_RESOLVED_QUERY_KEY, ["agent-inspect-live", 1]]);
  });

  it("fleet-graph invalidates on spawn/update; tasks on create/update", () => {
    const spawn = { role: "agent_spawned", agent_id: 1, snapshot: baseAgent } as unknown as SystemEvent;
    expect(foldFleetGraph(spawn).invalidations.map((i) => i.key[0])).toContain(FLEET_GRAPH_KEY_PREFIX[0]);
    const task = { role: "task_created", agent_id: 1, task: {} } as unknown as SystemEvent;
    expect(foldTasks(task).invalidations.map((i) => i.key)).toEqual([TASKS_QUERY_KEY]);
  });
});

describe("foldAgainstCache — the dispatch", () => {
  function ctxWith(cache: Map<string, unknown>) {
    return {
      getQueryData: (key: readonly unknown[]) =>
        cache.get(JSON.stringify(key)),
      setQueryData: (key: readonly unknown[], value: unknown) => {
        cache.set(JSON.stringify(key), value);
      },
      invalidateQueries: () => undefined,
    };
  }

  it.each([pageOpened({ name: "p1" }), pageClosed("p1")])("invalidates both page keys for %s", (event) => {
    const cache = new Map<string, unknown>();
    const outcome = foldAgainstCache(ctxWith(cache), event);
    expect(outcome.writes).toEqual([]);
    expect(outcome.invalidations.map((item) => item.key)).toEqual([
      ["agent-pages", 1], ALL_PAGES_QUERY_KEY,
    ]);
  });

  it("never writes an un-fetched page key", () => {
    const cache = new Map<string, unknown>();
    const outcome = foldAgainstCache(ctxWith(cache), pageOpened({ name: "p1" }));
    expect(outcome.writes).toEqual([]);
    expect(cache.size).toBe(0);
  });

  it("emits notices invalidations from a notice event", () => {
    const cache = new Map<string, unknown>();
    cache.set(JSON.stringify(AGENTS_QUERY_KEY), [baseAgent]);
    const outcome = foldAgainstCache(ctxWith(cache), {
      role: "notice_resolved",
      agent_id: 1,
      notice_id: 1,
    } as unknown as SystemEvent);
    expect(outcome.invalidations.map((i) => JSON.stringify(i.key))).toContain(
      JSON.stringify(NOTICES_QUERY_KEY),
    );
  });

  it("events that touch nothing produce NO_FOLD", () => {
    const cache = new Map<string, unknown>();
    cache.set(JSON.stringify(AGENTS_QUERY_KEY), [baseAgent]);
    const outcome = foldAgainstCache(ctxWith(cache), {
      role: "token_usage",
      agent_id: 1,
      tokens: 1,
    } as unknown as SystemEvent);
    expect(outcome.writes).toEqual([]);
    expect(outcome.invalidations).toEqual([]);
  });
});
