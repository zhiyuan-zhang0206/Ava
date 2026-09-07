// useAgents regression + mutation-kill tests.
//
// Original PR #180 regression: activeId stays in sync between the
// store and the hook return value.
// 2026-05-16 follow-up: added lifecycle mutation (spawn / fork /
// terminate / restart / resurrect) + localStorage / URL hydration +
// auto-select effect coverage to lift Stryker mutation score above 9.83%.
//
// Pins down:
// - The activeId returned by useAgents is the store's activeId
//   (single source).
// - After store.setActiveId, useAgents re-renders and returns the new value.
// - agents list syncs into the store.
// - All five lifecycle mutation paths: onMutate / onError / onSettled / success.
// - URL ?agent_id=N + localStorage hydration persistence.
// - Auto-select activeId boundaries (prev still alive / all terminated / agents empty).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";
import { AuthProvider } from "./auth-context";
import { useStore } from "./store";
import type { AgentRow, SystemEvent, WireAgentRow } from "./types";
import { projectAgentStatus } from "./types";
import { AGENTS_QUERY_KEY, upsertAgent, useAgents } from "./use-agents";
import { foldAgents, TERMINATED_AGENTS_QUERY_KEY } from "./fold/agents";
import { RECONNECT_QUERY_KEYS } from "./fold";
import { SETTINGS_QUERY_KEY } from "./use-user-settings";
import { EventStreamProvider } from "./useEventStream";

vi.mock("./api", () => ({
  // API_BASE is also re-exported from this module; useEventStream
  // reads it for `new EventSource(`${API_BASE}/api/system`)`.
  API_BASE: "",
  api: {
    listAgents: vi.fn(),
    spawnAgent: vi.fn(),
    terminateAgent: vi.fn(),
    restartAgent: vi.fn(),
    resurrectAgent: vi.fn(),
    checkAuth: vi.fn(),
    getSettings: vi.fn(),
    putSetting: vi.fn(),
  },
}));

// EventSource is unavailable in happy-dom; stub a minimal one so
// <EventStreamProvider> can construct without throwing. The tests below
// don't drive SSE behavior — that's covered separately in
// useEventStream.test.ts.
let lastEventSource: StubEventSource | null = null;
class StubEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  readyState = StubEventSource.CONNECTING;
  onopen: ((this: EventSource, ev: Event) => unknown) | null = null;
  onmessage: ((this: EventSource, ev: MessageEvent) => unknown) | null = null;
  onerror: ((this: EventSource, ev: Event) => unknown) | null = null;
  constructor(public url: string, _init?: EventSourceInit) {
    // eslint-disable-next-line @typescript-eslint/no-this-alias -- record latest instance for deliverSseMessage
    lastEventSource = this;
  }
  close(): void {
    this.readyState = StubEventSource.CLOSED;
  }
  /* eslint-disable @typescript-eslint/no-empty-function -- EventSource interface conformance; no behavior needed in tests */
  addEventListener(): void {}
  removeEventListener(): void {}
  /* eslint-enable @typescript-eslint/no-empty-function */
  dispatchEvent(): boolean {
    return true;
  }
}

/** Drive the most recently constructed StubEventSource as if the server
 * pushed a frame. Used by the SSE → cache regression tests below. */
function deliverSseMessage(payload: unknown): void {
  if (!lastEventSource) throw new Error("no EventSource constructed yet");
  const handler = lastEventSource.onmessage;
  if (!handler) throw new Error("EventSource.onmessage not yet wired");
  handler.call(
    lastEventSource as unknown as EventSource,
    { data: JSON.stringify(payload) } as MessageEvent,
  );
}

async function waitForEventSource(): Promise<void> {
  await waitFor(() => expect(lastEventSource).not.toBeNull());
}
beforeAll(() => {
  // happy-dom ships a real EventSource that opens a TCP socket; stub
  // both globalThis and window so neither path attempts a connection.
  vi.stubGlobal("EventSource", StubEventSource);
  if (typeof window !== "undefined") {
    (window as unknown as { EventSource: typeof StubEventSource }).EventSource = StubEventSource;
  }
});

const MOCK_AGENTS: AgentRow[] = [
  { agent_id: 1, label: "Test #1", status: "running", last_active_at: "2026-05-10T00:00:00Z", last_inbound_at: "2026-05-10T00:00:00Z", spawner: "user", fork_source_agent_id: null, pid: 100, spawned_at: "2026-05-10T00:00:00Z", started_at: "2026-05-10T00:00:01Z", machine: "test-host", supports_vision: true, notices_awaiting_response: [], unread_notice_count: 0, heartbeat_paused_until: null, liveness_state: "online" },
  { agent_id: 2, label: "Test #2", status: "idling", last_active_at: "2026-05-10T01:00:00Z", last_inbound_at: "2026-05-10T01:00:00Z", spawner: "user", fork_source_agent_id: null, pid: 101, spawned_at: "2026-05-10T01:00:00Z", started_at: "2026-05-10T01:00:01Z", machine: "wsl", supports_vision: true, notices_awaiting_response: [], unread_notice_count: 0, heartbeat_paused_until: null, liveness_state: "online" },
];

