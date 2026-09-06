"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { ChatMarkdown } from "@/components/markdown";
import { AutoExpandTextarea } from "@/components/ui/auto-expand-textarea";
import { SendButton } from "@/components/ui/send-button";
import { api } from "@/lib/api";
import { errMsg } from "@/lib/errors";
import { PRIORITY_BG } from "@/lib/notices";
import { formatAbsolute, formatRelative } from "@/lib/time";
import type { OpenNotice, ResolveNoticeIn } from "@/lib/types";
import { cn } from "@/lib/utils";
import { FLEX, FLEX_1 } from "@/lib/layout";

/**
 * The reply surface for a single open notice — its content plus the resolve
 * controls, branching on `require_response`:
 *   - require_response → a reply box (Enter / send = "answer") + a "Dismiss" action
 *   - FYI              → an optional note + a "Mark read" action
 * Owns the reply/pending/error state and calls `api.resolveNotice`; on success
 * it clears the box and calls `onResolved` so the caller can refetch (inspector)
 * or advance a queue. It never wakes the agent itself — that is the gateway's
 * job when the reply lands.
 *
 * Shared component: the inspector panel renders it at the bottom of its section list; the
 * fleet inbox renders it in its detail pane. The inbox drives keyboard cycling
 * (Up/Down over the open list when the reply box is empty) through `onCycle`, and
 * auto-advance-after-resolve through `onResolved`. The inspector passes neither
 * cycling callback, so those keys fall through unchanged there. Created time is
 * opt-in because the inbox already renders it in its agent context header.
 */
export function OpenNoticeDetail({
  agentId,
  notice,
  onResolved,
  onCycle,
  autoFocus = false,
  showTimestamp = false,
  className,
}: {
  agentId: number;
  notice: OpenNotice;
  onResolved?: () => void;
  onCycle?: (direction: -1 | 1) => void;
  autoFocus?: boolean;
  showTimestamp?: boolean;
  className?: string;
}) {
  const t = useTranslations("noticeDetail");
  const requiresResponse = notice.require_response;
  const [reply, setReply] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Focus the reply box on mount when asked. The parent keys this component by
  // notice id, so a new notice replacing a resolved one re-focuses too.
  useEffect(() => {
    if (autoFocus) textareaRef.current?.focus();
  }, [autoFocus]);

  // On success we do NOT clear `pending`: the resolve removes the open notice,
  // so the caller (via onResolved) refetches and this component unmounts — a
  // sticky disabled state avoids a flash of re-enabled controls before it goes.
  const run = async (body: ResolveNoticeIn, alreadyMsg: string, failVerb: string) => {
    if (pending) return;
    setPending(true);
    setError(null);
    try {
      await api.resolveNotice(agentId, notice.id, body);
      setReply("");
      onResolved?.();
    } catch (e: unknown) {
      const msg = errMsg(e);
      setError(msg.includes("409") ? alreadyMsg : t("failed", { verb: failVerb, msg }));
      setPending(false);
    }
  };

  const answer = () => {
    const text = reply.trim();
    if (!text) return;
    void run({ action: "answer", reply: text }, t("alreadyAnswered"), t("send"));
  };
  const dismiss = () =>
    void run({ action: "dismiss" }, t("alreadyDismissed"), t("dismiss"));
  const markRead = () => {
    const text = reply.trim();
    void run(
      { action: "read", ...(text ? { reply: text } : {}) },
      t("alreadyRead"),
      t("markRead"),
    );
  };

  const onSend = requiresResponse ? answer : markRead;

  return (
    <div className={cn("space-y-2", className)}>
      <div className="space-y-1.5 rounded bg-sidebar-accent/40 px-2 py-2">
        <div className={cn("flex-wrap items-center gap-1.5", FLEX)}>
          <span
            className={cn(
              "shrink-0 rounded px-1 font-mono text-2xs font-semibold text-white",
              PRIORITY_BG[notice.priority],
            )}
          >
            {notice.priority}
          </span>
          {notice.blocking && (
            <span className="shrink-0 rounded bg-destructive/10 px-1 text-2xs font-medium tracking-wide text-destructive uppercase">
              {t("blocking")}
            </span>
          )}
          <span className="text-2xs text-muted-foreground">
            {requiresResponse ? "Decision" : "FYI"}
          </span>
          {showTimestamp ? (
            <>
              <span aria-hidden className="text-2xs text-muted-foreground">·</span>
              <span className="text-2xs text-muted-foreground">
                {formatRelative(notice.created_at)}, {formatAbsolute(notice.created_at)}
              </span>
            </>
          ) : null}
        </div>
        <h4 className="text-xs font-medium break-words">{notice.title}</h4>
        {notice.content && (
          <div className="text-xs break-words text-muted-foreground">
            <ChatMarkdown content={notice.content} />
          </div>
        )}
      </div>

      <div className={cn("items-end gap-2", FLEX)}>
        <AutoExpandTextarea
          ref={textareaRef}
          value={reply}
          onChange={(e) => setReply(e.target.value)}
          onSend={onSend}
          onKeyDown={(e) => {
            // With an empty reply box, Up/Down cycles the caller's open list to
            // the previous/next notice (the inbox's arrow-key navigation). A
            // non-empty box owns those keys for cursor movement.
            if (onCycle && !reply.trim() && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
              e.preventDefault();
              onCycle(e.key === "ArrowUp" ? -1 : 1);
            }
          }}
          placeholder={
            requiresResponse
              ? t("answerPlaceholder")
              : t("notePlaceholder")
          }
          disabled={pending}
          aria-label={t("replyTo", { title: notice.title })}
          className={cn("min-h-[3.25rem] rounded border border-input bg-background px-2 py-1.5 font-sans text-xs focus:border-primary disabled:opacity-50", FLEX_1)}
        />
        {requiresResponse ? (
          <SendButton
            state={pending ? "sending" : "send"}
            onClick={answer}
            disabled={pending || !reply.trim()}
            aria-label={t("sendAnswer")}
          />
        ) : (
          <SendButton
            state={pending ? "sending" : "send"}
            onClick={markRead}
            disabled={pending || !reply.trim()}
            aria-label={t("sendNote")}
          />
        )}
      </div>

      <div className={cn("min-h-4 items-center justify-between gap-2", FLEX)}>
        {error ? <p className="text-2xs text-destructive">{error}</p> : <span />}
        {requiresResponse ? (
          <button
            type="button"
            onClick={dismiss}
            disabled={pending}
            className="shrink-0 rounded border border-border px-2 py-0.5 text-2xs text-muted-foreground hover:bg-sidebar-accent hover:text-foreground disabled:opacity-50"
          >
            Dismiss
          </button>
        ) : (
          <button
            type="button"
            onClick={markRead}
            disabled={pending}
            className="shrink-0 rounded border border-border px-2 py-0.5 text-2xs text-muted-foreground hover:bg-sidebar-accent hover:text-foreground disabled:opacity-50"
          >
            {pending ? t("marking") : t("markRead")}
          </button>
        )}
      </div>
    </div>
  );
}
