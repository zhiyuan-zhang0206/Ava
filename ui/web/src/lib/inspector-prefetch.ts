"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef } from "react";

import { api } from "./api";
import { useInspectorHours, useInspectorOpen } from "./inspector-panel-store";

export const COMPACT_INSPECT_WINDOW = -1;
export const INSPECT_PREFETCH_DELAY_MS = 300;

export const inspectLiveQueryKey = (agentId: number) =>
  ["agent-inspect-live", agentId] as const;

export const inspectWindowedQueryKey = (agentId: number, hours: number | null) =>
  ["agent-inspect", agentId, hours] as const;

export function fetchWindowedInspect(
  agentId: number,
  hours: number | null,
  signal?: AbortSignal,
) {
  return api.getAgentInspect(
    agentId,
    hours === COMPACT_INSPECT_WINDOW ? null : hours,
    hours === COMPACT_INSPECT_WINDOW,
    signal,
  );
}

/** Debounced, open-panel-only prefetch handlers for one sidebar row. */
export function useInspectorPrefetch(agentId: number) {
  const queryClient = useQueryClient();
  const { open } = useInspectorOpen();
  const { inspectorHours } = useInspectorHours();
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const startedRef = useRef(false);

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const cancel = useCallback(() => {
    clearTimer();
    if (!startedRef.current) return;
    startedRef.current = false;
    void queryClient.cancelQueries({ queryKey: inspectLiveQueryKey(agentId) });
    void queryClient.cancelQueries({
      queryKey: inspectWindowedQueryKey(agentId, inspectorHours),
    });
  }, [agentId, clearTimer, inspectorHours, queryClient]);

  const prefetch = useCallback(() => {
    clearTimer();
    if (!open) return;
    startedRef.current = true;
    void queryClient.prefetchQuery({
      queryKey: inspectLiveQueryKey(agentId),
      queryFn: ({ signal }) => api.getAgentInspectLive(agentId, signal),
    });
    void queryClient.prefetchQuery({
      queryKey: inspectWindowedQueryKey(agentId, inspectorHours),
      queryFn: ({ signal }) => fetchWindowedInspect(agentId, inspectorHours, signal),
    });
  }, [agentId, clearTimer, inspectorHours, open, queryClient]);

  const schedule = useCallback(() => {
    clearTimer();
    if (!open) return;
    timerRef.current = setTimeout(prefetch, INSPECT_PREFETCH_DELAY_MS);
  }, [clearTimer, open, prefetch]);

  useEffect(() => {
    if (!open) cancel();
    return cancel;
  }, [cancel, open]);

  return { prefetch, schedule, cancel };
}
