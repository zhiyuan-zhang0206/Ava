// Reply suggestions for the selected agent: drafted replies shown as grey text in the
// empty composer.
//
// The hook is the only writer of the candidates. It asks the gateway once per agent
// turn: when the agent is idle, its last visible timeline item is its own message, and
// nothing new has started for REPLY_SUGGESTION_DELAY_MS. Anything that makes the turn
// stale -- a new turn (busy again, or a new last item), another agent, a hidden page, the
// setting turned off, unmount -- aborts the request and hides the suggestion; the gateway
// stops its model calls when the request is aborted.
//
// Opening an agent that is already idle with the agent's message last takes the same
// path, so "I came back to this conversation" gets a suggestion too. A turn is requested
// at most once: a settled request (answer, empty answer or failure) is remembered by
// "agent + last item", an aborted one is not, so it can run again when the conditions
// return.

"use client";

import { useEffect, useRef, useState } from "react";

import type { BackendTimelineItem, PublicAgentStatus } from "../contracts/types";
import { errMsg } from "../contracts/errors";
import { useDocumentVisible } from "../layout/use-document-visible";
import { api } from "../transport/api";

/** Quiet period after the agent goes idle before a suggestion is requested. */
export const REPLY_SUGGESTION_DELAY_MS = 1000;

interface Params {
  agentId: number | null;
  status: PublicAgentStatus | undefined;
  /** The agent is mid-turn (the page's steady busy signal). */
  busy: boolean;
  items: readonly BackendTimelineItem[];
  /** The user setting is loaded and on. */
  enabled: boolean;
}

/** The last item that is not a framework marker: markers trail a finished turn. */
function lastConversationItem(items: readonly BackendTimelineItem[]): BackendTimelineItem | null {
  for (let i = items.length - 1; i >= 0; i--) {
    if (items[i].kind !== "system_marker") return items[i];
  }
  return null;
}

export function useReplySuggestion({ agentId, status, busy, items, enabled }: Params): readonly string[] | null {
  const visible = useDocumentVisible();
  const requested = useRef(new Set<string>());
  const [answer, setAnswer] = useState<{ turnKey: string; texts: readonly string[] } | null>(null);

  const last = lastConversationItem(items);
  const turnKey =
    agentId !== null && last !== null && last.kind === "agent_chat" && !last.partial
      ? `${agentId}:${last.item_id}`
      : null;
  const eligible = enabled && visible && !busy && status === "idling" && turnKey !== null;

  useEffect(() => {
    // `eligible` already implies a turn key (TypeScript narrows through the alias).
    if (!eligible || agentId === null) return;
    const seen = requested.current;
    if (seen.has(turnKey)) return;
    const controller = new AbortController();
    let settled = false;
    const timer = setTimeout(() => {
      seen.add(turnKey);
      api
        .getReplySuggestion(agentId, controller.signal)
        .then((response) => {
          // An answer that raced its own abort belongs to a turn that is gone.
          if (controller.signal.aborted) return;
          settled = true;
          if (response.suggestions.length > 0) setAnswer({ turnKey, texts: response.suggestions });
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted) return;
          settled = true;
          console.warn(`[reply-suggestion] request failed: ${errMsg(error)}`);
        });
    }, REPLY_SUGGESTION_DELAY_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
      if (!settled) seen.delete(turnKey);
    };
  }, [eligible, agentId, turnKey]);

  return eligible && answer !== null && answer.turnKey === turnKey ? answer.texts : null;
}
