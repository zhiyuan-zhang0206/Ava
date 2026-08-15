import { describe, expect, it } from "vitest";

import { buildAgentTree } from "./agent-tree";
import type { AgentRow, AgentStatus } from "./types";

let nextTs = 1000;
function ag(
  agent_id: number,
  spawner: string,
  status: AgentStatus = "running",
  /** Test-only last_active override — defaults to monotonically following spawned_at (later-inserted = more recently active). */
  lastActiveOverride?: string,
): AgentRow {
  // Later-inserted agents have a larger spawned_at (string lex sort
  // is monotonic too) → ranked first on desc.
  // last_active_at defaults to spawned_at, simulating the common case
  // of "agent has never received an inbound, falls back to spawned_at";
  // pass an explicit override to test last_active != spawned_at.
  const ts = `2026-05-04T00:00:${String(nextTs++).padStart(6, "0")}Z`;
  return {
    agent_id,
    spawner,
    fork_source_agent_id: null,
    fork_source_checkpoint_id: null,
    status,
    pid: null,
    spawned_at: ts,
    started_at: null,
    last_active_at: lastActiveOverride ?? ts,
    label: null,
    machine: "test",
    notices_awaiting_response: [], unread_notice_count: 0,
    heartbeat_paused_until: null,
    liveness_state: "online",
    last_probe_at: null,
  };
}

describe("buildAgentTree", () => {
  it("empty input → empty list", () => {
    expect(buildAgentTree([])).toEqual([]);
  });

  it("user-spawned single root agent", () => {
    const roots = buildAgentTree([ag(1, "user")]);
    expect(roots).toHaveLength(1);
    expect(roots[0].agent.agent_id).toBe(1);
    expect(roots[0].children).toEqual([]);
  });

  it("agent:N spawner attaches under its parent", () => {
    const roots = buildAgentTree([
      ag(1, "user"),
      ag(2, "agent:1"),
      ag(3, "agent:2"),
    ]);
    expect(roots).toHaveLength(1);
    expect(roots[0].agent.agent_id).toBe(1);
    expect(roots[0].children).toHaveLength(1);
    expect(roots[0].children[0].agent.agent_id).toBe(2);
    expect(roots[0].children[0].children[0].agent.agent_id).toBe(3);
  });

  it("different root spawners all sit at the top level (no more group split)", () => {
    const roots = buildAgentTree([
      ag(1, "user"),
      ag(2, "claude-code"),
      ag(3, "agent:1"),
      ag(4, "agent:2"),
    ]);
    // Top level has only non-sub-agents (1 user + 2 claude-code), sorted by id asc
    expect(roots.map((r) => r.agent.agent_id)).toEqual([1, 2]);
    const root1 = roots.find((r) => r.agent.agent_id === 1)!;
    const root2 = roots.find((r) => r.agent.agent_id === 2)!;
    expect(root1.children[0].agent.agent_id).toBe(3);
    expect(root2.children[0].agent.agent_id).toBe(4);
  });

  it("terminated agent stays in tree, preserves spawn lineage", () => {
    const roots = buildAgentTree([
      ag(1, "user"),
      ag(2, "agent:1", "terminated"),
      ag(3, "agent:1"),
    ]);
    expect(roots).toHaveLength(1);
    const root = roots[0];
    // Children ordered purely by id — status never reorders, so the
    // terminated agent 2 keeps its lineage slot ahead of the alive 3.
    expect(root.children.map((c) => c.agent.agent_id)).toEqual([2, 3]);
    expect(root.children[0].agent.status).toBe("terminated");
  });

  it("siblings sort purely by id — status does not reorder", () => {
    // 4 top-level roots with mixed statuses: id asc regardless of status,
    // so a terminated row never sinks and a running row never floats.
    const roots = buildAgentTree([
      ag(1, "user"),
      ag(2, "user", "terminated"),
      ag(3, "user"),
      ag(4, "user", "terminated"),
    ]);
    expect(roots.map((r) => r.agent.agent_id)).toEqual([1, 2, 3, 4]);
  });

  it("sub-agent stays attached to parent after suicide (the user core case)", () => {
    const roots = buildAgentTree([
      ag(1, "user"),
      ag(2, "agent:1", "terminated"),
    ]);
    expect(roots).toHaveLength(1);
    const root = roots[0];
    expect(root.agent.agent_id).toBe(1);
    expect(root.children).toHaveLength(1);
    expect(root.children[0].agent.agent_id).toBe(2);
    expect(root.children[0].agent.status).toBe("terminated");
  });

  it("broken-chain spawner=agent:999 (parent missing) → surfaces as its own root", () => {
    const roots = buildAgentTree([ag(5, "agent:999")]);
    expect(roots).toHaveLength(1);
    expect(roots[0].agent.agent_id).toBe(5);
    expect(roots[0].children).toEqual([]);
  });

  it("top-level siblings sorted by id asc within same status", () => {
    const roots = buildAgentTree([
      ag(1, "user"),
      ag(2, "user"),
      ag(3, "user"),
    ]);
    expect(roots.map((r) => r.agent.agent_id)).toEqual([1, 2, 3]);
  });

  it("last_active_at is ignored for sorting — order is stable by id", () => {
    const a = ag(1, "user", "running");
    const b = ag(2, "user", "running");
    const aRecent = { ...a, last_active_at: "2026-05-05T00:00:00.000000Z" };
    const roots = buildAgentTree([aRecent, b]);
    expect(roots.map((r) => r.agent.agent_id)).toEqual([1, 2]);
  });

  it("when last_active_at ties, breaks by id asc (smaller id first)", () => {
    const a = ag(10, "user", "running", "2026-05-05T00:00:00.000000Z");
    const b = ag(5, "user", "running", "2026-05-05T00:00:00.000000Z");
    const roots = buildAgentTree([a, b]);
    expect(roots.map((r) => r.agent.agent_id)).toEqual([5, 10]);
  });

  it("status is irrelevant to order — all six statuses sort by id asc", () => {
    // Order is fixed by id, not status: a row never jumps when its status
    // changes (idling -> running, terminate, resurrect, etc.). Mixed
    // statuses in scrambled id order still come out strictly by id.
    const roots = buildAgentTree([
      ag(1, "user", "terminated"),
      ag(2, "user", "allocated"),
      ag(3, "user", "idling"),
      ag(4, "user", "starting"),
      ag(5, "user", "restarting"),
      ag(6, "user", "running"),
    ]);
    expect(roots.map((r) => r.agent.agent_id)).toEqual([1, 2, 3, 4, 5, 6]);
  });
});

