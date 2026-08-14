// useTasks — the data source for the Task Graph.
// Fetches the full /api/tasks (the agent_tasks table). Freshness is SSE-driven:
// task_created / task_updated events (published by the ava.tasks SDK write path)
// are folded by the R4 fold owner (lib/fold/tasks — debounced invalidation),
// with a slow 30s poll as the reconciliation fallback for changes that skip the
// SDK (the gateway PATCH path, daemon counter writes). There is no client-side
// fallback: the view reads the backend registry or shows an empty/error state.

"use client";

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { api } from "./api";
import type { TaskListResponse, TaskRow } from "./types";

const EMPTY_TASKS: TaskListResponse = { tasks: [] };

export interface TasksResult {
  readonly tasks: readonly TaskRow[];
  readonly loading: boolean;
  readonly error: boolean;
}

export function useTasks(): TasksResult {
  const { data, isError, isLoading } = useQuery({
    queryKey: ["tasks"],
    queryFn: () => api.getTasks(),
    retry: false,
    // Slow reconciliation poll beneath the SSE invalidation below — a CONSTANT
    // interval (not success-gated), so a failed poll keeps retrying and the board
    // self-heals instead of freezing when SSE is also down.
    refetchInterval: 30_000,
  });

  const result = useMemo(() => {
    // Stale-while-error: once we have data, keep serving it even if the latest
    // poll failed (error carries the failure so the view can flag it). Only fall
    // back to the empty list when there is nothing loaded yet.
    if (data) {
      return { tasks: data.tasks, loading: false, error: isError };
    }
    return { tasks: EMPTY_TASKS.tasks, loading: isLoading, error: isError };
  }, [data, isError, isLoading]);
  return result;
}
