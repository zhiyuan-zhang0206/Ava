// ava:ref ui/web/src/docs/frontend-state/shared-context.ava.okf.md#refresh-ownership
"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "../transport/api";

export function useToUser(agentId: number | null, references: string[] = []) {
  return useQuery({
    queryKey: references.length ? ["to-user", agentId, ...references] : ["to-user", agentId],
    queryFn: ({ signal }) => {
      if (agentId == null) throw new Error("To user requires an agent");
      return api.getToUser(agentId, signal, references);
    },
    enabled: agentId != null,
    staleTime: Infinity,
    retry: false,
    refetchOnMount: "always",
    refetchOnWindowFocus: "always",
  });
}
