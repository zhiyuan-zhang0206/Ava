// R4 layer 1 — fold unit tests (Task #1024). The pure reducers + the
// applyEvent dispatch: every snapshot×stream reconciliation rule lives here
// and is locked at the reducer level (the hook-level behavior is covered by
// use-agents / use-agent-pages / use-all-pages / use-notices tests, which now
// drive the real fold through a stubbed EventSource).

import { describe, expect, it } from "vitest";

import type { AgentRow, PageRow, SystemEvent } from "../types";
import { AGENTS_QUERY_KEY, foldAgents, TERMINATED_AGENTS_QUERY_KEY } from "./agents";
import { foldFleetGraph } from "./graph";
import { foldAgainstCache, ALL_PAGES_QUERY_KEY } from "./index";
import { foldNotices, NOTICES_QUERY_KEY, NOTICES_RESOLVED_QUERY_KEY } from "./notices";
import { foldPages } from "./pages";
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
  notices_awaiting_response: [],
  unread_notice_count: 0,
  heartbeat_paused_until: null,
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
  const base: AgentRow[] = [baseAgent];

  it("agent_spawned appends a new agent", () => {
    const ev = {
      role: "agent_spawned",
      agent_id: 2,
      snapshot: { ...baseAgent, agent_id: 2, label: "b" },
    } as unknown as SystemEvent;
    const next = foldAgents(base, ev);
    expect(next?.map((a) => a.agent_id)).toEqual([1, 2]);
  });

  it("agent_updated replaces the row in place (same reference when nothing changed)", () => {
    const ev = {
      role: "agent_updated",
      agent_id: 1,
      snapshot: { ...baseAgent, label: "renamed" },
    } as unknown as SystemEvent;
    const next = foldAgents(base, ev);
    expect(next?.[0]?.label).toBe("renamed");
    // heartbeat-ish no-op events keep the same reference (no re-render noise)
    const noop = foldAgents(base, {
      role: "agent_updated",
      agent_id: 1,
      snapshot: { ...baseAgent, last_active_at: "2026-05-10T02:00:00Z" },
    } as unknown as SystemEvent);
    expect(noop).toBe(base);
  });

  it("moves a terminated update out of the live cache and into seeded history", () => {
    const terminated = {
      role: "agent_updated",
      agent_id: 1,
      snapshot: { ...baseAgent, status: "terminated" },
    } as unknown as SystemEvent;

    expect(foldAgents(base, terminated, "live")).toEqual([]);
    expect(foldAgents([], terminated, "terminated")?.[0]).toMatchObject({
      agent_id: 1,
      status: "terminated",
    });
  });

  it("moves a resurrected update out of terminated history and into live", () => {
    const previous = [{ ...baseAgent, status: "terminated" as const }];
    const resurrected = {
      role: "agent_updated",
      agent_id: 1,
      snapshot: { ...baseAgent, status: "idling" },
    } as unknown as SystemEvent;

    expect(foldAgents(previous, resurrected, "terminated")).toEqual([]);
    expect(foldAgents([], resurrected, "live")?.[0]?.status).toBe("idling");
  });

  it("keeps a liveness-only snapshot update even when public status stays idling", () => {
    const idling = { ...baseAgent, status: "idling" as const };
    const previous = [idling];
    const next = foldAgents(previous, {
      role: "agent_updated",
      agent_id: 1,
      snapshot: {
        ...idling,
        status: "restarting",
        liveness_state: "offline",
        last_probe_at: "2026-05-10T03:00:00Z",
      },
    } as unknown as SystemEvent);

    expect(next?.[0]).toMatchObject({
      status: "idling",
      liveness_state: "offline",
    });
    expect(next?.[0]).not.toHaveProperty("last_probe_at");
    expect(next).not.toBe(previous);
  });

  it("keeps a model-capability-only snapshot update", () => {
    const previous = [baseAgent];
    const next = foldAgents(previous, {
      role: "agent_updated",
      agent_id: 1,
      snapshot: { ...baseAgent, supports_vision: false },
    } as unknown as SystemEvent);

    expect(next?.[0]?.supports_vision).toBe(false);
    expect(next).not.toBe(previous);
  });

  it("empty-cache guard: never seeds a partial before the initial fetch", () => {
    const ev = {
      role: "agent_spawned",
      agent_id: 7,
      snapshot: baseAgent,
    } as unknown as SystemEvent;
    expect(foldAgents(undefined, ev)).toBeUndefined();
    // a fetched-empty list IS a real state — the first agent merges in
    expect(foldAgents([], ev)?.length).toBe(1);
  });

  it("label_updated patches just the label", () => {
    const ev = { role: "label_updated", agent_id: 1, label: "renamed" } as unknown as SystemEvent;
    const next = foldAgents(base, ev);
    expect(next?.[0]?.label).toBe("renamed");
  });

  it("unrelated events return undefined (no write)", () => {
    expect(foldAgents(base, { role: "notice_posted", notice_id: 1, priority: "P2", title: "t", task_id: null } as unknown as SystemEvent)).toBeUndefined();
  });
});

