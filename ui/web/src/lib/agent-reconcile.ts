"use client";

// One composed switch-refresh read for the selected conversation surface
// (task #3900 batch 2 / #3910).
//
// On (re)attach the shared detail stream fires "open" to the timeline,
// token-usage, and pending readers in the same tick; before this module each
// answered with its own trailing read (three requests). Now the three share
// ONE `GET /api/agents/{id}/conversation-snapshot` whose sections are written
// straight into the same three query keys, so every reader renders the
// refreshed snapshot without a read of its own.
//
// Ordering rules — the composed write must never land older data after newer:
// - Join, don't cancel (the same rule as fold/repair.ts): a read already in
//   flight predates the request, so the composed read starts only once
//   nothing is fetching.
// - If a newer write lands while the composed read is in flight (an
//   event-triggered refetch, a fold write), the snapshot is dropped and one
//   trailing read is owed — the pre-reconcile repair semantics.
// - A request during a run (a second subscription gap) also leaves one
//   trailing read.
// - compact_done aborts an in-flight reconcile (use-timeline.ts): a
//   pre-compact snapshot must not land after the post-compact window is
//   re-read — the same staleness the timeline cancelQueries prevents for its
//   own fetch.
// A failed reconcile falls back to invalidating the three keys (the
// pre-batch-2 repair path); each reader then repairs itself and its error
// surfaces through its own query state.

import type { QueryClient, QueryKey } from "@tanstack/react-query";
import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect } from "react";

import { api } from "./api";
import { useDocumentVisible } from "./use-document-visible";

/** Delay before the queued run fires: long enough for the same-tick request
 *  burst (the three readers of one "open") to merge into a single run,
 *  without holding the refresh past the current frame. */
const QUEUE_MS = 0;
/** Join re-check and trailing-run cadence — the coalesce window repair.ts
 *  uses for the same purpose. */
const RETRY_MS = 200;

interface Reconcile {
  /** Conversation readers attached for this agent. */
  subscribers: number;
  running: boolean;
  /** A request arrived during a run — another read is owed afterwards. */
  dirty: boolean;
  timer: ReturnType<typeof setTimeout> | null;
  controller: AbortController | null;
}

export interface AgentReconcile {
  /** Attach one conversation reader for `agentId`; returns its detach.
   *  Detaching the last reader abandons in-flight and queued work. */
  subscribe(agentId: number): () => void;
  /** A subscription gap opened for `agentId` — refresh its three read models
   *  with one composed read. No-op without a live reader. */
  request(agentId: number): void;
  /** Drop queued/in-flight reconcile work for `agentId` (compact_done: a
   *  superseding read takes over). */
  abort(agentId: number): void;
}

