// useTasks — the task-list data source for the Task Graph and inbox.
// Fetches the caller-selected /api/tasks projection. Freshness is SSE-driven:
// task_created / task_updated events (published by the ava.tasks SDK write path)
// are folded by the R4 fold owner (lib/fold/tasks — debounced invalidation),
// with a slow 30s poll as the reconciliation fallback for changes that skip the
// SDK (the gateway PATCH path, daemon counter writes). There is no client-side
// fallback: the view reads the backend registry or shows an empty/error state.

"use client";

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { api } from "./api";
import type { TaskFields, TaskListResponse, TaskRow, TaskSummaryRow } from "./types";

const EMPTY_TASKS: TaskListResponse = { tasks: [] };

export interface TasksResult<T extends TaskSummaryRow = TaskSummaryRow> {
  readonly tasks: readonly T[];
  readonly loading: boolean;
  readonly error: boolean;
}

/** Last-activity window for the task list — the task graph's time filter.
 *  "all" (the default) keeps the unfiltered registry. */
export type TaskWindow = "24h" | "7d" | "30d" | "all";

export function useTasks(window?: TaskWindow, fields?: "full"): TasksResult<TaskRow>;
export function useTasks(window: TaskWindow, fields: "summary"): TasksResult;
export function useTasks(
  window: TaskWindow = "all",
  fields: TaskFields = "full",
): TasksResult {
  const { data, isError, isLoading } = useQuery({
    // Projection and window join the key so neither can reuse the other's
    // rows. The SSE fold owner invalidates ["tasks"] which prefix-matches all.
    queryKey: ["tasks", fields, window],
    queryFn: () => api.getTasks({ window, fields }),
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
