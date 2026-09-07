"use client";

// Lifecycle actions for the agent cache — extracted from use-agents.ts (R4
// layer-1 line budget: use-agents ≤400). Public API unchanged: useAgents
// composes useAgentActions and spreads its result, so readers of the hook
// (and the tests) keep the same surface.
//
// No optimistic writes: useMutation drives only the per-row pending flag; the
// row updates when the SSE event arrives carrying the real new state (folded
// by the root fold owner). Until then the button shows a spinner.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "./api";
import { errMsg } from "./errors";
import { AGENTS_QUERY_KEY, TERMINATED_AGENTS_QUERY_KEY } from "./fold/agents";
import { track } from "./telemetry";
import { useStore } from "./store";
import type { AgentRow } from "./types";

export type PendingAction = "restarting" | "terminating" | "resurrecting" | "compacting";

export interface AgentActions {
  pendingActions: Record<number, PendingAction>;
  pendingSpawnCount: number;
  forkPending: boolean;
  spawn: (machine?: string, model?: string, preset?: string, reasoning_effort?: string) => Promise<number | null>;
  fork: (sourceId: number, prompt?: string) => Promise<number | null>;
  terminate: (id: number, force?: boolean) => Promise<void>;
  restart: (id: number) => Promise<void>;
  resurrect: (id: number, prompt?: string) => Promise<void>;
  compact: (id: number) => Promise<void>;
}

