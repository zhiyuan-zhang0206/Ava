"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "../transport/api";

/** File change hints and reconnect repair belong to the global SSE fold. */
export function useToUser(agentId: number | null) {
  return useQuery({
    queryKey: ["to-user", agentId],
    queryFn: ({ signal }) => {
      if (agentId == null) throw new Error("To user requires an agent");
      return api.getToUser(agentId, signal);
    },
    enabled: agentId != null,
    staleTime: Infinity,
    retry: false,
    refetchOnMount: "always",
    refetchOnWindowFocus: "always",
  });
}