let _qc: QueryClient;
// The R4 fold (layer 1) lives INSIDE <EventStreamProvider> — the Provider's
// fold owner subscribes to the global broadcast, applies applyEvent into the
// query cache, and runs the central reconnect reconcile. The wrapper mirrors
// the app root (components/providers.tsx), so SSE → cache merge (exercised by
// the regression block) happens through the real fold, not through useAgents
// (which is now a pure reader). Renders nothing; inert unless an SSE agent
// event is delivered.
function wrapper({ children }: { children: React.ReactNode }) {
  // Both the fold owner and any useAgents reader live inside
  // <AuthProvider> ⊃ <QueryClientProvider> ⊃ <EventStreamProvider>.
  return React.createElement(
    AuthProvider,
    null,
    React.createElement(
      QueryClientProvider,
      { client: _qc },
      React.createElement(EventStreamProvider, null, children),
    ),
  );
}

// vitest has no localStorage by default — mock a simple implementation
const localStorageMock = (() => {
  const store = new Map<string, string>();
  return {
    getItem: vi.fn((key: string) => store.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => { store.set(key, value); }),
    removeItem: vi.fn((key: string) => { store.delete(key); }),
    clear: vi.fn(() => { store.clear(); }),
  };
})();
Object.defineProperty(globalThis, "localStorage", { value: localStorageMock, writable: true });

const ACTIVE_ID_KEY = "ava.active.agent_id";

// eslint-disable-next-line @typescript-eslint/no-empty-function
function noop(): void {}

// Use happy-dom's window.history.replaceState to change URL — the
// first useAgents effect reads ?agent_id=N from window.location.href
function setUrlSearch(search: string) {
  const url = new URL(window.location.href);
  url.search = search;
  window.history.replaceState(null, "", url.toString());
}