describe("buildAgentTree fork lineage", () => {
  it("UI fork (spawner='user' + fork_source set) nests under its source, not as a detached root", () => {
    // Regression: a UI-initiated fork carries spawner='user' (the spawn
    // button never sets an explicit spawner), so before fork-source
    // resolution it surfaced as a top-level root, disconnected from the
    // agent it branched from.
    const src = ag(1, "user");
    const forked: AgentRow = { ...ag(2, "user"), fork_source_agent_id: 1 };
    const roots = buildAgentTree([src, forked]);
    expect(roots).toHaveLength(1);
    expect(roots[0].agent.agent_id).toBe(1);
    expect(roots[0].children.map((c) => c.agent.agent_id)).toEqual([2]);
    expect(roots[0].children[0].isFork).toBe(true);
  });

  it("fork nests under its fork_source even when spawner points elsewhere", () => {
    // SDK fork: spawner='agent:2' is who triggered it, fork_source=1 is
    // the state origin. We nest under the state origin (lineage), and
    // mark the edge as a fork.
    const a1 = ag(1, "user");
    const a2 = ag(2, "agent:1");
    const forked: AgentRow = { ...ag(3, "agent:2"), fork_source_agent_id: 1 };
    const roots = buildAgentTree([a1, a2, forked]);
    expect(roots).toHaveLength(1);
    const root1 = roots[0];
    expect(root1.agent.agent_id).toBe(1);
    expect(root1.children.map((c) => c.agent.agent_id).sort((x, y) => x - y)).toEqual([2, 3]);
    const node3 = root1.children.find((c) => c.agent.agent_id === 3)!;
    const node2 = root1.children.find((c) => c.agent.agent_id === 2)!;
    expect(node3.isFork).toBe(true);
    expect(node2.isFork).toBe(false);
    // 3 must NOT be nested under its spawner (2)
    expect(node2.children.map((c) => c.agent.agent_id)).not.toContain(3);
  });

  it("regular spawn child + roots have isFork=false", () => {
    const roots = buildAgentTree([ag(1, "user"), ag(2, "agent:1")]);
    expect(roots[0].isFork).toBe(false);
    expect(roots[0].children[0].isFork).toBe(false);
  });

  it("fork whose source is missing → surfaces as a root but is STILL tagged isFork (not dropped)", () => {
    // The source was pruned/hard-deleted; we can't draw the edge, but the
    // node is a fork by nature — keep it in the tree and badge it so its
    // origin (#999) is still visible, rather than silently demoting it to a
    // plain root.
    const orphan: AgentRow = { ...ag(5, "user"), fork_source_agent_id: 999 };
    const roots = buildAgentTree([orphan]);
    expect(roots).toHaveLength(1);
    expect(roots[0].agent.agent_id).toBe(5);
    expect(roots[0].isFork).toBe(true);
  });

  it("orphan fork with a present spawner falls back to nesting under the spawner, still isFork", () => {
    // SDK fork: spawner='agent:1' present, fork_source=999 pruned. Don't
    // detach to root when a spawner link is available — nest under 1, tagged fork.
    const a1 = ag(1, "user");
    const orphan: AgentRow = { ...ag(2, "agent:1"), fork_source_agent_id: 999 };
    const roots = buildAgentTree([a1, orphan]);
    expect(roots).toHaveLength(1);
    expect(roots[0].agent.agent_id).toBe(1);
    expect(roots[0].children.map((c) => c.agent.agent_id)).toEqual([2]);
    expect(roots[0].children[0].isFork).toBe(true);
  });
});

