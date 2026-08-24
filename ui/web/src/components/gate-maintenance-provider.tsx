"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";

import {
  getGateMaintenanceState,
  reloadThroughGate,
  UI_UPDATE_QUERY_KEY,
} from "@/lib/gate-maintenance";

const POLL_MS = 15_000;

/**
 * Poll the unauthenticated Gate marker for every open SPA state.
 *
 * This provider deliberately sits outside the auth-gated operational pollers:
 * login/loading/session-invalid tabs must still navigate through Gate when a
 * rollout begins. It never renders or times maintenance state.
 */
export function GateMaintenanceProvider() {
  const { data } = useQuery({
    queryKey: UI_UPDATE_QUERY_KEY,
    queryFn: getGateMaintenanceState,
    refetchInterval: POLL_MS,
  });

  useEffect(() => {
    if (data != null && data.status !== "inactive") reloadThroughGate();
  }, [data]);

  return null;
}