beforeEach(() => {
  vi.clearAllMocks();
  lastEventSource = null;
  localStorageMock.clear();
  // Clear URL search
  setUrlSearch("");
  // Reset store (only UI slice remains here — agents / lifecycle pending
  // moved to TanStack Query + useAgents-derived state).
  useStore.setState({
    activeId: null,
    toast: null,
  });
  _qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  vi.mocked(api.listAgents).mockImplementation((scope = "live") =>
    Promise.resolve(scope === "terminated" ? [] : MOCK_AGENTS),
  );
  vi.mocked(api.checkAuth).mockResolvedValue({ authenticated: true });
  vi.mocked(api.getSettings).mockResolvedValue({ settings: [] });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("useAgents activeId / store sync", () => {
  it("returned activeId comes from the Zustand store", async () => {
    expect(useStore.getState().activeId).toBeNull();

    const { result } = renderHook(() => useAgents(
    /* eslint-disable-next-line @typescript-eslint/no-empty-function -- showError noop in tests */
    () => {}), { wrapper });

    expect(result.current.activeId).toBeNull();

    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });
    expect(useStore.getState().activeId).toBe(1);
  });

  it("after store.setActiveId, useAgents returns the new value (simulates sidebar switch)", async () => {
    const { result } = renderHook(() => useAgents(noop), { wrapper });

    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    act(() => {
      useStore.getState().setActiveId(2);
    });

    await waitFor(() => {
      expect(result.current.activeId).toBe(2);
    });
    expect(useStore.getState().activeId).toBe(2);
  });

  it("always fetches and merges the terminated roster, no show-terminated setting required", async () => {
    // The terminated scope is part of the roster unconditionally: its rows
    // are the lineage joints the spawn tree walks (#312 regression — an
    // alive child of a terminated parent needs that parent row to re-parent
    // under the nearest visible ancestor).
    const terminated = { ...MOCK_AGENTS[0], agent_id: 9, status: "terminated" as const };
    vi.mocked(api.listAgents).mockImplementation((scope = "live") =>
      Promise.resolve(scope === "terminated" ? [terminated] : MOCK_AGENTS),
    );

    const { result } = renderHook(() => useAgents(noop), { wrapper });

    await waitFor(() => {
      expect(result.current.agents.map((agent) => agent.agent_id)).toEqual([1, 2, 9]);
    });
    expect(vi.mocked(api.listAgents)).toHaveBeenCalledWith("live");
    expect(vi.mocked(api.listAgents)).toHaveBeenCalledWith("terminated");
    expect(_qc.getQueryData(TERMINATED_AGENTS_QUERY_KEY)).toEqual([terminated]);
  });

  it("keeps the terminated parent row in the merged roster while the setting is off (#312 shape)", async () => {
    // 228 (alive) -> 240 (terminated) -> 312 (alive, raw spawner agent:240).
    // With the show-terminated setting OFF, the combined roster must still
    // carry 240: only then can the tree builder mount 312 under 228 instead
    // of surfacing 312 as an orphan root.
    const a228 = { ...MOCK_AGENTS[0], agent_id: 228, label: "root", status: "idling" as const };
    const a312 = { ...MOCK_AGENTS[0], agent_id: 312, label: "monitor", status: "idling" as const, spawner: "agent:240" };
    const a240 = { ...MOCK_AGENTS[0], agent_id: 240, label: "parent", status: "terminated" as const, spawner: "agent:228" };
    vi.mocked(api.listAgents).mockImplementation((scope = "live") =>
      Promise.resolve(scope === "terminated" ? [a240] : [a228, a312]),
    );

    const { result } = renderHook(() => useAgents(noop), { wrapper });

    await waitFor(() => {
      const ids = result.current.agents.map((agent) => agent.agent_id);
      expect(ids).toEqual([228, 240, 312]);
    });
    const merged240 = result.current.agents.find((a) => a.agent_id === 240);
    expect(merged240?.status).toBe("terminated");
    expect(merged240?.spawner).toBe("agent:228");
    // The live child keeps its RAW spawner — lineage truth, not a payload rewrite.
    expect(result.current.agents.find((a) => a.agent_id === 312)?.spawner).toBe("agent:240");
  });

  it("replays a termination SSE over an older first history snapshot", async () => {
    const terminated = { ...MOCK_AGENTS[0], status: "terminated" as const };
    _qc.setQueryData(SETTINGS_QUERY_KEY, { "display.show_terminated": true });
    let resolveHistory!: (rows: AgentRow[]) => void;
    const history = new Promise<AgentRow[]>((resolve) => {
      resolveHistory = resolve;
    });
    vi.mocked(api.listAgents).mockImplementation((scope = "live") =>
      scope === "terminated" ? history : Promise.resolve(MOCK_AGENTS),
    );

    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => expect(result.current.agents).toEqual(MOCK_AGENTS));
    await waitForEventSource();
    act(() => {
      deliverSseMessage({
        role: "agent_updated",
        agent_id: terminated.agent_id,
        snapshot: terminated,
      });
    });

    await act(async () => {
      resolveHistory([]); // stale DB snapshot taken before the termination
      await history;
    });

    await waitFor(() => {
      expect(_qc.getQueryData(TERMINATED_AGENTS_QUERY_KEY)).toEqual([terminated]);
      expect(result.current.agents.find((agent) => agent.agent_id === 1)?.status).toBe(
        "terminated",
      );
    });
  });

  it("replays a resurrection SSE over an older terminated snapshot", async () => {
    const oldTerminated = {
      ...MOCK_AGENTS[0],
      agent_id: 9,
      status: "terminated" as const,
    };
    const resurrected = { ...oldTerminated, status: "idling" as const };
    _qc.setQueryData(SETTINGS_QUERY_KEY, { "display.show_terminated": true });
    let resolveHistory!: (rows: AgentRow[]) => void;
    const history = new Promise<AgentRow[]>((resolve) => {
      resolveHistory = resolve;
    });
    vi.mocked(api.listAgents).mockImplementation((scope = "live") =>
      scope === "terminated" ? history : Promise.resolve(MOCK_AGENTS),
    );

    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => expect(result.current.agents).toEqual(MOCK_AGENTS));
    await waitForEventSource();
    act(() => {
      deliverSseMessage({
        role: "agent_updated",
        agent_id: resurrected.agent_id,
        snapshot: resurrected,
      });
    });

    await act(async () => {
      resolveHistory([oldTerminated]); // stale snapshot taken before resurrection
      await history;
    });

    await waitFor(() => {
      expect(_qc.getQueryData(TERMINATED_AGENTS_QUERY_KEY)).toEqual([]);
      const rows = result.current.agents.filter((agent) => agent.agent_id === 9);
      expect(rows).toHaveLength(1);
      expect(rows[0]?.status).toBe("idling");
    });
  });

  it("preserves a deep-linked terminated selection until settings and history settle", async () => {
    const terminated = {
      ...MOCK_AGENTS[0],
      agent_id: 9,
      status: "terminated" as const,
    };
    let resolveSettings!: (value: {
      settings: { key: string; value: boolean; updated_at: string }[];
    }) => void;
    const settings = new Promise<{
      settings: { key: string; value: boolean; updated_at: string }[];
    }>((resolve) => {
      resolveSettings = resolve;
    });
    let resolveHistory!: (rows: AgentRow[]) => void;
    const history = new Promise<AgentRow[]>((resolve) => {
      resolveHistory = resolve;
    });
    vi.mocked(api.getSettings).mockReturnValue(settings);
    vi.mocked(api.listAgents).mockImplementation((scope = "live") =>
      scope === "terminated" ? history : Promise.resolve(MOCK_AGENTS),
    );
    setUrlSearch("?agent_id=9");

    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => expect(_qc.getQueryData(AGENTS_QUERY_KEY)).toEqual(MOCK_AGENTS));
    expect(result.current.activeId).toBe(9);
    expect(result.current.isLoading).toBe(true);

    await act(async () => {
      resolveSettings({
        settings: [
          {
            key: "display.show_terminated",
            value: true,
            updated_at: "2026-08-27T00:00:00Z",
          },
        ],
      });
      await settings;
    });
    await waitFor(() =>
      expect(vi.mocked(api.listAgents)).toHaveBeenCalledWith("terminated"),
    );
    expect(result.current.activeId).toBe(9);
    expect(result.current.isLoading).toBe(true);

    await act(async () => {
      resolveHistory([terminated]);
      await history;
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.activeId).toBe(9);
    expect(result.current.agents.some((agent) => agent.agent_id === 9)).toBe(true);
  });

  it("returned agents reflect the listAgents result (no store mirror)", async () => {
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    expect(result.current.agents).toEqual([]);
    await waitFor(() => {
      expect(result.current.agents).toEqual(MOCK_AGENTS);
    });
  });

  it("returned setActiveId is store.setActiveId", async () => {
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });
    act(() => {
      result.current.setActiveId(2);
    });
    expect(useStore.getState().activeId).toBe(2);
    expect(result.current.activeId).toBe(2);
  });
});

