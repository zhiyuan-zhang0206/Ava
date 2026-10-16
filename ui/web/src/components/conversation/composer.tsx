"use client";

import { newOperationKey as newClientMessageId } from "@/lib/transport/operation-key";

import { useQuery } from "@tanstack/react-query";
import Image from "next/image";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ContextButton } from "@/components/inspector/context-breakdown";
import { ContextMeterGhost } from "@/components/inspector/context-meter";
import {
  parseSlash,
  SlashAutocomplete,
  type SlashAutocompleteHandle,
} from "@/components/conversation/slash-autocomplete";
import { SendButton } from "@/components/ui/send-button";
import { Textarea } from "@/components/ui/textarea";
import { api, MessageDeliveryUnknownError } from "@/lib/transport/api";
import { errMsg } from "@/lib/contracts/errors";
import { clearMessageSent, markMessageSent } from "@/lib/format/interaction-timing";
import { track } from "@/lib/telemetry/telemetry";
import type { CommandItem } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { FLEX, FLEX_1, FLEX_COL, MIN_W_0 } from "@/lib/layout/layout";

export type ComposerMode = "disabled" | "idle" | "busy";

interface Props {
  mode: ComposerMode;
  children?: React.ReactNode;
  details?: React.ReactNode;
  onSend: (content: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>;
  onStop: () => void;
  onUploadFiles?: (files: File[]) => void;
  onAttachImage?: (file: File) => Promise<string>;
  filesUploading?: boolean;
  focusToken?: number;
  contextTokens: number;
  contextPending?: boolean;
  maxContextTokens?: number;
  softCompactTokens?: number;
  hardCompactTokens?: number;
  agentId?: number | null;
  agentTerminated?: boolean;
  maxWidthCss?: string;
  suggestions?: readonly string[] | null;
}

interface PendingImage {
  key: string;
  name: string;
  previewUrl: string;
  url?: string;
  status: "uploading" | "ready" | "error";
}

const draftKey = (id: number) => `composer-draft-${id}`;
const sendAttemptKey = (id: number) => `composer-send-attempt-${id}`;

interface SendAttempt {
  signature: string;
  clientMessageId: string;
  agentId: number | null;
  content: string;
  imageUrls: string[];
}

function isAttempt(attempt: SendAttempt | null, clientMessageId: string): boolean {
  return attempt?.clientMessageId === clientMessageId;
}

const memorySession = new Map<string, string | null>();

function readSession(key: string): string | null {
  if (memorySession.has(key)) return memorySession.get(key) ?? null;
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeSession(key: string, value: string): void {
  memorySession.set(key, value);
  try {
    sessionStorage.setItem(key, value);
  } catch {
    return;
  }
  memorySession.delete(key);
}

function removeSession(key: string): void {
  memorySession.set(key, null);
  try {
    sessionStorage.removeItem(key);
  } catch {
    return;
  }
  memorySession.delete(key);
}

function readSendAttempt(agentId: number): SendAttempt | null {
  try {
    const parsed = JSON.parse(readSession(sendAttemptKey(agentId)) ?? "null") as unknown;
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      !("signature" in parsed) ||
      typeof parsed.signature !== "string" ||
      !("clientMessageId" in parsed) ||
      typeof parsed.clientMessageId !== "string" ||
      !("content" in parsed) ||
      typeof parsed.content !== "string" ||
      !("imageUrls" in parsed) ||
      !Array.isArray(parsed.imageUrls) ||
      !parsed.imageUrls.every((url) => typeof url === "string")
    ) {
      return null;
    }
    return parsed as SendAttempt;
  } catch {
    return null;
  }
}

export function Composer({ mode, onSend, onStop, onUploadFiles, onAttachImage, filesUploading = false, focusToken, contextTokens, contextPending = false, maxContextTokens = 0, softCompactTokens = 0, hardCompactTokens = 0, agentId = null, agentTerminated = false, maxWidthCss, suggestions = null, children, details }: Props) {
  const t = useTranslations("common");
  const prevAgentIdRef = useRef(agentId);
  const [value, setValue] = useState(() => {
    if (agentId == null) return '';
    return readSession(draftKey(agentId)) ?? '';
  });
  const [replySelection, setReplySelection] = useState<{
    options: readonly string[];
    agentId: number | null;
    index: number;
    consumed: boolean;
  } | null>(null);
  const currentReplySelection =
    replySelection !== null && replySelection.options === suggestions && replySelection.agentId === agentId
      ? replySelection : null;
  const replyIndex = currentReplySelection?.index ?? 0;
  const [caret, setCaret] = useState(() => value.length);
  const [dragOver, setDragOver] = useState(false);
  const [images, setImages] = useState<PendingImage[]>([]);
  const [sending, setSending] = useState(false);
  const sendingRef = useRef(false);
  const [initialSendAttempt] = useState<SendAttempt | null>(() =>
    agentId == null ? null : readSendAttempt(agentId),
  );
  const sendAttemptRef = useRef<SendAttempt | null>(initialSendAttempt);
  const composerLocked = sending;
  const { data: commands = [] } = useQuery({
    queryKey: ["agent-commands", agentId],
    queryFn: async () => {
      try {
        return await (agentId == null ? api.getCommands() : api.getCommands(agentId));
      } catch (e) {
        console.warn(`[composer] getCommands failed: ${errMsg(e)}`);
        throw new Error("command catalog unavailable", { cause: e });
      }
    },
    staleTime: Infinity,
    retry: false,
  });
  const taRef = useRef<HTMLTextAreaElement>(null);
  const acRef = useRef<SlashAutocompleteHandle>(null);
  const [breakdownOpen, setBreakdownOpen] = useState(false);
  const handleBreakdownOpenChange = useCallback((open: boolean) => {
    setBreakdownOpen(open);
    if (open) acRef.current?.close();
  }, []);
  const handleAutocompleteOpenChange = useCallback((open: boolean) => {
    if (open) setBreakdownOpen(false);
  }, []);
  const pendingCaret = useRef<number | null>(null);
  const syncCaret = (ta: HTMLTextAreaElement) => setCaret(ta.selectionStart);

  const query = parseSlash(value, caret)?.query ?? null;

  const commandHint = useMemo(() => {
    const hintOf = (token: string) => {
      const hint = commands.find((c) => c.name === token.slice(1))?.instruction_hint;
      if (!hint) return null;
      return hint;
    };
    if (parseSlash(value, caret)) return null;
    const firstToken = /\S+/.exec(value);
    if (firstToken && firstToken[0].startsWith("/") && firstToken.index < caret) {
      return hintOf(firstToken[0]);
    }
    return null;
  }, [value, caret, commands]);

  useEffect(() => {
    if (focusToken === undefined) return;
    taRef.current?.focus();
  }, [focusToken]);

  useEffect(() => {
    const pos = pendingCaret.current;
    if (pos === null) return;
    pendingCaret.current = null;
    const ta = taRef.current;
    if (!ta) return;
    ta.focus();
    ta.setSelectionRange(pos, pos);
  }, [value, caret]);

  const clearImages = () => {
    setImages((prev) => {
      for (const img of prev) URL.revokeObjectURL(img.previewUrl);
      return [];
    });
  };
  useEffect(() => {
    return () => {
      setImages((prev) => {
        for (const img of prev) URL.revokeObjectURL(img.previewUrl);
        return prev;
      });
    };
  }, []);

  const valueRef = useRef(value);
  useEffect(() => {
    valueRef.current = value;
  }, [value]);
  useEffect(() => {
    const prevId = prevAgentIdRef.current;
    if (prevId != null && prevId !== agentId) {
      const snapshot = valueRef.current;
      const trimmed = snapshot.trim();
      if (trimmed) {
        writeSession(draftKey(prevId), snapshot);
      } else {
        removeSession(draftKey(prevId));
      }
    }
    if (agentId != null && agentId !== prevId) {
      const draft = readSession(draftKey(agentId)) ?? "";
      setValue(draft);
      setCaret(draft.length);
      const attempt = readSendAttempt(agentId);
      sendAttemptRef.current = attempt;
    }
    prevAgentIdRef.current = agentId;
  }, [agentId]);

  useEffect(() => {
    if (agentId == null) return;
    const trimmed = value.trim();
    if (trimmed) {
      writeSession(draftKey(agentId), value);
    } else {
      removeSession(draftKey(agentId));
    }
  }, [value, agentId]);

  const removeImage = (key: string) => {
    if (composerLocked) return;
    setImages((prev) => {
      const gone = prev.find((i) => i.key === key);
      if (gone) URL.revokeObjectURL(gone.previewUrl);
      return prev.filter((i) => i.key !== key);
    });
  };

  const attachImages = (files: File[]) => {
    if (composerLocked || !onAttachImage) return;
    for (const file of files) {
      const key = `${file.name}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      const previewUrl = URL.createObjectURL(file);
      setImages((prev) => [...prev, { key, name: file.name, previewUrl, status: "uploading" }]);
      onAttachImage(file)
        .then((u) =>
          setImages((prev) =>
            prev.map((i) => (i.key === key ? { ...i, url: u, status: "ready" as const } : i)),
          ),
        )
        .catch(() =>
          setImages((prev) =>
            prev.map((i) => (i.key === key ? { ...i, status: "error" as const } : i)),
          ),
        );
    }
  };

  const send = async (
    content: string,
    imageUrls: string[],
  ) => {
    if (sendingRef.current) return;
    sendingRef.current = true;
    setSending(true);
    const sendingAgentId = agentId;
    let activeAttempt: SendAttempt | null = null;
    try {
      const signature = JSON.stringify([sendingAgentId, content, imageUrls]);
      let candidate = sendAttemptRef.current;
      if (candidate?.signature !== signature && sendingAgentId != null) {
        const persisted = readSendAttempt(sendingAgentId);
        candidate = persisted?.signature === signature ? persisted : null;
      }
      const attempt: SendAttempt =
        candidate?.signature === signature
          ? candidate
          : {
              signature,
              clientMessageId: newClientMessageId(),
              agentId: sendingAgentId,
              content,
              imageUrls: [...imageUrls],
            };
      activeAttempt = attempt;
      sendAttemptRef.current = attempt;
      if (sendingAgentId != null) {
        writeSession(sendAttemptKey(sendingAgentId), JSON.stringify(attempt));
        markMessageSent(sendingAgentId);
      }
      const ok = await onSend(content, imageUrls, attempt.clientMessageId);
      if (ok) {
        track("composer-send");
        if (prevAgentIdRef.current === sendingAgentId) {
          if (suggestions) {
            setReplySelection({
              options: suggestions, agentId: sendingAgentId, index: replyIndex, consumed: true,
            });
          }
          setValue((current) => (current.trim() === content ? "" : current));
          setCaret(0);
          clearImages();
        }
        if (sendingAgentId != null) {
          removeSession(draftKey(sendingAgentId));
          removeSession(sendAttemptKey(sendingAgentId));
        }
        if (isAttempt(sendAttemptRef.current, attempt.clientMessageId)) {
          sendAttemptRef.current = null;
        }
      } else if (sendingAgentId != null) {
        clearMessageSent(sendingAgentId);
      }
    } catch (error: unknown) {
      if (!(error instanceof MessageDeliveryUnknownError)) {
        if (activeAttempt?.agentId != null) clearMessageSent(activeAttempt.agentId);
        throw error;
      }
    } finally {
      sendingRef.current = false;
      setSending(false);
    }
  };

  const readyImageUrls = images.flatMap((i) => (i.status === "ready" && i.url ? [i.url] : []));
  const uploadingImages = images.some((i) => i.status === "uploading");

  const submit = () => {
    if (composerLocked) return;
    const content = value.trim();
    if (uploadingImages) return;
    if (!content && readyImageUrls.length === 0) return;
    void send(content, readyImageUrls);
  };

  const runCommand = (cmd: CommandItem) => {
    if (composerLocked) return;
    const token = parseSlash(value, caret);
    if (!token) return;
    const head = `${value.slice(0, token.start)}/${cmd.name}`;
    const tail = value.slice(token.end);
    const pos = head.length + 1;
    pendingCaret.current = pos;
    setCaret(pos);
    setValue(`${head}${/^\s/.test(tail) ? "" : " "}${tail}`);
  };

  const offeredSuggestion =
    value === "" && !currentReplySelection?.consumed && suggestions?.length
      ? suggestions[replyIndex] : null;
  const suggestionPlaceholder =
    offeredSuggestion !== null && suggestions && suggestions.length > 1
      ? `[${replyIndex + 1}/${suggestions.length}] ${offeredSuggestion}` : offeredSuggestion;
  const acceptSuggestion = (text: string) => {
    pendingCaret.current = text.length;
    setCaret(text.length);
    setValue(text);
  };

  const routeFiles = (files: File[]) => {
    if (composerLocked) return;
    const imgs = files.filter((f) => f.type.startsWith("image/"));
    const others = files.filter((f) => !f.type.startsWith("image/"));
    if (imgs.length > 0) {
      if (onAttachImage) attachImages(imgs);
      else onUploadFiles?.(imgs);
    }
    if (others.length > 0) onUploadFiles?.(others);
  };
  const canReceiveFiles = !composerLocked && (!!onUploadFiles || !!onAttachImage);
  const handleDrop = (e: React.DragEvent) => {
    if (!canReceiveFiles) return;
    e.preventDefault();
    setDragOver(false);
    const files = Array.from(e.dataTransfer.files);
    if (files.length > 0) routeFiles(files);
  };
  const handleDragOver = (e: React.DragEvent) => {
    if (!canReceiveFiles || !e.dataTransfer.types.includes("Files")) return;
    e.preventDefault();
    setDragOver(true);
  };
  const handlePaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    if (!canReceiveFiles) return;
    const files = Array.from(e.clipboardData.files);
    if (files.length > 0) {
      e.preventDefault();
      routeFiles(files);
    }
  };

  const trimmed = value.trim();
  const hasContent = !!trimmed || readyImageUrls.length > 0;
  const showStop = mode === "busy" && !hasContent && !composerLocked;
  const buttonDisabled =
    composerLocked || mode === "disabled" || uploadingImages || (!showStop && !hasContent);

  return (
    <div
      data-testid="composer"
      onDrop={handleDrop}
      onDragOver={handleDragOver}
      onDragLeave={() => setDragOver(false)}
      style={maxWidthCss ? { maxWidth: maxWidthCss } : undefined}
      className={cn(
        "mx-auto w-full px-4 pt-2 pb-3 transition-colors",
        MIN_W_0,
        dragOver && "bg-primary/5 ring-1 ring-inset ring-primary/40",
      )}
    >
      <div className={cn("gap-2 border-t border-border pt-2", FLEX, FLEX_COL)}>
      <div className={cn("relative items-center gap-2 px-0.5 text-xs text-muted-foreground h-4 leading-4", FLEX, MIN_W_0)}>
        {children}
        <span data-testid="composer-meta" className={cn("truncate", commandHint && "italic", MIN_W_0)}>
          {commandHint ??
            (contextTokens > 0 ? (
              <ContextButton
                agentId={agentId}
                open={breakdownOpen}
                onOpenChange={handleBreakdownOpenChange}
                contextTokens={contextTokens}
                maxContextTokens={maxContextTokens}
                softCompactTokens={softCompactTokens}
                hardCompactTokens={hardCompactTokens}
              />
            ) : contextPending ? (
              <ContextMeterGhost />
            ) : (
              " "
            ))}
        </span>
        {details && (
          <span className={cn("relative ml-auto shrink-0 items-center gap-2", FLEX)}>{details}</span>
        )}
      </div>
      {filesUploading ? (
        <div
          role="status"
          data-testid="composer-upload-hint"
          className="px-0.5 text-xs leading-4 text-muted-foreground"
        >
          {t("filesUploadingHint")}
        </div>
      ) : null}
      {images.length > 0 ? (
        <div className={cn("flex-wrap gap-2 px-0.5 pb-1", FLEX)} data-testid="composer-attachments">
          {images.map((img) => (
            <div key={img.key} className="relative">
              <Image
                src={img.previewUrl}
                alt={img.name}
                width={56}
                height={56}
                className={cn(
                  "h-14 w-14 rounded border border-border object-cover",
                  img.status === "uploading" && "opacity-50",
                  img.status === "error" && "ring-1 ring-destructive",
                )}
              />
              {img.status !== "ready" ? (
                <span
                  className={cn(
                    "absolute inset-0 items-center justify-center text-[10px]",
                    img.status === "error" ? "text-destructive" : "text-muted-foreground",
                    FLEX
                  )}
                >
                  {img.status === "error" ? t("failed") : "…"}
                </span>
              ) : null}
              <button
                type="button"
                aria-label={t("removeImage", { name: img.name })}
                onClick={() => removeImage(img.key)}
                disabled={composerLocked}
                className={cn("absolute -right-1.5 -top-1.5 h-4 w-4 items-center justify-center rounded-full bg-foreground/80 text-background text-[10px] leading-none", FLEX)}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      ) : null}
      <div className="relative">
        <SlashAutocomplete
          ref={acRef}
          commands={commands}
          query={query}
          onSelect={runCommand}
          onOpenChange={handleAutocompleteOpenChange}
        />
        <div className={cn("gap-2 items-end", FLEX)}>
        <Textarea
          ref={taRef}
          data-testid="composer-input"
          aria-label={t("composerInput")}
          aria-description={offeredSuggestion !== null ? t("replySuggestionKeys") : undefined}
          placeholder={
            suggestionPlaceholder ??
            (agentTerminated ? t("composerPlaceholderResurrect") : t("composerPlaceholder"))
          }
          value={value}
          readOnly={composerLocked}
          onChange={(e) => {
            if (composerLocked) return;
            setValue(e.target.value);
            syncCaret(e.target);
          }}
          onKeyUp={(e) => syncCaret(e.currentTarget)}
          onClick={(e) => syncCaret(e.currentTarget)}
          onSelect={(e) => syncCaret(e.currentTarget)}
          onPaste={handlePaste}
          disabled={mode === "disabled"}
          focusVisible={false}
          className={cn("font-mono text-base resize-none max-h-[50vh] overflow-y-auto min-h-9 py-1.5 leading-5", FLEX_1)}
          suppressHydrationWarning
          onKeyDown={(e) => {
            const composing = e.nativeEvent.isComposing || e.key === "Process";
            const ac = acRef.current;
            if (ac?.isOpen() && !composing) {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                ac.moveDown();
                return;
              }
              if (e.key === "ArrowUp") {
                e.preventDefault();
                ac.moveUp();
                return;
              }
              if (e.key === "Escape") {
                e.preventDefault();
                ac.close();
                return;
              }
              if (e.key === "Tab" && !e.shiftKey) {
                e.preventDefault();
                ac.selectActive();
                return;
              }
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                if (ac.selectActive()) return;
              }
            }
            if (
              offeredSuggestion !== null &&
              !composing &&
              !composerLocked &&
              !ac?.isOpen() &&
              !e.shiftKey &&
              !e.ctrlKey &&
              !e.altKey &&
              !e.metaKey
            ) {
              if (e.key === "ArrowRight") {
                e.preventDefault();
                acceptSuggestion(offeredSuggestion);
                return;
              }
              if (
                suggestions && suggestions.length > 1 &&
                (e.key === "ArrowUp" || e.key === "ArrowDown")
              ) {
                e.preventDefault();
                const step = e.key === "ArrowDown" ? 1 : -1;
                setReplySelection({
                  options: suggestions, agentId,
                  index: Math.min(Math.max(replyIndex + step, 0), suggestions.length - 1),
                  consumed: false,
                });
                return;
              }
            }
            if (e.key === "Enter" && !e.shiftKey && !composing) {
              e.preventDefault();
              if (mode === "disabled") return;
              submit();
            }
          }}
        />
        <SendButton
          data-testid="composer-send"
          state={sending ? "sending" : showStop ? "stop" : "send"}
          aria-label={sending ? "Sending" : showStop ? "Stop current turn" : "Send message"}
          disabled={buttonDisabled}
          onClick={() => { if (showStop) { track("composer-stop"); onStop(); } else { submit(); } }}
        />
        </div>
      </div>
      </div>
    </div>
  );
}