export function useAgentActions(
  showError: (msg: string) => void,
  agents: readonly AgentRow[],
): AgentActions {
  const queryClient = useQueryClient();
  const setActiveId = useStore((s) => s.setActiveId);
  // ── Lifecycle mutations — useMutation drives the per-row pending flag.
  // No optimistic writes: the row updates when the SSE event arrives
  // carrying the real new state. Until then the button shows a spinner. ──

  const spawnMutation = useMutation({
    // machine provided → use cross-machine forward; omitted → empty body {},
    // the backend defaults to the gateway machine that received the request.
    // model provided → include config.llm_model; omit config entirely when
    // model is undefined (cluster default, no override needed).
    mutationFn: ({
      machine,
      model,
      preset,
      reasoning_effort,
    }: {
      machine?: string;
      model?: string;
      preset?: string;
      reasoning_effort?: string;
    }) =>
      api.spawnAgent({
        ...(machine !== undefined ? { machine } : {}),
        ...(model !== undefined || reasoning_effort !== undefined
          ? { config: { ...(model !== undefined ? { llm_model: model } : {}), ...(reasoning_effort !== undefined ? { reasoning_effort } : {}) } }
          : {}),
        ...(preset !== undefined ? { preset } : {}),
      }),
    onSuccess: () => track("spawn"),
    onError: (e: unknown) => showError(`Spawn failed: ${errMsg(e)}`),
  });

  const forkMutation = useMutation({
    // Fork defaults to the source agent's machine — sharing local
    // resources on the same node (~/.ava/memory / plugin config /
    // panel server etc.) and avoiding unnecessary cross-machine
    // forwards. If source can not be found, the UI is holding a stale
    // id (the button should not appear for a non-existent agent) —
    // throw directly so the onError path showErrors instead of
    // silently masking the real bug.
    mutationFn: ({ sourceId, prompt }: { sourceId: number; prompt?: string }) => {
      // Fork remains available while a terminated conversation is selected.
      // Resolve at mutation time from both authoritative scoped caches instead
      // of a render-time closure, so a recent SSE move between scopes cannot
      // strand the source row in the sibling cache.
      const source = [
        ...(queryClient.getQueryData<AgentRow[]>(AGENTS_QUERY_KEY) ?? []),
        ...(queryClient.getQueryData<AgentRow[]>(TERMINATED_AGENTS_QUERY_KEY) ?? []),
      ].find((agent) => agent.agent_id === sourceId);
      if (!source) throw new Error(`Fork source agent #${sourceId} not in cache`);
      // A prompt requires prompt_source (backend rejects prompt without it);
      // a frontend prompt always comes from the user. No prompt → omit both.
      return api.spawnAgent({
        fork_from: sourceId,
        machine: source.machine,
        ...(prompt !== undefined ? { prompt, prompt_source: "user" } : {}),
      });
    },
    onSuccess: () => track("fork"),
    onError: (e: unknown) => showError(`Fork failed: ${errMsg(e)}`),
  });

  const terminateMutation = useMutation({
    mutationFn: ({ id, force }: { id: number; force: boolean }) =>
      api.terminateAgent(id, force),
    onSuccess: (data) => {
      // Acceptance is not observed exit; lifecycle rows remain SSE-owned.
      const messages = {
        enqueued: "Termination requested",
        already_terminated: "Already terminated",
      };
      useStore.getState().showToast(messages[data.status]);
      track("terminate");
    },
    onError: (e: unknown) => showError(`Terminate failed: ${errMsg(e)}`),
  });

  const restartMutation = useMutation({
    mutationFn: (id: number) => api.restartAgent(id),
    onSuccess: () => track("restart"),
    onError: (e: unknown) => showError(`Restart failed: ${errMsg(e)}`),
  });

  const resurrectMutation = useMutation({
    mutationFn: ({ id, prompt }: { id: number; prompt?: string }) =>
      api.resurrectAgent(id, prompt),
    onSuccess: () => track("resurrect"),
    onError: (e: unknown) => showError(`Resurrect failed: ${errMsg(e)}`),
  });

  const compactMutation = useMutation({
    mutationFn: (id: number) => api.compact(id, "framework"),
    onSuccess: () => track("compact"),
    onError: (e: unknown) => showError(`Compact failed: ${errMsg(e)}`),
  });

  // -- Derive pending state from mutation state, exposed to components --

  const pendingActions = useMemo<Record<number, PendingAction>>(() => {
    // isPending discriminates the mutation-result union — variables is
    // guaranteed present on the pending arm, no null check needed.
    const out: Record<number, PendingAction> = {};
    if (terminateMutation.isPending) {
      out[terminateMutation.variables.id] = "terminating";
    }
    if (restartMutation.isPending) {
      out[restartMutation.variables] = "restarting";
    }
    if (resurrectMutation.isPending) {
      out[resurrectMutation.variables.id] = "resurrecting";
    }
    if (compactMutation.isPending) {
      out[compactMutation.variables] = "compacting";
    }
    return out;
  }, [
    terminateMutation.isPending,
    terminateMutation.variables,
    restartMutation.isPending,
    restartMutation.variables,
    resurrectMutation.isPending,
    resurrectMutation.variables,
    compactMutation.isPending,
    compactMutation.variables,
  ]);

  // Track ids that have been spawned (mutation returned an id) but whose
  // AgentSpawned SSE event has not yet landed the row into the cache. The
  // SpawningRow placeholder stays visible for each id in this set, then
  // is replaced by the real row in the same render frame the snapshot
  // arrives. Without this, the brief window between mutation-resolve and
  // SSE-arrival flashes as "no placeholder, no row".
  const [pendingSpawnIds, setPendingSpawnIds] = useState<ReadonlySet<number>>(
    () => new Set(),
  );

  // Agent ids present when the most recent spawn was fired. The gateway
  // publishes AgentSpawned right after DB commit — before launching the
  // process and returning the HTTP response — so the new unclaimed idling row
  // routinely lands in cache while spawnMutation.isPending is still true.
  // The in-flight placeholder below diffs against this baseline to yield to
  // the real row instead of rendering on top of it (the "two rows" bug).
  // State, not a ref: it is read during render (in pendingSpawnCount), and
  // React 18 batches this set with the mutation's isPending flip in the same
  // synchronous spawn() call, so both land in one render frame.
  const [spawnBaseline, setSpawnBaseline] = useState<ReadonlySet<number>>(
    () => new Set(),
  );

  // Drop pending ids whose snapshot now lives in the cache.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- derived-state sync against the query cache; the functional updater returns prev when nothing changed, so no render loop
    setPendingSpawnIds((prev) => {
      if (prev.size === 0) return prev;
      let mutated = false;
      const next = new Set<number>();
      for (const id of prev) {
        if (agents.some((a) => a.agent_id === id)) {
          mutated = true;
        } else {
          next.add(id);
        }
      }
      return mutated ? next : prev;
    });
  }, [agents]);

  // Placeholder count = in-flight phase + post-resolve phase.
  //   - in-flight: the spawn POST is unresolved. Show one placeholder
  //     UNLESS this spawn's row already arrived via SSE — a row outside
  //     spawnBaselineRef means the real idling row is already in cache,
  //     so the placeholder must yield rather than render a second row.
  //   - post-resolve: id is known, held in pendingSpawnIds until its
  //     snapshot lands in cache (then the agents-change effect drops it).
  const pendingSpawnCount = useMemo(() => {
    let inFlight = 0;
    if (spawnMutation.isPending) {
      const arrived = agents.some((a) => !spawnBaseline.has(a.agent_id));
      inFlight = arrived ? 0 : 1;
    }
    return inFlight + pendingSpawnIds.size;
  }, [spawnMutation.isPending, agents, pendingSpawnIds, spawnBaseline]);

  const forkPending = forkMutation.isPending;

  // -- Action wrappers --

  const markSpawnPending = useCallback(
    (id: number) => {
      // If the AgentSpawned event already landed (gateway publishes before
      // returning the HTTP response, so this is a real race window), the
      // snapshot is in the cache and we don't need a placeholder at all.
      // Skipping the add here also keeps the agents-change useEffect from
      // having to chase ids that were already resolved.
      const already = (
        queryClient.getQueryData<AgentRow[]>(AGENTS_QUERY_KEY) ?? []
      ).some((a) => a.agent_id === id);
      if (already) return;
      setPendingSpawnIds((prev) => {
        if (prev.has(id)) return prev;
        const next = new Set(prev);
        next.add(id);
        return next;
      });
    },
    [queryClient],
  );

  const spawn = useCallback(
    async (machine?: string, model?: string, preset?: string, reasoning_effort?: string): Promise<number | null> => {
      // Snapshot current ids so the in-flight placeholder can detect when
      // this spawn's AgentSpawned row arrives via SSE (which beats the HTTP
      // response) and yield to it instead of double-rendering.
      setSpawnBaseline(
        new Set(
          (queryClient.getQueryData<AgentRow[]>(AGENTS_QUERY_KEY) ?? []).map(
            (a) => a.agent_id,
          ),
        ),
      );
      try {
        const { id } = await spawnMutation.mutateAsync({ machine, model, preset, reasoning_effort });
        // Hold the SpawningRow placeholder until AgentSpawned arrives.
        markSpawnPending(id);
        setActiveId(id);
        return id;
      } catch {
        return null;
      }
    },
    [spawnMutation, setActiveId, markSpawnPending, queryClient],
  );

  const fork = useCallback(
    async (sourceId: number, prompt?: string): Promise<number | null> => {
      try {
        const { id } = await forkMutation.mutateAsync({ sourceId, prompt });
        markSpawnPending(id);
        setActiveId(id);
        return id;
      } catch {
        return null;
      }
    },
    [forkMutation, setActiveId, markSpawnPending],
  );

  const terminate = useCallback(
    async (id: number, force = false) => {
      // Task #837 (user ruling): terminating the currently SELECTED agent
      // auto-switches to the nearest adjacent alive agent — the user never
      // sits on a row that is about to die. Neighbor order: next in list
      // order, wrapping around to the front when the terminated agent was
      // last. Skipping terminated candidates keeps a multi-kill session
      // landing on a live row.
      const prev = useStore.getState().activeId;
      if (prev === id) {
        const list =
          queryClient.getQueryData<AgentRow[]>(AGENTS_QUERY_KEY) ?? [];
        const idx = list.findIndex((a) => a.agent_id === id);
        const neighbors = [...list.slice(idx + 1), ...list.slice(0, idx)];
        const next = neighbors.find((a) => a.status !== "terminated");
        if (next) setActiveId(next.agent_id);
      }
      // mutateAsync throws on onError; catch here because the error
      // toast is already handled in onError; the caller does not need
      // to know about it.
      try {
        await terminateMutation.mutateAsync({ id, force });
      } catch {
        // error already handled in onError callback
      }
    },
    [terminateMutation, queryClient, setActiveId],
  );

  const restart = useCallback(
    async (id: number) => {
      try {
        await restartMutation.mutateAsync(id);
      } catch {
        // error already handled in onError callback
      }
    },
    [restartMutation],
  );

  const resurrect = useCallback(
    async (id: number, prompt?: string) => {
      try {
        await resurrectMutation.mutateAsync({ id, prompt });
      } catch {
        // error already handled in onError callback
      }
    },
    [resurrectMutation],
  );

  const compact = useCallback(
    async (id: number) => {
      try {
        await compactMutation.mutateAsync(id);
      } catch {
        // error already handled in onError callback
      }
    },
    [compactMutation],
  );

  return {
    pendingActions,
    pendingSpawnCount,
    forkPending,
    spawn,
    fork,
    terminate,
    restart,
    resurrect,
    compact,
  };
}