describe("foldPages", () => {
  const row: PageRow = {
    id: 1,
    agent_id: 1,
    name: "report",
    port: 9000,
    title: "report",
    serve_dir: null,
    url: "http://host/report",
    created_at: "2026-01-01T00:00:00Z",
    closed_at: null,
  };

  it("page_opened appends to a fetched-empty list", () => {
    const next = foldPages([], pageOpened({ name: "x" }), 1);
    expect(next?.map((p) => p.name)).toEqual(["x"]);
  });

  it("re-open replaces the row and preserves the original created_at", () => {
    const ev = pageOpened({ name: "report", port: 9999 });
    const next = foldPages([row], ev, 1);
    expect(next?.length).toBe(1);
    expect(next?.[0]?.port).toBe(9999);
    expect(next?.[0]?.created_at).toBe("2026-01-01T00:00:00Z");
  });

  it("page_closed removes by name (per-agent scope)", () => {
    const next = foldPages([row], pageClosed("report"), 1);
    expect(next).toEqual([]);
  });

  it("ignores events for other agents (per-agent scope)", () => {
    expect(foldPages([row], pageOpened({ name: "other", agent_id: 2 }), 1)).toBeUndefined();
    expect(foldPages([row], pageClosed("report", 2), 1)).toBeUndefined();
  });

  it("empty-cache guard: no partial seed before the fetch", () => {
    expect(foldPages(undefined, pageOpened({ name: "x" }), 1)).toBeUndefined();
  });

  it("all-pages scope keys rows by (agent_id, name)", () => {
    const a = foldPages([], pageOpened({ name: "x", agent_id: 1 }), null);
    const b = foldPages(a, pageOpened({ name: "x", agent_id: 2 }), null);
    expect(b?.length).toBe(2); // same name, different agent = distinct rows
    const removed = foldPages(b, pageClosed("x", 1), null);
    expect(removed?.map((p) => p.agent_id)).toEqual([2]);
  });
});

describe("invalidation policies", () => {
  it("notice_posted invalidates only the open queue", () => {
    const o = foldNotices({ role: "notice_posted", notice_id: 1, priority: "P2", title: "t", task_id: null } as unknown as SystemEvent);
    expect(o.invalidations).toEqual([{ key: NOTICES_QUERY_KEY }]);
  });

  it("notice_resolved invalidates the open queue AND the history", () => {
    const o = foldNotices({ role: "notice_resolved", agent_id: 1, notice_id: 1 } as unknown as SystemEvent);
    expect(o.invalidations.map((i) => i.key)).toEqual([NOTICES_QUERY_KEY, NOTICES_RESOLVED_QUERY_KEY]);
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

  it("writes agents + pages folds into their keys", () => {
    const cache = new Map<string, unknown>();
    cache.set(JSON.stringify(AGENTS_QUERY_KEY), [baseAgent]);
    cache.set(JSON.stringify(["all-pages"]), []);
    cache.set(JSON.stringify(["agent-pages", 1]), []);

    const outcome = foldAgainstCache(ctxWith(cache), pageOpened({ name: "p1" }));

    const writes = new Map(outcome.writes.map((w) => [JSON.stringify(w.key), w.value]));
    expect(writes.has(JSON.stringify(["agent-pages", 1]))).toBe(true);
    expect(writes.has(JSON.stringify(ALL_PAGES_QUERY_KEY))).toBe(true);
    expect((writes.get(JSON.stringify(["agent-pages", 1])) as PageRow[])[0]?.name).toBe("p1");
  });

  it("writes lifecycle transitions to both seeded roster scopes", () => {
    const cache = new Map<string, unknown>();
    cache.set(JSON.stringify(AGENTS_QUERY_KEY), [baseAgent]);
    cache.set(JSON.stringify(TERMINATED_AGENTS_QUERY_KEY), []);
    const event = {
      role: "agent_updated",
      agent_id: 1,
      snapshot: { ...baseAgent, status: "terminated" },
    } as unknown as SystemEvent;

    const outcome = foldAgainstCache(ctxWith(cache), event);
    const writes = new Map(outcome.writes.map((write) => [JSON.stringify(write.key), write.value]));

    expect(writes.get(JSON.stringify(AGENTS_QUERY_KEY))).toEqual([]);
    expect(writes.get(JSON.stringify(TERMINATED_AGENTS_QUERY_KEY))).toEqual([
      expect.objectContaining({ agent_id: 1, status: "terminated" }),
    ]);
  });

  it("never writes an un-fetched per-agent pages key (empty-cache guard)", () => {
    const cache = new Map<string, unknown>();
    cache.set(JSON.stringify(AGENTS_QUERY_KEY), [baseAgent]);
    cache.set(JSON.stringify(ALL_PAGES_QUERY_KEY), []);

    const outcome = foldAgainstCache(ctxWith(cache), pageOpened({ name: "p1" }));
    const keys = outcome.writes.map((w) => JSON.stringify(w.key));
    expect(keys).not.toContain(JSON.stringify(["agent-pages", 1]));
    expect(keys).toContain(JSON.stringify(ALL_PAGES_QUERY_KEY));
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