describe("buildAgentTree isLast", () => {
  it("single root is last", () => {
    const roots = buildAgentTree([ag(1, "user")]);
    expect(roots[0].isLast).toBe(true);
  });

  it("multiple roots: only the last after sort has isLast=true", () => {
    const roots = buildAgentTree([ag(1, "user"), ag(2, "user"), ag(3, "user")]);
    expect(roots.map((r) => r.agent.agent_id)).toEqual([1, 2, 3]);
    expect(roots.map((r) => r.isLast)).toEqual([false, false, true]);
  });

  it("child isLast is independent of root", () => {
    const roots = buildAgentTree([
      ag(1, "user"),
      ag(2, "agent:1"),
      ag(3, "agent:1"),
      ag(4, "user"),
      ag(5, "agent:4"),
    ]);
    const rootIds = roots.map((r) => r.agent.agent_id);
    expect(rootIds).toEqual([1, 4]);
    const root4 = roots.find((r) => r.agent.agent_id === 4)!;
    const root1 = roots.find((r) => r.agent.agent_id === 1)!;
    expect(root4.isLast).toBe(true);
    expect(root1.isLast).toBe(false);

    expect(root1.children.map((c) => c.agent.agent_id)).toEqual([2, 3]);
    expect(root1.children.map((c) => c.isLast)).toEqual([false, true]);

    expect(root4.children.map((c) => c.agent.agent_id)).toEqual([5]);
    expect(root4.children[0].isLast).toBe(true);
  });

  it("deeply nested: each level has its own isLast", () => {
    const roots = buildAgentTree([
      ag(1, "user"),
      ag(2, "agent:1"),
      ag(3, "agent:2"),
    ]);
    expect(roots[0].isLast).toBe(true);
    expect(roots[0].children[0].isLast).toBe(true);
    expect(roots[0].children[0].children[0].isLast).toBe(true);
  });
});

