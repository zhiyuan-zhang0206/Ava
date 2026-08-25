// useTokenUsage — shows context window occupancy.
//
// Two data sources:
// 1. React Query ["token-usage", agentId] — GET /api/agents/{id}/token-usage
//    historical latest value, with per-thread cache + stale-while-revalidate.
//    When switching back to a previously visited thread, the cache hit
//    shows the cached value instantly while a background refresh runs.
// 2. Real-time SSE token_usage event — published by the backend when
//    an LLM call completes, overwriting the cached value. SSE rate =
//    one per LLM call.
//
// Chunk-level can not get accurate input_tokens (Anthropic / OpenAI
// usage_metadata are both returned at stream end), so SSE rate = one
// per LLM call.
//
// SSE subscription uses the AgentEventStreamProvider shared active-agent
// connection (/api/system/all?agents=… while visible, the same throttled
// stream as useTimeline, not two separate connections). Hidden tabs receive
// a 7s poll signal that invalidates this REST snapshot.
//
// Token count has migrated from local useState to Zustand store.tokenUsage.

"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef } from "react";

import { api } from "./api";
import { useTimelineStore } from "./timeline-store";
import type { SystemEvent, TokenUsageResponse } from "./types";
import type { ConnectionEvent } from "./useEventStream";
import { useAgentEventStream } from "./useEventStream";

export interface TokenUsageState {
  contextTokens: number;
  maxContextTokens: number;
  /** Per-agent wind-down-reminder threshold (a fraction of the model window). */
  softCompactTokens: number;
  /** Per-agent force-compact ceiling (a fraction of the model window). */
  hardCompactTokens: number;
}

export function useTokenUsage(
  agentId: number | null,
  showError: (msg: string) => void,
): TokenUsageState {
  const queryClient = useQueryClient();
  const tokenUsage = useTimelineStore((s) => s.tokenUsage);
  const maxContextTokens = useTimelineStore((s) => s.maxContextTokens);
  const softCompactTokens = useTimelineStore((s) => s.softCompactTokens);
  const hardCompactTokens = useTimelineStore((s) => s.hardCompactTokens);
  const processSseEvent = useTimelineStore((s) => s.processSseEvent);
  const applyTokenUsage = useTimelineStore((s) => s.applyTokenUsage);
  // parse-failed dedupe — same pattern as useTimeline; report each
  // agent+error only once to prevent schema-drift toast floods on the
  // high-frequency token_usage event.
  const seenParseErrors = useRef<Set<string>>(new Set());

  // -- React Query: token-usage snapshot, cached by agentId --
  // staleTime 30s: within 30s, switch-back uses cache directly
  // gcTime 30min: keep inactive thread cache so returning from another
  //   page restores instantly, not just a quick sidebar agent-switch
  const tokenQuery = useQuery({
    queryKey: ["token-usage", agentId] as const,
    // eslint-disable-next-line @typescript-eslint/no-non-null-assertion -- the `enabled` gate below guarantees agentId is set before queryFn runs (standard TanStack idiom the types cannot see)
    queryFn: () => api.getTokenUsage(agentId!),
    enabled: agentId != null,
    staleTime: 30_000,
    gcTime: 30 * 60_000,
    // A transient checkpoint read failure (gateway restart / DB reconnect
    // window) is served as 0/0 by the backend — the SSE token_usage push
    // refreshes the UI only while the agent is actually working, so an idle
    // agent's context bar would stay hidden after a refresh that landed in
    // the window. Retry with backoff: 1+2+4+8+16s covers a rollout (~30s)
    // and recovers the bar instead of leaving it at 0 (Task #891).
    retry: 5,
    retryDelay: (attempt) => 2 ** attempt * 1000,
  });

  // -- On agent switch: hot cache hit → set cached value instantly; cold cache → reset to 0 --
  // All three token fields (usage / reasoning / max) move through the single
  // applyTokenUsage gate in one set(), so contextTokens and maxContextTokens can
  // never disagree across two renders. This is ungated (no isEventForThread) —
  // a local switch reset does not depend on activeThreadId having caught up,
  // removing the effect-ordering fragility of the old synthetic-event path.
  useEffect(() => {
    if (agentId == null) {
      applyTokenUsage(0, 0, 0, 0, 0);
      return;
    }
    seenParseErrors.current.clear();

    const cached = queryClient.getQueryData<TokenUsageResponse>(["token-usage", agentId]);
    if (cached) {
      // Hot cache hit: use cached value directly; React Query refreshes in background
      applyTokenUsage(
        cached.input_tokens,
        cached.reasoning_tokens,
        cached.max_input_tokens,
        cached.soft_compact_tokens,
        cached.hard_compact_tokens,
      );
    } else {
      // Cold cache: reset to 0; React Query is fetching
      applyTokenUsage(0, 0, 0, 0, 0);
    }
  }, [agentId, queryClient, applyTokenUsage]);

  // -- Once React Query data arrives → set value (overrides cached value or 0) --
  useEffect(() => {
    if (tokenQuery.data && agentId != null) {
      applyTokenUsage(
        tokenQuery.data.input_tokens,
        tokenQuery.data.reasoning_tokens,
        tokenQuery.data.max_input_tokens,
        tokenQuery.data.soft_compact_tokens,
        tokenQuery.data.hard_compact_tokens,
      );
    }
  }, [tokenQuery.data, agentId, applyTokenUsage]);

  // SSE real-time token_usage event → overrides current value
  const onEvent = useCallback(
    (ev: SystemEvent) => {
      if (ev.role === "token_usage") {
        processSseEvent(ev);
      }
    },
    [processSseEvent],
  );

  // Don't report closed/reconnecting (useTimeline's banner already
  // expresses it on the same endpoint; avoid double banners / toasts).
  // Only report parse-failed — otherwise schema drift on token_usage
  // would silently drop in the frontend, reproducing silent-failure-
  // hunter F7.
  const onConnectionEvent = useCallback(
    (ev: ConnectionEvent) => {
      switch (ev.type) {
        case "poll":
          if (agentId != null) {
            void queryClient.invalidateQueries({ queryKey: ["token-usage", agentId] });
          }
          return;
        case "parse-failed": {
          const key = String(ev.error);
          if (seenParseErrors.current.has(key)) return;
          seenParseErrors.current.add(key);
          showError(`Token usage SSE parse failed: ${key}`);
          return;
        }
        case "open":
        case "reconnecting":
        case "closed":
          return;
      }
    },
    [agentId, queryClient, showError],
  );

  useAgentEventStream(onEvent, onConnectionEvent);

  return { contextTokens: tokenUsage, maxContextTokens, softCompactTokens, hardCompactTokens };
}
