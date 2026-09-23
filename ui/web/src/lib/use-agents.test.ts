import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import { useStore } from "./store";
import type { AgentRow, AgentRoster } from "./types";
import { AGENTS_QUERY_KEY, useAgents } from "./use-agents";
import { AGENT_DETAIL_QUERY_KEY } from "./fold/agents";
vi.mock("./api", async (importOriginal) => ({
  ...await importOriginal<Record<string, unknown>>(),
  api: {
    getAgentRoster: vi.fn(), getAgent: vi.fn(), listAgents: vi.fn(), spawnAgent: vi.fn(),
    terminateAgent: vi.fn(), restartAgent: vi.fn(), resurrectAgent: vi.fn(), compact: vi.fn(),
  },
}));
function row(agent_id: number, status: AgentRow["status"] = "idling"): AgentRow {
  return { agent_id, status, label: `Test #${agent_id}`, spawner: "user", fork_source_agent_id: null,
    pid: agent_id + 100, spawned_at: "2026-05-10T00:00:00Z", started_at: "2026-05-10T00:00:01Z",
    last_active_at: "2026-05-10T00:00:00Z", last_inbound_at: "2026-05-10T00:00:00Z",
    machine: agent_id === 1 ? "test-host" : "wsl", supports_vision: true, awaiting_response_count: 0,
    highest_notice_priority: null, unread_notice_count: 0, heartbeat_paused_until: null, liveness_state: "online" };
}
const MOCK_AGENTS = [row(1, "running"), row(2)];
const ACTIVE_ID_KEY = "ava.active.agent_id";
let _qc: QueryClient;
const noop = () => undefined;
function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(QueryClientProvider, { client: _qc }, children);
}
function setUrlSearch(search: string) {
  const url = new URL(window.location.href); url.search = search; window.history.replaceState(null, "", url);
}
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear(); setUrlSearch("");
  useStore.setState({ activeId: null, toast: null, openTasksNotice: null });
  _qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  vi.mocked(api.getAgentRoster).mockResolvedValue({ agents: MOCK_AGENTS, ancestors: [] });
  vi.mocked(api.getAgent).mockImplementation((id) => Promise.resolve(row(id, "terminated")));
});
afterEach(() => { cleanup(); _qc.clear(); vi.restoreAllMocks(); });

describe("bounded roster and independent selection", () => {
  it("loads one coherent tree without reading the archive", async () => {
    vi.mocked(api.getAgentRoster).mockResolvedValue({ agents: [row(3)], ancestors: [{ agent_id: 2, spawner: "user", fork_source_agent_id: null }] });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => expect(result.current.agents).toHaveLength(1));
    expect(result.current.ancestors).toHaveLength(1);
    expect(api.listAgents).not.toHaveBeenCalled();
  });
  it("reads a deep-linked terminated agent by ID without changing selection", async () => {
    setUrlSearch("?agent_id=999");
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => expect(result.current.activeAgent?.agent_id).toBe(999));
    expect(api.getAgent).toHaveBeenCalledWith(999, expect.any(AbortSignal));
    expect(api.listAgents).not.toHaveBeenCalled();
    expect(result.current.agents).toEqual(MOCK_AGENTS);
    expect(result.current.activeId).toBe(999);
  });
  it("retains a selected agent as it leaves the live roster", async () => {
    useStore.setState({ activeId: 2 });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => expect(result.current.activeAgent?.agent_id).toBe(2));
    act(() => { _qc.setQueryData(AGENTS_QUERY_KEY, { agents: [MOCK_AGENTS[0]], ancestors: [] }); });
    await waitFor(() => expect(result.current.activeAgent?.status).toBe("terminated"));
    expect(result.current.activeId).toBe(2);
  });
  it("URL selection overrides remembered selection and persists later changes", async () => {
    localStorage.setItem(ACTIVE_ID_KEY, "1"); setUrlSearch("?agent_id=2");
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => expect(result.current.activeId).toBe(2));
    act(() => result.current.setActiveId(1));
    await waitFor(() => expect(localStorage.getItem(ACTIVE_ID_KEY)).toBe("1"));
    expect(new URL(window.location.href).searchParams.get("agent_id")).toBe("1");
  });
  it("cancels obsolete historical detail and retains only the current detail cache", async () => {
    let oldSignal: AbortSignal | undefined;
    vi.mocked(api.getAgent).mockImplementation((id, signal) => {
      if (id === 99) { oldSignal = signal; return new Promise(() => undefined); }
      return Promise.resolve(row(id, "terminated"));
    });
    useStore.setState({ activeId: 99 });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => expect(oldSignal).toBeDefined());
    act(() => result.current.setActiveId(100));
    await waitFor(() => expect(result.current.activeAgent?.agent_id).toBe(100));
    expect(oldSignal?.aborted).toBe(true);
    await waitFor(() => expect(_qc.getQueryData([...AGENT_DETAIL_QUERY_KEY, 99])).toBeUndefined());
  });
});