// ─────────────────────────────────────────────────────────────
// hydration: URL ?agent_id=N + localStorage persistence
// ─────────────────────────────────────────────────────────────

describe("useAgents hydration (URL / localStorage)", () => {
  // Prefetch so useQuery has data on first render, avoiding
  // auto-select clearing the hydration-set activeId when agents is empty
  function prefetchAgents() {
    _qc.setQueryData(AGENTS_QUERY_KEY, MOCK_AGENTS);
  }

  it("on mount, URL ?agent_id=2 wins (deep-link)", async () => {
    prefetchAgents();
    setUrlSearch("?agent_id=2");
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(2);
    });
    expect(useStore.getState().activeId).toBe(2);
  });

  it("URL agent_id wins over localStorage", async () => {
    prefetchAgents();
    setUrlSearch("?agent_id=2");
    localStorageMock.setItem(ACTIVE_ID_KEY, "1");
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(2);
    });
  });

  it("no URL agent_id → falls back to localStorage", async () => {
    prefetchAgents();
    localStorageMock.setItem(ACTIVE_ID_KEY, "2");
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(2);
    });
    expect(localStorageMock.getItem).toHaveBeenCalledWith(ACTIVE_ID_KEY);
  });

  it("neither URL nor localStorage → wait for agents and auto-select the first", async () => {
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });
  });

  it("URL agent_id non-numeric → falls back to localStorage", async () => {
    prefetchAgents();
    setUrlSearch("?agent_id=foo");
    localStorageMock.setItem(ACTIVE_ID_KEY, "2");
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(2);
    });
  });

  it("URL agent_id <= 0 → fallback localStorage", async () => {
    prefetchAgents();
    setUrlSearch("?agent_id=0");
    localStorageMock.setItem(ACTIVE_ID_KEY, "2");
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(2);
    });
  });

  it("localStorage non-numeric → does not set activeId (takes auto-select path)", async () => {
    localStorageMock.setItem(ACTIVE_ID_KEY, "not-a-number");
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    // Falls through to auto-select with the first agent
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });
  });

  it("after setActiveId(5), localStorage writes '5'", async () => {
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });
    act(() => {
      result.current.setActiveId(5);
    });
    await waitFor(() => {
      expect(localStorageMock.setItem).toHaveBeenCalledWith(ACTIVE_ID_KEY, "5");
    });
  });

  it("after setActiveId(5), URL syncs ?agent_id=5", async () => {
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });
    act(() => {
      result.current.setActiveId(5);
    });
    await waitFor(() => {
      const url = new URL(window.location.href);
      expect(url.searchParams.get("agent_id")).toBe("5");
    });
  });

  it("after setActiveId(null), localStorage entry removed + URL ?agent_id removed", async () => {
    prefetchAgents();
    setUrlSearch("?agent_id=2");
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(2);
    });
    // Explicitly clearing a selection is only meaningful once both roster
    // inputs have settled. Before that, the guarded initial auto-select still
    // owns the transition from the hydrated sentinel to a valid roster row.
    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });
    act(() => {
      result.current.setActiveId(null);
    });
    await waitFor(() => {
      expect(localStorageMock.removeItem).toHaveBeenCalledWith(ACTIVE_ID_KEY);
    });
    const url = new URL(window.location.href);
    expect(url.searchParams.has("agent_id")).toBe(false);
  });
});

// ─────────────────────────────────────────────────────────────
// auto-select effect boundaries
// ─────────────────────────────────────────────────────────────