describe("buildAgentTree ID-sort stability (RCS: no resort on state change)", () => {
  it("sibling order under an id sort is unaffected by status / last_active_at changes", () => {
    const sort = { key: "id" as const, dir: "desc" as const };
    const before = buildAgentTree(
      [
        ag(1, "user", "idling", "2026-05-05T00:00:03Z"),
        ag(2, "user", "idling", "2026-05-05T00:00:02Z"),
        ag(3, "user", "idling", "2026-05-05T00:00:01Z"),
      ],
      sort,
    ).map((n) => n.agent.agent_id);
    expect(before).toEqual([3, 2, 1]);

    // Agent 1 wakes up: running + most recently active. ID order must hold.
    const after = buildAgentTree(
      [
        ag(1, "user", "running", "2026-05-06T00:00:00Z"),
        ag(2, "user", "idling", "2026-05-05T00:00:02Z"),
        ag(3, "user", "idling", "2026-05-05T00:00:01Z"),
      ],
      sort,
    ).map((n) => n.agent.agent_id);
    expect(after).toEqual(before);
  });
});


describe("buildAgentTree hideTerminated", () => {
  it("re-parents child to grandparent when parent is terminated (single level)", () => {
    // 1(alive) → 2(terminated) → 3(alive)
    // When hideTerminated: expect 1 → 3
    const roots = buildAgentTree(
      [ag(1, "user"), ag(2, "agent:1", "terminated"), ag(3, "agent:2")],
      undefined,
      { hideTerminated: true },
    );
    expect(roots).toHaveLength(1);
    expect(roots[0].agent.agent_id).toBe(1);
    expect(roots[0].children).toHaveLength(1);
    expect(roots[0].children[0].agent.agent_id).toBe(3);
    expect(roots[0].children[0].children).toEqual([]);
  });

  it("collapses multi-level terminated chain (recursive re-parenting)", () => {
    // 1(alive) → 2(terminated) → 3(terminated) → 4(alive)
    // When hideTerminated: expect 1 → 4
    const roots = buildAgentTree(
      [
        ag(1, "user"),
        ag(2, "agent:1", "terminated"),
        ag(3, "agent:2", "terminated"),
        ag(4, "agent:3"),
      ],
      undefined,
      { hideTerminated: true },
    );
    expect(roots).toHaveLength(1);
    expect(roots[0].agent.agent_id).toBe(1);
    expect(roots[0].children).toHaveLength(1);
    expect(roots[0].children[0].agent.agent_id).toBe(4);
  });

  it("drops terminated leaf nodes (no children to re-parent)", () => {
    // 1(alive) → 2(terminated, leaf)
    const roots = buildAgentTree(
      [ag(1, "user"), ag(2, "agent:1", "terminated")],
      undefined,
      { hideTerminated: true },
    );
    expect(roots).toHaveLength(1);
    expect(roots[0].agent.agent_id).toBe(1);
    expect(roots[0].children).toEqual([]);
  });

  it("re-parents multiple children of a terminated node", () => {
    // 1(alive) → 2(terminated) → 3(alive), 4(alive)
    // When hideTerminated: expect 1 → 3, 4
    const roots = buildAgentTree(
      [
        ag(1, "user"),
        ag(2, "agent:1", "terminated"),
        ag(3, "agent:2"),
        ag(4, "agent:2"),
      ],
      undefined,
      { hideTerminated: true },
    );
    expect(roots).toHaveLength(1);
    expect(roots[0].agent.agent_id).toBe(1);
    expect(roots[0].children.map((c) => c.agent.agent_id)).toEqual([3, 4]);
  });

  it("re-parents to root when top-level parent is terminated", () => {
    // 1(terminated, spawned by user) → 2(alive)
    // When hideTerminated: 2 becomes a root
    const roots = buildAgentTree(
      [ag(1, "user", "terminated"), ag(2, "agent:1")],
      undefined,
      { hideTerminated: true },
    );
    expect(roots).toHaveLength(1);
    expect(roots[0].agent.agent_id).toBe(2);
    expect(roots[0].children).toEqual([]);
  });

  it("keeps alive siblings alongside re-parented children (mixed status)", () => {
    // 1(alive) → 2(terminated) → 3(alive), AND 1 → 5(alive)
    // When hideTerminated: expect 1 → 3, 5 (sorted by id)
    const roots = buildAgentTree(
      [
        ag(1, "user"),
        ag(2, "agent:1", "terminated"),
        ag(3, "agent:2"),
        ag(5, "agent:1"),
      ],
      undefined,
      { hideTerminated: true },
    );
    expect(roots).toHaveLength(1);
    expect(roots[0].agent.agent_id).toBe(1);
    expect(roots[0].children.map((c) => c.agent.agent_id)).toEqual([3, 5]);
  });

  it("isLast recalculated correctly after re-parenting", () => {
    // 1(alive) → 2(terminated) → 3(alive), AND 1 → 4(alive)
    // After re-parenting: 1 → 3, 4. 4 should be isLast=true.
    const roots = buildAgentTree(
      [
        ag(1, "user"),
        ag(2, "agent:1", "terminated"),
        ag(3, "agent:2"),
        ag(4, "agent:1"),
      ],
      undefined,
      { hideTerminated: true },
    );
    expect(roots[0].children.map((c) => c.agent.agent_id)).toEqual([3, 4]);
    expect(roots[0].children.map((c) => c.isLast)).toEqual([false, true]);
  });

  it("hideTerminated=false preserves original behavior (terminated stay in tree)", () => {
    // Same as the existing test: terminated agent stays in tree
    const roots = buildAgentTree(
      [ag(1, "user"), ag(2, "agent:1", "terminated"), ag(3, "agent:1")],
      undefined,
      { hideTerminated: false },
    );
    expect(roots).toHaveLength(1);
    expect(roots[0].children.map((c) => c.agent.agent_id)).toEqual([2, 3]);
    expect(roots[0].children[0].agent.status).toBe("terminated");
  });

  it("default (no options) preserves original behavior", () => {
    const roots = buildAgentTree([
      ag(1, "user"),
      ag(2, "agent:1", "terminated"),
      ag(3, "agent:1"),
    ]);
    expect(roots).toHaveLength(1);
    expect(roots[0].children.map((c) => c.agent.agent_id)).toEqual([2, 3]);
  });

  it("re-parented fork child preserves isFork flag", () => {
    // 1(alive) → 2(terminated, fork of 1) → 3(alive)
    // 3 was spawned by 2, 2 was a fork. When 2 is hidden, 3 should still
    // be re-parented under 1.
    const a1 = ag(1, "user");
    const a2: AgentRow = { ...ag(2, "agent:1", "terminated"), fork_source_agent_id: 1 };
    const a3 = ag(3, "agent:2");
    const roots = buildAgentTree([a1, a2, a3], undefined, { hideTerminated: true });
    expect(roots).toHaveLength(1);
    // 3 is re-parented under 1 (since 2 is hidden)
    expect(roots[0].children).toHaveLength(1);
    expect(roots[0].children[0].agent.agent_id).toBe(3);
    // 3 is not a fork itself (it was spawned normally by 2)
    expect(roots[0].children[0].isFork).toBe(false);
  });

  it("terminated fork whose children spawn under spawner-ancestor re-parents correctly", () => {
    // Complex case: 1(alive) → 2(alive) → 3(terminated, fork of 1) → 4(alive, spawned by 3)
    // 3 is terminated and hidden. 3's children should re-parent to 3's parent (2, not fork source 1).
    // ResolveParent: 3's fork_source=1, so parent is 1. But 3 is also spawned by agent:2.
    // Wait — fork-source wins, so 3's resolved parent is 1 even though spawner says 2.
    // When 3 is hidden: 3's children (4) re-parent to 3's parent, which is 1.
    // Expected: 1 → 2, 4 (both under 1 since 3 was under 1 due to fork-source)
    const a1 = ag(1, "user");
    const a2 = ag(2, "agent:1");
    const a3: AgentRow = { ...ag(3, "agent:2", "terminated"), fork_source_agent_id: 1 };
    const a4 = ag(4, "agent:3");
    const roots = buildAgentTree([a1, a2, a3, a4], undefined, { hideTerminated: true });
    expect(roots).toHaveLength(1);
    expect(roots[0].agent.agent_id).toBe(1);
    // Children of 1: 2 (alive, direct) and 4 (re-parented from hidden 3)
    const childIds = roots[0].children.map((c) => c.agent.agent_id);
    expect(childIds.sort((x, y) => x - y)).toEqual([2, 4]);
  });
});
