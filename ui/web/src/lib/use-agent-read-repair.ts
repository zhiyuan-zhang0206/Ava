"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef } from "react";

import { createQueryRepairScheduler } from "./fold/repair";
import { useDocumentVisible } from "./use-document-visible";

/** The three selected-stream readers share the same ownership boundary.
 * Opening during a read requires a trailing read; losing selection/visibility
 * abandons both that repair and its HTTP request. This does not version data.
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