describe("useAgents auto-select", () => {
  it("prev activeId still alive → kept, not switched (re-fetch same id does not switch back to first)", async () => {
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    // Initial auto-select picks 1
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });
    // User switches to 2
    act(() => {
      result.current.setActiveId(2);
    });
    expect(useStore.getState().activeId).toBe(2);
    // refetch (returns a new array reference with the same content) → triggers auto-select effect
    await act(async () => {
      await _qc.refetchQueries({ queryKey: AGENTS_QUERY_KEY });
    });
    // prev=2 still in agents → kept, not switched to 1
    expect(useStore.getState().activeId).toBe(2);
  });

  it("prev activeId not in agents → switches to first alive", async () => {
    useStore.setState({ activeId: 999 });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });
  });

  it("all terminated → falls back to agents[0]", async () => {
    const allDead: AgentRow[] = MOCK_AGENTS.map((a) => ({ ...a, status: "terminated" as const }));
    vi.mocked(api.listAgents).mockResolvedValue(allDead);
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });
  });

  it("agents empty → activeId stays null", async () => {
    vi.mocked(api.listAgents).mockResolvedValue([]);
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    // wait for query to settle
    await waitFor(() => {
      expect(result.current.agents).toEqual([]);
    });
    expect(result.current.activeId).toBeNull();
  });

  it("terminated entries before the first running → picks first alive", async () => {
    const mixed: AgentRow[] = [
      { ...MOCK_AGENTS[0], status: "terminated" as const },
      { ...MOCK_AGENTS[1], status: "running" as const },
    ];
    vi.mocked(api.listAgents).mockResolvedValue(mixed);
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(2);
    });
  });
});

