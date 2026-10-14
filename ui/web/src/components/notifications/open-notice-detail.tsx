"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { ChatMarkdown } from "@/components/content/markdown";
import { Button } from "@/components/ui/button";
import { AutoExpandTextarea } from "@/components/ui/auto-expand-textarea";
import { SendButton } from "@/components/ui/send-button";
import { api } from "@/lib/transport/api";
import { newOperationKey } from "@/lib/transport/operation-key";
import { errMsg } from "@/lib/contracts/errors";
import { PRIORITY_BG } from "@/lib/notifications/notices";
import { formatAbsolute, formatRelative } from "@/lib/format/time";
import type { NoticeAnswer, OpenNotice, ResolveNoticeIn } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { FLEX, FLEX_1 } from "@/lib/layout/layout";

import { NoticeQuestionForm } from "./notice-question-form";

/**
 * The reply surface for a single open notice — its content plus the resolve
 * controls, branching on `require_response`:
 *   - structured question → numbered radio/checkbox or text form + explicit submit
 *   - legacy require_response → a reply box (Enter / send = "answer") + a "Dismiss" action
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
  const [structuredAnswer, setStructuredAnswer] = useState<NoticeAnswer | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const inFlight = useRef(false);
  const attempt = useRef<{ intent: string; key: string } | null>(null);

  // Focus the reply box on mount when asked. The parent keys this component by
  // notice id, so a new notice replacing a resolved one re-focuses too.
  useEffect(() => {
    if (autoFocus) textareaRef.current?.focus();
  }, [autoFocus]);

  // On success we do NOT clear `pending`: the resolve removes the open notice,
  // so the caller (via onResolved) refetches and this component unmounts — a
  // sticky disabled state avoids a flash of re-enabled controls before it goes.
  const run = async (body: ResolveNoticeIn, alreadyMsg: string, failVerb: string) => {
    if (pending || unavailable || inFlight.current) return;
    inFlight.current = true;
    setPending(true);
    setError(null);
    try {
      const intent = JSON.stringify({ agentId, noticeId: notice.id, body });
      if (attempt.current?.intent !== intent) {
        attempt.current = { intent, key: newOperationKey() };
      }
      // Retain this intent's key after failure so a manual retry recovers its
      // original reply. A changed action/body/target starts a distinct intent.
      await api.resolveNotice(agentId, notice.id, body, attempt.current.key);
      setReply("");
      onResolved?.();
    } catch (e: unknown) {
      const msg = errMsg(e);
      const conflict = msg.includes("409");
      setError(conflict ? alreadyMsg : t("failed", { verb: failVerb, msg }));
      if (conflict && notice.question) setUnavailable(true);
      setPending(false);
    } finally {
      inFlight.current = false;
    }
  };

  const answer = () => {
    if (notice.question) {
      if (structuredAnswer) void run({ action: "answer", answer: structuredAnswer }, t("noLongerAvailable"), t("submitAnswer"));
      return;
    }
    const text = reply.trim();
    if (!text) return;
    void run({ action: "answer", reply: text }, t("alreadyAnswered"), t("send"));
  };
  const dismiss = () =>
    void run({ action: "dismiss" }, t(notice.question ? "noLongerAvailable" : "alreadyDismissed"), t("dismiss"));
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
      <div className="space-y-3 pb-4">
        <div className={cn("flex-wrap items-center gap-1.5", FLEX)}>
          <span
            className={cn(
              "shrink-0 rounded px-1 text-[10px] font-semibold text-white",
              PRIORITY_BG[notice.priority],
            )}
          >
            {notice.priority}
          </span>
          {notice.blocking && (
            <span className="shrink-0 rounded bg-destructive/10 px-1 text-[9px] font-medium tracking-wide text-destructive uppercase">
              {t("blocking")}
            </span>
          )}
          <span className="text-[10px] text-muted-foreground">
            {requiresResponse ? t("needsAnswer") : "FYI"}
          </span>
          {showTimestamp ? (
            <>
              <span aria-hidden className="text-[10px] text-muted-foreground">·</span>
              <span className="text-[10px] text-muted-foreground">
                {formatRelative(notice.created_at)}, {formatAbsolute(notice.created_at)}
              </span>
            </>
          ) : null}
          {notice.expire_at ? (
            <>
              <span aria-hidden className="text-[10px] text-muted-foreground">·</span>
              <span className="text-[10px] text-muted-foreground">
                {t("expires", { time: formatRelative(notice.expire_at) })}
              </span>
            </>
          ) : null}
        </div>
        <h4 className="text-base font-medium break-words">{notice.title}</h4>
        {notice.content && (
          <div className="text-sm break-words text-foreground/85">
            <ChatMarkdown content={notice.content} />
          </div>
        )}
      </div>

      {notice.question ? (
        <>
          <NoticeQuestionForm question={notice.question} pending={pending || unavailable} focusAnswer={autoFocus} onAnswerChange={setStructuredAnswer} />
          <div className={cn("sticky bottom-0 flex-wrap items-center justify-between gap-3 border-t border-border bg-background py-3", FLEX)}>
            <Button variant="ghost" onClick={dismiss} disabled={pending || unavailable}>{t("closeQuestion")}</Button>
            <Button onClick={answer} disabled={pending || unavailable || !structuredAnswer}>{pending ? t("submitting") : t("submitAnswer")}</Button>
          </div>
        </>
      ) : (
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
            className={cn("min-h-[3.25rem] rounded border border-input bg-background px-2 py-1.5 font-mono text-xs focus:border-primary disabled:opacity-50", FLEX_1)}
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
      )}

      <div className={cn("min-h-4 items-center justify-between gap-2", FLEX)}>
        {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : <span />}
        {!notice.question && (requiresResponse ? (
          <button
            type="button"
            onClick={dismiss}
            disabled={pending}
            className="shrink-0 rounded border border-border px-2 py-0.5 text-[10px] text-muted-foreground hover:bg-sidebar-accent hover:text-foreground disabled:opacity-50"
          >
            {t("dismiss")}
          </button>
        ) : (
          <button
            type="button"
            onClick={markRead}
            disabled={pending}
            className="shrink-0 rounded border border-border px-2 py-0.5 text-[10px] text-muted-foreground hover:bg-sidebar-accent hover:text-foreground disabled:opacity-50"
          >
            {pending ? t("marking") : t("markRead")}
          </button>
        ))}
      </div>
    </div>
  );
}