describe("useAgents.spawn", () => {
  it("success → calls api.spawnAgent() + setActiveId(new id) + returns id", async () => {
    vi.mocked(api.spawnAgent).mockResolvedValue({ id: 42 });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    let returned: number | null = null;
    await act(async () => {
      returned = await result.current.spawn();
    });

    expect(api.spawnAgent).toHaveBeenCalledWith({});
    expect(returned).toBe(42);
    expect(useStore.getState().activeId).toBe(42);
  });

  it("preset rides inside config (task #2694), not as a sibling field", async () => {
    vi.mocked(api.spawnAgent).mockResolvedValue({ id: 60 });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    await act(async () => {
      await result.current.spawn(undefined, undefined, "coder");
    });

    expect(api.spawnAgent).toHaveBeenCalledWith({ config: { preset: "coder" } });
  });

  it("preset + model → both inside config, explicit fields next to the preset", async () => {
    vi.mocked(api.spawnAgent).mockResolvedValue({ id: 61 });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    await act(async () => {
      await result.current.spawn(undefined, "claude-sonnet-5", "coder", "high");
    });

    expect(api.spawnAgent).toHaveBeenCalledWith({
      config: { llm_model: "claude-sonnet-5", reasoning_effort: "high", preset: "coder" },
    });
  });

  it("with machine arg → forwarded to api.spawnAgent({machine})", async () => {
    vi.mocked(api.spawnAgent).mockResolvedValue({ id: 50 });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    await act(async () => {
      await result.current.spawn("wsl");
    });

    expect(api.spawnAgent).toHaveBeenCalledWith({ machine: "wsl" });
  });

  it("failure → calls showError + returns null + does not change activeId", async () => {
    const showError = vi.fn();
    vi.mocked(api.spawnAgent).mockRejectedValue(new Error("boom"));
    const { result } = renderHook(() => useAgents(showError), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    let returned: number | null | undefined;
    await act(async () => {
      returned = await result.current.spawn();
    });

    expect(returned).toBeNull();
    expect(showError).toHaveBeenCalledWith(expect.stringContaining("Spawn failed"));
    expect(showError).toHaveBeenCalledWith(expect.stringContaining("boom"));
    // activeId stays 1, did not switch to null
    expect(useStore.getState().activeId).toBe(1);
  });

  it("releases a spawn placeholder after authoritative repair even if the agent already terminated", async () => {
    let resolveSpawn!: (value: { id: number }) => void;
    let resolveRoster!: (value: AgentRoster) => void;
    vi.mocked(api.spawnAgent).mockImplementation(() => new Promise((resolve) => { resolveSpawn = resolve; }));
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => expect(result.current.activeId).toBe(1));
    vi.mocked(api.getAgentRoster).mockImplementation(() => new Promise((resolve) => { resolveRoster = resolve; }));
    // Selection may move before the new agent's ID read settles.
    vi.mocked(api.getAgent).mockImplementation(() => new Promise(() => { /* ID detail remains in flight. */ }));
    let pending!: Promise<number | null>;
    act(() => { pending = result.current.spawn(); });
    await waitFor(() => expect(result.current.pendingSpawnCount).toBe(1));
    act(() => { resolveSpawn({ id: 7 }); });
    await waitFor(() => expect(result.current.activeId).toBe(7));
    act(() => { result.current.setActiveId(1); });
    expect(result.current.pendingSpawnCount).toBe(1);
    await act(async () => {
      resolveRoster({ agents: MOCK_AGENTS, ancestors: [] });
      await pending;
    });
    expect(result.current.pendingSpawnCount).toBe(0);
    expect(result.current.agents.some((agent) => agent.agent_id === 7)).toBe(false);
  });
});