// ─────────────────────────────────────────────────────────────
// lifecycle mutations: spawn / fork / terminate / restart / resurrect
// ─────────────────────────────────────────────────────────────

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

  it("pendingSpawnCount=1 while mutation in-flight, then stays =1 until snapshot lands", async () => {
    let resolve: ((v: { id: number }) => void) | undefined;
    vi.mocked(api.spawnAgent).mockImplementation(
      () => new Promise((res) => { resolve = res; }),
    );
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });

    let spawnPromise: Promise<number | null>;
    act(() => {
      spawnPromise = result.current.spawn();
    });

    // Mutation in-flight: pendingSpawnCount = 1 via spawnMutation.isPending.
    await waitFor(() => {
      expect(result.current.pendingSpawnCount).toBe(1);
    });

    await act(async () => {
      resolve!({ id: 7 });
      await spawnPromise!;
    });

    // Mutation resolved but AgentSpawned not yet pushed → still =1
    // (transferred from isPending to pendingSpawnIds for id=7).
    expect(result.current.pendingSpawnCount).toBe(1);

    // Simulate AgentSpawned arrival: the cache now contains id=7.
    act(() => {
      _qc.setQueryData<AgentRow[]>(AGENTS_QUERY_KEY, (prev) => [
        ...(prev ?? []),
        {
          agent_id: 7,
          spawner: "user",
          fork_source_agent_id: null,
          status: "idling" as const,
          pid: null,
          spawned_at: "2026-05-25T00:00:00Z",
          started_at: null,
          last_active_at: "2026-05-25T00:00:00Z", last_inbound_at: "2026-05-25T00:00:00Z",
          label: null,
          machine: "test-host",
          supports_vision: true,
          notices_awaiting_response: [], unread_notice_count: 0,
          heartbeat_paused_until: null,
          liveness_state: "online",
        },
      ]);
    });

    await waitFor(() => {
      expect(result.current.pendingSpawnCount).toBe(0);
    });
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
    _qc.setQueryData(SETTINGS_QUERY_KEY, { "display.show_terminated": true });
    _qc.setQueryData(TERMINATED_AGENTS_QUERY_KEY, [terminated]);
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

  it("source not found (stale UI id) → throws, showError + does not call spawnAgent", async () => {
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
    expect(showError).toHaveBeenCalledWith(expect.stringContaining("not in cache"));
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

    const before = _qc.getQueryData<AgentRow[]>(AGENTS_QUERY_KEY);
    expect(before?.find((a) => a.agent_id === 1)?.status).toBe("running");

    let termPromise: Promise<void>;
    act(() => {
      termPromise = result.current.terminate(1);
    });

    // Mid-flight: row 1 still 'running'; mutation flag is true.
    await waitFor(() => {
      expect(result.current.pendingActions[1]).toBe("terminating");
    });
    const mid = _qc.getQueryData<AgentRow[]>(AGENTS_QUERY_KEY);
    expect(mid?.find((a) => a.agent_id === 1)?.status).toBe("running");
    expect(mid?.find((a) => a.agent_id === 2)?.status).toBe("idling");

    await act(async () => {
      resolve!({ status: "enqueued" });
      await termPromise!;
    });

    // After resolve: still no cache rewrite (no onSettled invalidate),
    // pending flag cleared.
    const after = _qc.getQueryData<AgentRow[]>(AGENTS_QUERY_KEY);
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
    const after = _qc.getQueryData<AgentRow[]>(AGENTS_QUERY_KEY);
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

describe("useAgents listAgents error", () => {
  it("listAgents reject → showError called with 'Failed to list agents'", async () => {
    const showError = vi.fn();
    vi.mocked(api.listAgents).mockRejectedValue(new Error("net-down"));
    renderHook(() => useAgents(showError), { wrapper });
    await waitFor(() => {
      expect(showError).toHaveBeenCalledWith(expect.stringContaining("Failed to list agents"));
    });
    expect(showError).toHaveBeenCalledWith(expect.stringContaining("net-down"));
  });
});

// ─────────────────────────────────────────────────────────────
// AGENTS_QUERY_KEY export
// ─────────────────────────────────────────────────────────────

describe("AGENTS_QUERY_KEY", () => {
  it("keeps live and terminated snapshots on sibling, non-overlapping keys", () => {
    expect(AGENTS_QUERY_KEY).toEqual(["agents", "live"]);
    expect(TERMINATED_AGENTS_QUERY_KEY).toEqual(["agents", "terminated"]);
  });
});

// ─────────────────────────────────────────────────────────────
// SSE → cache regressions: covers the multi-source race that caused
// "two loading rows" / "sidebar flicker" before the rework. The earlier
// implementation mirrored agents into Zustand from useQuery + ran a 5s
// poll + wrote optimistic updates from onMutate — three writers landed
// in different orders depending on network timing. Unit tests at the
// time couldn't trip this because mock api.* resolved synchronously and
// the polling timer was usually mocked away. The new contract is "one
// fetch + SSE upserts"; the tests below pin that contract.
// ─────────────────────────────────────────────────────────────

describe("useAgents SSE merge (regression)", () => {
  it("AgentSpawned event upserts a new snapshot into the cache without a refetch", async () => {
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.agents).toEqual(MOCK_AGENTS);
    });
    await waitForEventSource();
    const fetchesBefore = vi.mocked(api.listAgents).mock.calls.length;

    const newSnapshot: WireAgentRow = {
      agent_id: 42,
      spawner: "user",
      fork_source_agent_id: null,
      fork_source_checkpoint_id: null,
      status: "idling",
      pid: null,
      spawned_at: "2026-05-25T12:00:00Z",
      started_at: null,
      last_active_at: "2026-05-25T12:00:00Z", last_inbound_at: "2026-05-25T12:00:00Z",
      label: null,
      machine: "test-host",
      supports_vision: true,
      notices_awaiting_response: [], unread_notice_count: 0,
      heartbeat_paused_until: null,
      liveness_state: "online",
      last_probe_at: null,
    };
    act(() => {
      deliverSseMessage({
        role: "agent_spawned",
        agent_id: 42,
        snapshot: newSnapshot,
      });
    });

    await waitFor(() => {
      const { fork_source_checkpoint_id: _, last_probe_at: __, ...summary } = newSnapshot;
      expect(result.current.agents.find((a) => a.agent_id === 42)).toEqual(summary);
    });
    // No extra listAgents fetch — SSE was the only source of the new row.
    expect(vi.mocked(api.listAgents).mock.calls.length).toBe(fetchesBefore);
  });

  it("AgentUpdated termination moves the row live→terminated without a refetch", async () => {
    // The terminated scope is always seeded, so the fold moves the row
    // across caches: it leaves the live roster and lands in the terminated
    // roster. The combined agents list still carries it (status=terminated)
    // — that row is the lineage joint that keeps its live children mounted.
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.agents).toEqual(MOCK_AGENTS);
    });
    await waitForEventSource();
    const fetchesBefore = vi.mocked(api.listAgents).mock.calls.length;

    const updated: AgentRow = { ...MOCK_AGENTS[0], status: "terminated" };
    act(() => {
      deliverSseMessage({
        role: "agent_updated",
        agent_id: 1,
        snapshot: updated,
      });
    });

    await waitFor(() => {
      expect(result.current.agents.find((a) => a.agent_id === 1)?.status).toBe(
        "terminated",
      );
      expect(_qc.getQueryData(TERMINATED_AGENTS_QUERY_KEY)).toContainEqual(updated);
    });
    // The second row stays exactly as it was — the merge is by id, not
    // a full-list replacement (which would race with concurrent updates).
    expect(result.current.agents.find((a) => a.agent_id === 2)?.status).toBe(
      "idling",
    );
    expect(vi.mocked(api.listAgents).mock.calls.length).toBe(fetchesBefore);
  });

  it("spawn → mutation resolves first, AgentSpawned removes placeholder", async () => {
    // Common ordering: POST /api/agents returns {id}, then the gateway
    // publishes AgentSpawned a few ms later. We hold the placeholder
    // through the gap by parking the id in pendingSpawnIds until the
    // cache contains it.
    vi.mocked(api.spawnAgent).mockResolvedValue({ id: 99 });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.activeId).toBe(1);
    });
    await waitForEventSource();

    await act(async () => {
      await result.current.spawn();
    });

    // Mutation resolved; cache still empty for id=99 → placeholder = 1.
    expect(result.current.pendingSpawnCount).toBe(1);
    expect(result.current.agents.find((a) => a.agent_id === 99)).toBeUndefined();

    // Deliver the AgentSpawned event.
    const snapshot: WireAgentRow = {
      agent_id: 99,
      spawner: "user",
      fork_source_agent_id: null,
      fork_source_checkpoint_id: null,
      status: "idling",
      pid: null,
      spawned_at: "2026-05-25T12:00:00Z",
      started_at: null,
      last_active_at: "2026-05-25T12:00:00Z", last_inbound_at: "2026-05-25T12:00:00Z",
      label: null,
      machine: "test-host",
      supports_vision: true,
      notices_awaiting_response: [], unread_notice_count: 0,
      heartbeat_paused_until: null,
      liveness_state: "online",
      last_probe_at: null,
    };
    act(() => {
      deliverSseMessage({
        role: "agent_spawned",
        agent_id: 99,
        snapshot,
      });
    });

    // Same render frame: placeholder gone, real row in.
    await waitFor(() => {
      expect(result.current.pendingSpawnCount).toBe(0);
    });
    expect(result.current.agents.find((a) => a.agent_id === 99)).toBeTruthy();
  });

  it("spawn → AgentSpawned arrives BEFORE mutation resolves; no double placeholder", async () => {
    // Race ordering: gateway publishes after DB commit but before
    // returning the HTTP response. Without the markSpawnPending
    // cache-precheck, the id would be added to pendingSpawnIds after
    // the cache already contained it, stranding a phantom placeholder
    // until the next agents change. The precheck collapses this to 0.
    let resolveSpawn: ((v: { id: number }) => void) | undefined;
    vi.mocked(api.spawnAgent).mockImplementation(
      () => new Promise((res) => { resolveSpawn = res; }),
    );
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.agents).toEqual(MOCK_AGENTS);
    });
    await waitForEventSource();

    let p: Promise<number | null>;
    act(() => {
      p = result.current.spawn();
    });
    await waitFor(() => {
      expect(result.current.pendingSpawnCount).toBe(1);
    });

    // SSE lands first.
    const snapshot: WireAgentRow = {
      agent_id: 99,
      spawner: "user",
      fork_source_agent_id: null,
      fork_source_checkpoint_id: null,
      status: "idling",
      pid: null,
      spawned_at: "2026-05-25T12:00:00Z",
      started_at: null,
      last_active_at: "2026-05-25T12:00:00Z", last_inbound_at: "2026-05-25T12:00:00Z",
      label: null,
      machine: "test-host",
      supports_vision: true,
      notices_awaiting_response: [], unread_notice_count: 0,
      heartbeat_paused_until: null,
      liveness_state: "online",
      last_probe_at: null,
    };
    act(() => {
      deliverSseMessage({
        role: "agent_spawned",
        agent_id: 99,
        snapshot,
      });
    });
    await waitFor(() => {
      expect(result.current.agents.find((a) => a.agent_id === 99)).toBeTruthy();
    });

    // Mutation resolves with the same id. markSpawnPending sees the row
    // already in cache → does NOT add to pendingSpawnIds. Count drops
    // straight to 0; no phantom placeholder.
    await act(async () => {
      resolveSpawn!({ id: 99 });
      await p!;
    });
    await waitFor(() => {
      expect(result.current.pendingSpawnCount).toBe(0);
    });
  });

  it("spawn → idling row arrives via SSE while POST still in flight → in-flight placeholder yields (no two rows)", async () => {
    // The "two rows on idling" bug: the gateway publishes AgentSpawned
    // right after DB commit, well before the HTTP response resolves (it
    // launches the agent process in between). So the idling row lands in
    // cache while spawnMutation.isPending is still true. The in-flight
    // placeholder must yield to that real row — otherwise the placeholder
    // and the idling row render at the same time (two rows for one spawn).
    let resolveSpawn: ((v: { id: number }) => void) | undefined;
    vi.mocked(api.spawnAgent).mockImplementation(
      () => new Promise((res) => { resolveSpawn = res; }),
    );
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      expect(result.current.agents).toEqual(MOCK_AGENTS);
    });
    await waitForEventSource();

    let p: Promise<number | null>;
    act(() => {
      p = result.current.spawn();
    });
    // POST in flight, SSE not yet arrived → one placeholder, no real row.
    await waitFor(() => {
      expect(result.current.pendingSpawnCount).toBe(1);
    });

    // SSE delivers the idling row BEFORE the POST resolves.
    const snapshot: WireAgentRow = {
      agent_id: 99,
      spawner: "user",
      fork_source_agent_id: null,
      fork_source_checkpoint_id: null,
      status: "idling",
      pid: null,
      spawned_at: "2026-05-25T12:00:00Z",
      started_at: null,
      last_active_at: "2026-05-25T12:00:00Z", last_inbound_at: "2026-05-25T12:00:00Z",
      label: null,
      machine: "test-host",
      supports_vision: true,
      notices_awaiting_response: [], unread_notice_count: 0,
      heartbeat_paused_until: null,
      liveness_state: "online",
      last_probe_at: null,
    };
    act(() => {
      deliverSseMessage({ role: "agent_spawned", agent_id: 99, snapshot });
    });
    await waitFor(() => {
      expect(result.current.agents.find((a) => a.agent_id === 99)).toBeTruthy();
    });

    // The real idling row is in cache while isPending is still true.
    // The in-flight placeholder must already be gone — total spawn rows = 1,
    // not 2. (Pre-fix this was 1: spawnMutation.isPending blindly added a
    // placeholder on top of the row.)
    expect(result.current.pendingSpawnCount).toBe(0);

    // POST resolves; markSpawnPending precheck skips the add → stays 0.
    await act(async () => {
      resolveSpawn!({ id: 99 });
      await p!;
    });
    expect(result.current.pendingSpawnCount).toBe(0);
  });

  it("agent_spawned before the initial list fetch lands → cache is NOT seeded (empty-cache guard)", async () => {
    // The fold (applyEvent → foldAgents) refuses to seed a one-agent partial
    // from an SSE event that races ahead of the initial list fetch: a lone
    // snapshot is not the whole fleet. Every live-roster writer converges onto
    // this one guarded path — the old fleet-view writer lacked the guard
    // (R15), so seeding was inconsistent by entry point.
    // Park listAgents so the cache stays empty (undefined) for the whole test.
    vi.mocked(api.listAgents).mockImplementation(
      () => new Promise(() => { /* never resolves */ }),
    );
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitForEventSource();

    const snapshot: AgentRow = { ...MOCK_AGENTS[0], agent_id: 7, label: "ghost" };
    act(() => {
      deliverSseMessage({ role: "agent_spawned", agent_id: 7, snapshot });
    });

    // Guard held: the cache was never seeded with the partial.
    expect(_qc.getQueryData(AGENTS_QUERY_KEY)).toBeUndefined();
    expect(result.current.agents).toEqual([]);
  });
});

