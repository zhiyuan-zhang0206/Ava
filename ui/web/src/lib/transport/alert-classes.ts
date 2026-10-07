// The sidebar warning/error card's classes: the selected window's classes, one class's newest
// events, and the dismiss / reopen actions. Every action invalidates the whole ["stats"] family,
// so the card's active-class count and the list move together.

import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";

import { api } from "./api";
import type { StatsWindowHours } from "../agents/sidebar";
import type { AlertClassRow, AlertClassSample, AlertClassesResponse } from "../contracts/types";

type ClassIdentity = Pick<AlertClassRow, "level" | "event_name" | "source" | "process">;

const STATS_KEY = ["stats"] as const;

export function useAlertClasses(
  windowHours: StatsWindowHours,
  enabled: boolean,
): UseQueryResult<AlertClassesResponse> {
  return useQuery({
    queryKey: [...STATS_KEY, "alert-classes", windowHours],
    queryFn: ({ signal }) => api.getAlertClasses(windowHours, signal),
    enabled,
    retry: 1,
    staleTime: 15_000,
    // Switching the window keeps the previous list on screen until the new one lands.
    placeholderData: keepPreviousData,
  });
}

export function useAlertClassSamples(
  cls: ClassIdentity,
  windowHours: StatsWindowHours,
  enabled: boolean,
): UseQueryResult<AlertClassSample[]> {
  return useQuery({
    queryKey: [
      ...STATS_KEY,
      "alert-class-samples",
      windowHours,
      cls.level,
      cls.event_name,
      cls.source,
      cls.process,
    ],
    queryFn: ({ signal }) => api.getAlertClassSamples(cls, windowHours, signal),
    enabled,
    retry: 1,
    staleTime: 15_000,
  });
}

export function useAlertClassActions() {
  const queryClient = useQueryClient();
  const refresh = () => queryClient.invalidateQueries({ queryKey: STATS_KEY });
  const dismiss = useMutation({
    mutationFn: (cls: AlertClassRow) => api.dismissAlertClass(cls),
    onSettled: refresh,
  });
  const reopen = useMutation({
    mutationFn: (dismissalId: number) => api.reopenAlertClass(dismissalId),
    onSettled: refresh,
  });
  return { dismiss, reopen };
}