describe("useAgents.fork", () => {
  it("defaults to source agent's machine — fork lands on the same node", async () => {
    vi.mocked(api.spawnAgent).mockResolvedValue({ id: 99 });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    let returned: number | null = null;
    await act(async () => {
      returned = await result.current.fork(2);
    });

    // source id=2 in MOCK_AGENTS has machine='wsl' → fork passes it through by default
    expect(api.spawnAgent).toHaveBeenCalledWith({ fork_from: 2, machine: "wsl" });
    expect(returned).toBe(99);
    expect(useStore.getState().activeId).toBe(99);
  });

  it("forks a selected terminated conversation from the history cache", async () => {
    const terminated = {
      ...MOCK_AGENTS[0],
      agent_id: 9,
      machine: "archive-host",
      status: "terminated" as const,
    };
    _qc.setQueryData([...AGENT_DETAIL_QUERY_KEY, terminated.agent_id], terminated);
    setUrlSearch("?agent_id=9");
    vi.mocked(api.spawnAgent).mockResolvedValue({ id: 43 });

    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => expect(result.current.activeId).toBe(9));
    await act(async () => {
      await result.current.fork(9);
    });

    expect(api.spawnAgent).toHaveBeenCalledWith({
      fork_from: 9,
      machine: "archive-host",
    });
  });

  it("fork with prompt → passes prompt + prompt_source 'user'", async () => {
    vi.mocked(api.spawnAgent).mockResolvedValue({ id: 77 });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    await act(async () => {
      await result.current.fork(2, "explore the auth flow");
    });

    expect(api.spawnAgent).toHaveBeenCalledWith({
      fork_from: 2,
      machine: "wsl",
      prompt: "explore the auth flow",
      prompt_source: "user",
    });
  });

  it("missing source is confirmed by its detail read before refusing a fork", async () => {
    vi.mocked(api.getAgent).mockRejectedValue(new Error("Agent not found"));
    // fail-fast: the button should never appear for a non-existent
    // agent; receiving a stale id is a UI bug. Silent fallback would
    // mask it (the CLAUDE.md "missing-required-field → empty fallback" anti-pattern).
    const showError = vi.fn();
    vi.mocked(api.spawnAgent).mockResolvedValue({ id: 100 });
    const { result } = renderHook(() => useAgents(showError), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    let returned: number | null | undefined;
    await act(async () => {
      returned = await result.current.fork(9999);  // non-existent source id
    });

    expect(api.spawnAgent).not.toHaveBeenCalled();
    expect(returned).toBeNull();
    expect(showError).toHaveBeenCalledWith(expect.stringContaining("Agent not found"));
  });

  it("failure → showError + returns null", async () => {
    const showError = vi.fn();
    vi.mocked(api.spawnAgent).mockRejectedValue(new Error("fork-boom"));
    const { result } = renderHook(() => useAgents(showError), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    let returned: number | null | undefined;
    await act(async () => {
      returned = await result.current.fork(2);
    });

    expect(returned).toBeNull();
    expect(showError).toHaveBeenCalledWith(expect.stringContaining("Fork failed"));
    expect(showError).toHaveBeenCalledWith(expect.stringContaining("fork-boom"));
  });

  it("forkPending=true while mutation in-flight; resets after resolve", async () => {
    let resolve: ((v: { id: number }) => void) | undefined;
    vi.mocked(api.spawnAgent).mockImplementation(
      () => new Promise((res) => { resolve = res; }),
    );
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    let forkPromise: Promise<number | null>;
    act(() => {
      forkPromise = result.current.fork(2);
    });

    await waitFor(() => {
      expect(result.current.forkPending).toBe(true);
    });

    await act(async () => {
      resolve!({ id: 8 });
      await forkPromise!;
    });

    await waitFor(() => {
      expect(result.current.forkPending).toBe(false);
    });
  });
});

describe("useAgents.terminate", () => {
  it("success → calls api.terminateAgent(id, false) (graceful default)", async () => {
    vi.mocked(api.terminateAgent).mockResolvedValue({ status: "enqueued" });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    await act(async () => {
      await result.current.terminate(1);
    });

    expect(api.terminateAgent).toHaveBeenCalledWith(1, false);
    // Cache is updated by the AgentUpdated SSE event, not by an
    // invalidateQueries call — proving "no polling, no optimistic writes".
  });

  it("force → calls api.terminateAgent(id, true) and reports acceptance", async () => {
    vi.mocked(api.terminateAgent).mockResolvedValue({ status: "enqueued" });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    await act(async () => {
      await result.current.terminate(1, true);
    });

    expect(api.terminateAgent).toHaveBeenCalledWith(1, true);
    expect(useStore.getState().toast).toBe("Termination requested");
  });

  it("accepted termination reports request, not completed exit", async () => {
    // Placed in onSuccess rather than onMutate to avoid contradictory
    // "Termination requested" + "Terminate failed" coexistence when the backend
    // rejects. The toast auto-clears after 3s (built into store.showToast).
    vi.mocked(api.terminateAgent).mockResolvedValue({ status: "enqueued" });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.agents).toEqual(MOCK_AGENTS);
    });

    expect(useStore.getState().toast).toBeNull();
    await act(async () => {
      await result.current.terminate(1);
    });
    expect(useStore.getState().toast).toBe("Termination requested");
  });

  it("hosted force accepted is not reported killed and does not rewrite lifecycle cache", async () => {
    vi.mocked(api.terminateAgent).mockResolvedValue({ status: "enqueued" });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.agents).toEqual(MOCK_AGENTS);
    });
    await act(async () => {
      await result.current.terminate(1, true);
    });
    expect(api.terminateAgent).toHaveBeenCalledWith(1, true);
    expect(useStore.getState().toast).toBe("Termination requested");
    expect(result.current.agents).toEqual(MOCK_AGENTS);
  });

  it("onError does not show 'Terminated' toast — only showError on backend reject", async () => {
    // Regression guard: the toast must wait for onSuccess; it can't fire in onMutate.
    const showError = vi.fn();
    vi.mocked(api.terminateAgent).mockRejectedValue(new Error("backend boom"));
    const { result } = renderHook(() => useAgents(showError), { wrapper });
    await waitFor(() => {
      expect(result.current.agents).toEqual(MOCK_AGENTS);
    });

    await act(async () => {
      await result.current.terminate(1);
    });
    expect(useStore.getState().toast).toBeNull();
    expect(showError).toHaveBeenCalledWith(expect.stringContaining("Terminate failed"));
  });

  it("open-tasks hint opens the notice state (response field #2488)", async () => {
    vi.mocked(api.terminateAgent).mockResolvedValue({
      status: "enqueued",
      open_tasks: {
        count: 2,
        more: 1,
        tasks: [
          {
            id: 12,
            title: "Ship the hint",
            status: "in_progress",
            updated_at: "2026-09-14T05:00:00+00:00",
          },
        ],
      },
    });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    await act(async () => {
      await result.current.terminate(1);
    });

    expect(useStore.getState().openTasksNotice).toMatchObject({ count: 2, more: 1 });
  });

  it("count 0 keeps the notice closed", async () => {
    vi.mocked(api.terminateAgent).mockResolvedValue({
      status: "enqueued",
      open_tasks: { count: 0, more: 0, tasks: [] },
    });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    await act(async () => {
      await result.current.terminate(1);
    });

    expect(useStore.getState().openTasksNotice).toBeNull();
  });

  it("null hint and an old gateway without the field stay silent", async () => {
    vi.mocked(api.terminateAgent).mockResolvedValueOnce({
      status: "enqueued",
      open_tasks: null,
    });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });
    await act(async () => {
      await result.current.terminate(1);
    });
    expect(useStore.getState().openTasksNotice).toBeNull();

    vi.mocked(api.terminateAgent).mockResolvedValueOnce({ status: "enqueued" });
    await act(async () => {
      await result.current.terminate(1);
    });
    expect(useStore.getState().openTasksNotice).toBeNull();
  });

  it("already_terminated with open tasks still opens the notice", async () => {
    vi.mocked(api.terminateAgent).mockResolvedValue({
      status: "already_terminated",
      open_tasks: {
        count: 1,
        more: 0,
        tasks: [
          {
            id: 3,
            title: "Winding down",
            status: "in_progress",
            updated_at: "2026-09-14T05:00:00+00:00",
          },
        ],
      },
    });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    await act(async () => {
      await result.current.terminate(1);
    });

    expect(useStore.getState().openTasksNotice).toMatchObject({ count: 1 });
    expect(useStore.getState().toast).toBe("Already terminated");
  });

  it("cache untouched while terminate in flight (no optimistic write)", async () => {
    // Hold the mutation pending and verify the agents cache is exactly
    // unchanged during the in-flight window — the row only flips after
    // the AgentUpdated SSE event lands, which is out of scope for the
    // mutation handler. Pins the "no optimistic" contract.
    let resolve: ((v: { status: "enqueued" }) => void) | undefined;
    vi.mocked(api.terminateAgent).mockImplementation(
      () => new Promise((res) => { resolve = res; }),
    );
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.agents).toEqual(MOCK_AGENTS);
    });

    const before = _qc.getQueryData<AgentRoster>(AGENTS_QUERY_KEY)?.agents;
    expect(before?.find((a) => a.agent_id === 1)?.status).toBe("running");

    let termPromise: Promise<void>;
    act(() => {
      termPromise = result.current.terminate(1);
    });

    // Mid-flight: row 1 still 'running'; mutation flag is true.
    await waitFor(() => {
      expect(result.current.pendingActions[1]).toBe("terminating");
    });
    const mid = _qc.getQueryData<AgentRoster>(AGENTS_QUERY_KEY)?.agents;
    expect(mid?.find((a) => a.agent_id === 1)?.status).toBe("running");
    expect(mid?.find((a) => a.agent_id === 2)?.status).toBe("idling");

    await act(async () => {
      resolve!({ status: "enqueued" });
      await termPromise!;
    });

    // After resolve: still no cache rewrite (no onSettled invalidate),
    // pending flag cleared.
    const after = _qc.getQueryData<AgentRoster>(AGENTS_QUERY_KEY)?.agents;
    expect(after?.find((a) => a.agent_id === 1)?.status).toBe("running");
    await waitFor(() => {
      expect(result.current.pendingActions[1]).toBeUndefined();
    });
  });

  it("failure → cache untouched + showError", async () => {
    const showError = vi.fn();
    vi.mocked(api.terminateAgent).mockRejectedValue(new Error("term-boom"));
    const { result } = renderHook(() => useAgents(showError), { wrapper });
    await waitFor(() => {
      expect(result.current.agents).toEqual(MOCK_AGENTS);
    });

    await act(async () => {
      await result.current.terminate(1);
    });

    // No optimistic write means nothing to roll back; the cache is still
    // exactly what listAgents returned.
    const after = _qc.getQueryData<AgentRoster>(AGENTS_QUERY_KEY)?.agents;
    expect(after?.find((a) => a.agent_id === 1)?.status).toBe("running");
    expect(showError).toHaveBeenCalledWith(expect.stringContaining("Terminate failed"));
    expect(showError).toHaveBeenCalledWith(expect.stringContaining("term-boom"));
  });

  it("isPending → pendingActions[id]='terminating'", async () => {
    let resolve: ((v: { status: "enqueued" }) => void) | undefined;
    vi.mocked(api.terminateAgent).mockImplementation(
      () => new Promise((res) => { resolve = res; }),
    );
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    let termPromise: Promise<void>;
    act(() => {
      termPromise = result.current.terminate(1);
    });

    await waitFor(() => {
      expect(result.current.pendingActions[1]).toBe("terminating");
    });

    await act(async () => {
      resolve!({ status: "enqueued" });
      await termPromise!;
    });

    await waitFor(() => {
      expect(result.current.pendingActions[1]).toBeUndefined();
    });
  });

  // Task #837: terminating the SELECTED agent auto-switches to a neighbor.
  // MOCK_AGENTS = [1, 2] — the only neighbor of 2 is 1 (the wrap case).
  it("terminating the selected agent switches to the adjacent agent", async () => {
    vi.mocked(api.terminateAgent).mockResolvedValue({ status: "enqueued" });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    // Select agent 2, then terminate it — the selection must move to its
    // neighbor agent 1 (wrapping around the list end).
    act(() => result.current.setActiveId(2));
    await act(async () => {
      await result.current.terminate(2);
    });
    expect(useStore.getState().activeId).toBe(1);
  });

  it("terminating a NON-selected agent leaves the selection alone", async () => {
    vi.mocked(api.terminateAgent).mockResolvedValue({ status: "enqueued" });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    // Terminate agent 2 while 1 is selected — selection stays on 1.
    await act(async () => {
      await result.current.terminate(2);
    });
    expect(useStore.getState().activeId).toBe(1);
  });
});