describe("public three-state agent status projection", () => {
  const base = MOCK_AGENTS[0];

  it.each([
    ["running", "running"],
    ["idling", "idling"],
    ["restarting", "idling"],
    ["terminated", "terminated"],
  ] as const)("projects internal %s → public %s", (internal, expected) => {
    expect(projectAgentStatus({ ...base, status: internal }).status).toBe(expected);
  });

  it("keeps already-public statuses as the same row reference", () => {
    const running = { ...base, status: "running" as const };
    expect(projectAgentStatus(running)).toBe(running);
    const idling = { ...base, status: "idling" as const };
    expect(projectAgentStatus(idling)).toBe(idling);
    const terminated = { ...base, status: "terminated" as const };
    expect(projectAgentStatus(terminated)).toBe(terminated);
  });

  it("fails fast on an unknown wire status", () => {
    expect(() =>
      projectAgentStatus({ ...base, status: "future-state" } as never),
    ).toThrow(/unknown internal agent status.*future-state/i);
  });

  it("an SSE restarting snapshot lands in the cache as idling", () => {
    const snapshot = { ...base, status: "restarting" };
    const next = foldAgents(MOCK_AGENTS, {
      role: "agent_updated",
      snapshot,
    } as unknown as SystemEvent);
    const row = next?.find((a) => a.agent_id === base.agent_id);
    expect(row?.status).toBe("idling");
  });

  it("upsert stores only the projected status", () => {
    const projected = projectAgentStatus({ ...base, status: "restarting" as const });
    const out = upsertAgent([{ ...base, status: "idling" as const }], projected);
    expect(out?.[0]?.status).toBe("idling");
  });
});