export function createAgentReconcile(client: QueryClient): AgentReconcile {
  const entries = new Map<number, Reconcile>();

  const keysFor = (agentId: number): QueryKey[] => [
    ["timeline", agentId],
    ["token-usage", agentId],
    ["pending", agentId],
  ];

  const anyFetching = (agentId: number): boolean =>
    keysFor(agentId).some((key) =>
      client
        .getQueryCache()
        .findAll({ queryKey: key })
        .some((query) => query.state.fetchStatus === "fetching"),
    );

  const schedule = (agentId: number, entry: Reconcile, delay: number): void => {
    entry.timer = setTimeout(() => { void run(agentId, entry); }, delay);
  };

  const run = async (agentId: number, entry: Reconcile): Promise<void> => {
    entry.timer = null;
    if (entries.get(agentId) !== entry) return;
    if (anyFetching(agentId)) {
      schedule(agentId, entry, RETRY_MS);
      return;
    }
    entry.running = true;
    entry.dirty = false;
    const controller = new AbortController();
    entry.controller = controller;
    // Baselines for the ordering guard: any data identity that replaces these
    // while the read is in flight is newer than its snapshot (identity, not a
    // timestamp — two writes inside one millisecond must not conflate).
    const baseline = keysFor(agentId).map((key) => client.getQueryState(key)?.data);
    try {
      const snapshot = await api.getConversationSnapshot(agentId, controller.signal);
      if (controller.signal.aborted || entries.get(agentId) !== entry) return;
      const superseded = keysFor(agentId).some((key, index) => {
        const state = client.getQueryState(key);
        if (state === undefined) return false;
        return state.fetchStatus === "fetching" || state.data !== baseline[index];
      });
      if (superseded) {
        // A newer read landed (or is landing) while this one was in flight —
        // drop the snapshot and owe one trailing read that starts after it.
        entry.dirty = true;
        return;
      }
      client.setQueryData(["timeline", agentId], snapshot.timeline);
      client.setQueryData(["token-usage", agentId], snapshot.token_usage);
      client.setQueryData(["pending", agentId], snapshot.pending);
    } catch {
      if (controller.signal.aborted) return; // ownership was lost; nothing to repair
      for (const key of keysFor(agentId)) {
        void client.invalidateQueries({ queryKey: key }, { cancelRefetch: false });
      }
    } finally {
      entry.controller = null;
      entry.running = false;
      if (entry.dirty && entries.get(agentId) === entry) {
        entry.dirty = false;
        schedule(agentId, entry, RETRY_MS);
      }
    }
  };

  return {
    subscribe(agentId: number): () => void {
      let entry = entries.get(agentId);
      if (entry === undefined) {
        entry = { subscribers: 0, running: false, dirty: false, timer: null, controller: null };
        entries.set(agentId, entry);
      }
      entry.subscribers += 1;
      return () => {
        const current = entries.get(agentId);
        if (current === undefined) return;
        current.subscribers -= 1;
        if (current.subscribers > 0) return;
        // Ownership lost: abandon queued and in-flight work — a late write
        // must not land for an agent no reader owns any more.
        if (current.timer !== null) {
          clearTimeout(current.timer);
          current.timer = null;
        }
        current.controller?.abort();
        entries.delete(agentId);
      };
    },

    request(agentId: number): void {
      const entry = entries.get(agentId);
      if (entry === undefined) return; // no live reader owns the surface
      if (entry.running) {
        entry.dirty = true; // a gap opened during a read — trail it
        return;
      }
      if (entry.timer !== null) return; // the queued run already covers this tick
      schedule(agentId, entry, QUEUE_MS);
    },

    abort(agentId: number): void {
      const entry = entries.get(agentId);
      if (entry === undefined) return;
      if (entry.timer !== null) {
        clearTimeout(entry.timer);
        entry.timer = null;
      }
      entry.dirty = false;
      entry.controller?.abort();
      entry.controller = null;
    },
  };
}

// One coordinator per browser QueryClient (therefore per page): the three
// readers mount independently, so the shared instance — not a per-hook one —
// is what lets one "open" burst collapse into one composed read.
const coordinators = new WeakMap<QueryClient, AgentReconcile>();

function coordinatorFor(client: QueryClient): AgentReconcile {
  let coordinator = coordinators.get(client);
  if (coordinator === undefined) {
    coordinator = createAgentReconcile(client);
    coordinators.set(client, coordinator);
  }
  return coordinator;
}

/** The conversation readers' shared switch-refresh hook (one call per reader
 *  — the timeline, token-usage, and pending hooks each call it once). */
export function useAgentReconcile(agentId: number | null): {
  requestReconcile: () => void;
  abortReconcile: () => void;
} {
  const client = useQueryClient();
  const isVisible = useDocumentVisible();

  useEffect(() => {
    if (agentId === null || !isVisible) return;
    return coordinatorFor(client).subscribe(agentId);
  }, [client, agentId, isVisible]);

  const requestReconcile = useCallback(() => {
    if (agentId === null) return;
    coordinatorFor(client).request(agentId);
  }, [client, agentId]);

  const abortReconcile = useCallback(() => {
    if (agentId === null) return;
    coordinatorFor(client).abort(agentId);
  }, [client, agentId]);

  return { requestReconcile, abortReconcile };
}