describe("useAgents.restart", () => {
  it("success → calls api.restartAgent(id)", async () => {
    vi.mocked(api.restartAgent).mockResolvedValue({ status: "enqueued" });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    await act(async () => {
      await result.current.restart(1);
    });

    expect(api.restartAgent).toHaveBeenCalledWith(1);
  });

  it("failure → showError + does not throw", async () => {
    const showError = vi.fn();
    vi.mocked(api.restartAgent).mockRejectedValue(new Error("restart-boom"));
    const { result } = renderHook(() => useAgents(showError), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    await act(async () => {
      await result.current.restart(1);
    });

    expect(showError).toHaveBeenCalledWith(expect.stringContaining("Restart failed"));
    expect(showError).toHaveBeenCalledWith(expect.stringContaining("restart-boom"));
  });

  it("isPending → pendingActions[id]='restarting'", async () => {
    let resolve: ((v: { status: "enqueued" }) => void) | undefined;
    vi.mocked(api.restartAgent).mockImplementation(
      () => new Promise((res) => { resolve = res; }),
    );
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    let rp: Promise<void>;
    act(() => {
      rp = result.current.restart(2);
    });

    await waitFor(() => {
      expect(result.current.pendingActions[2]).toBe("restarting");
    });

    await act(async () => {
      resolve!({ status: "enqueued" });
      await rp!;
    });
  });
});

describe("useAgents.resurrect", () => {
  it("success → calls api.resurrectAgent(id, prompt)", async () => {
    vi.mocked(api.resurrectAgent).mockResolvedValue({ status: "spawned" });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    await act(async () => {
      await result.current.resurrect(1, "resume work");
    });

    expect(api.resurrectAgent).toHaveBeenCalledWith(1, "resume work");
  });

  it("resurrect without a prompt → calls api.resurrectAgent(id) with no prompt", async () => {
    vi.mocked(api.resurrectAgent).mockResolvedValue({ status: "spawned" });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    await act(async () => {
      await result.current.resurrect(1);
    });

    expect(api.resurrectAgent).toHaveBeenCalledWith(1, undefined);
  });

  it("failure → showError", async () => {
    const showError = vi.fn();
    vi.mocked(api.resurrectAgent).mockRejectedValue(new Error("resurrect-boom"));
    const { result } = renderHook(() => useAgents(showError), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    await act(async () => {
      await result.current.resurrect(1, "resume");
    });

    expect(showError).toHaveBeenCalledWith(expect.stringContaining("Resurrect failed"));
    expect(showError).toHaveBeenCalledWith(expect.stringContaining("resurrect-boom"));
  });

  it("isPending → pendingActions[id]='resurrecting'", async () => {
    let resolve: ((v: { status: "spawned" }) => void) | undefined;
    vi.mocked(api.resurrectAgent).mockImplementation(
      () => new Promise((res) => { resolve = res; }),
    );
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    let rp: Promise<void>;
    act(() => {
      rp = result.current.resurrect(2, "resume");
    });

    await waitFor(() => {
      expect(result.current.pendingActions[2]).toBe("resurrecting");
    });

    await act(async () => {
      resolve!({ status: "spawned" });
      await rp!;
    });
  });
});

// ─────────────────────────────────────────────────────────────
// agentsError → showError
// ─────────────────────────────────────────────────────────────