describe("fold owner reconnect reconcile", () => {
  it("invalidates only global-fold query families when the SSE connection opens", async () => {
    const invalidateSpy = vi.spyOn(_qc, "invalidateQueries");
    renderHook(() => undefined, { wrapper });
    await waitForEventSource();

    act(() => {
      lastEventSource?.onopen?.call(
        lastEventSource as unknown as EventSource,
        new Event("open"),
      );
    });

    expect(invalidateSpy).toHaveBeenCalledTimes(RECONNECT_QUERY_KEYS.length);
    for (const queryKey of RECONNECT_QUERY_KEYS) {
      expect(invalidateSpy).toHaveBeenCalledWith({
        queryKey,
        exact:
          queryKey === AGENTS_QUERY_KEY ||
          queryKey === TERMINATED_AGENTS_QUERY_KEY,
      });
    }
    expect(invalidateSpy).not.toHaveBeenCalledWith();
  });

  it("refetches each scoped roster exactly once on reconnect", async () => {
    _qc.setQueryData(SETTINGS_QUERY_KEY, { "display.show_terminated": true });
    const { result } = renderHook(() => useAgents(noop), { wrapper });
    await waitFor(() => {
      // Wait for both initial snapshots to publish. Invalidating a query while
      // its first request is still in flight deliberately reuses that request,
      // so firing open earlier would not exercise reconnect refetch behavior.
      expect(result.current.isLoading).toBe(false);
      expect(vi.mocked(api.listAgents).mock.calls.filter(([scope]) => scope === "live")).toHaveLength(1);
      expect(
        vi.mocked(api.listAgents).mock.calls.filter(([scope]) => scope === "terminated"),
      ).toHaveLength(1);
    });
    await waitForEventSource();

    act(() => {
      lastEventSource?.onopen?.call(
        lastEventSource as unknown as EventSource,
        new Event("open"),
      );
    });

    await waitFor(() => {
      expect(vi.mocked(api.listAgents).mock.calls.filter(([scope]) => scope === "live")).toHaveLength(2);
      expect(
        vi.mocked(api.listAgents).mock.calls.filter(([scope]) => scope === "terminated"),
      ).toHaveLength(2);
    });
  });
});
