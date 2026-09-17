"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef } from "react";

import { createQueryRepairScheduler } from "./fold/repair";
import { useDocumentVisible } from "./use-document-visible";

/** The three selected-stream readers share the same ownership boundary:
 * losing selection/visibility abandons in-flight reads and trailing work.
 * Pending still repairs its own key on the events that change the queue; the
 * open-gap trailing read is the shared composed reconcile's job
 * (agent-reconcile.ts), which the three readers request together.
 */
export function useAgentReadRepair(
  domain: "timeline" | "pending" | "token-usage",
  agentId: number | null,
) {
  const client = useQueryClient();
  const isVisible = useDocumentVisible();
  const requestRef = useRef<((immediate: boolean) => void) | null>(null);

  useEffect(() => {
    if (agentId === null || !isVisible) return;
    const key = [domain, agentId] as const;
    const scheduler = createQueryRepairScheduler(client);
    requestRef.current = (immediate) => scheduler.request(key, immediate);
    return () => {
      requestRef.current = null;
      scheduler.dispose();
      void client.cancelQueries({ queryKey: key, exact: true });
    };
  }, [client, domain, agentId, isVisible]);

  const requestRepair = useCallback((immediate = true) => {
    requestRef.current?.(immediate);
  }, []);
  return { isVisible, requestRepair };
}
