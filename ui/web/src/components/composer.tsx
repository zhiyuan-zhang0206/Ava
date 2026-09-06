"use client";

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ContextButton } from "@/components/context-breakdown";
import {
  parseSlash,
  SlashAutocomplete,
  type SlashAutocompleteHandle,
} from "@/components/slash-autocomplete";
import { SendButton } from "@/components/ui/send-button";
import { Textarea } from "@/components/ui/textarea";
import { api, MessageDeliveryUnknownError } from "@/lib/api";
import { errMsg } from "@/lib/errors";
import { clearMessageSent, markMessageSent } from "@/lib/interaction-timing";
import { track } from "@/lib/telemetry";
import type { CommandItem } from "@/lib/types";
import { cn } from "@/lib/utils";
import { FLEX, FLEX_1, FLEX_COL, MIN_W_0 } from "@/lib/layout";

/** Composer tri-state: disabled (no active agent), idle (ready to send),
 *  busy (agent running — empty composer's button becomes Stop). "busy"
 *  covers the whole running state, not only mid-action, so Stop stays
 *  available between actions (the durable cancel is caught at claim).
 *  Terminated agents are now idle (send_message auto-resurrects them).
 *  Derived in page.tsx; TS exhaustiveness will flag when a new agent
 *  status needs to be wired in. */
export type ComposerMode = "disabled" | "idle" | "busy";

interface Props {
  mode: ComposerMode;
  children?: React.ReactNode;
  /** Right-edge Details selector on the composer's top row. */
  details?: React.ReactNode;
  /** Returns true on success — composer clears the input + attachments then;
   *  on false the user's text and images are preserved to avoid loss.
   *  `imageUrls` are the reference urls of attached images (empty for a
   *  plain-text send). */
  onSend: (content: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>;
  onStop: () => void;
  // Non-image files dragged onto / pasted into the composer are uploaded
  // through this (same path as the paperclip button — a text notification to
  // the agent). Undefined when no agent is active.
  onUploadFiles?: (files: File[]) => void;
  // Pasted / dropped images go through this instead: it uploads one image
  // silently and resolves to its reference url, which the composer attaches to
  // the next message as native model content. Undefined when no agent is active.
  onAttachImage?: (file: File) => Promise<string>;
  // File delivery runs independently of message submission. This state only
  // makes that parallel delivery visible; it must never gate Enter or Send.
  filesUploading?: boolean;
  // Monotonically increasing token — every value change steals focus
  // back to the textarea. Callers (page.tsx) increment this at moments
  // that demand "be able to type immediately" (spawn, switch thread, etc.).
  focusToken?: number;
  // context-window input_tokens — fed by useTokenUsage; 0 = the thread
  // hasn't done an LLM call yet (newly spawned / just switched to),
  // so the number is hidden
  contextTokens: number;
  // model context window ceiling; 0 = unknown — the "/max" segment
  // is hidden when 0
  maxContextTokens?: number;
  // per-agent wind-down (soft) + force-compact (hard) thresholds, each a
  // fraction of the model window; 0 = unknown. Shown as marks on the gauge and
  // as numbers beside the readout.
  softCompactTokens?: number;
  hardCompactTokens?: number;
  // the active agent id — makes the context readout a button that expands the
  // breakdown panel. null = no active agent (readout is non-interactive).
  agentId?: number | null;
  /** Whether the active agent is terminated. Sending resurrects it, so the
   *  textarea explains that behavior in its placeholder. */
  agentTerminated?: boolean;
  // CSS max-width for the composer root, from the timeline-width user setting
  // (lib/timeline-width.ts timelineMaxWidthCss) — aligned with the timeline
  // column (#723-⑧). The home page passes the viewport-ratio-derived cap on
  // desktop; undefined/absent = full-width — the narrow-viewport (mobile)
  // case (#805): no inline maxWidth is rendered and the composer fills its
  // column. One falsy convention shared with TimelineView/HeaderBar/
  // PendingStrip: undefined, never "" (audit C2).
  maxWidthCss?: string;
}

// One image attached to the composer, before / after its silent upload.
// `previewUrl` is a local object URL for the thumbnail; `url` is the gateway
// reference url, present once the upload resolves.
interface PendingImage {
  key: string;
  name: string;
  previewUrl: string;
  url?: string;
  status: "uploading" | "ready" | "error";
}


/** sessionStorage key for per-agent draft persistence across agent switches. */
const draftKey = (id: number) => `composer-draft-${id}`;
const sendAttemptKey = (id: number) => `composer-send-attempt-${id}`;

interface SendAttempt {
  signature: string;
  clientMessageId: string;
  agentId: number | null;
  content: string;
  imageUrls: string[];
  uncertain?: boolean;
}

function isAttempt(attempt: SendAttempt | null, clientMessageId: string): boolean {
  return attempt?.clientMessageId === clientMessageId;
}

// sessionStorage is optional browser infrastructure: privacy policies and
// quota failures can throw from any operation. Keep an in-memory mirror so
// persistence degradation never blocks a send or leaves its spinner latched.
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
    memorySession.delete(key);
  } catch {
    // The in-memory mirror preserves this tab's retry identity.
  }
}

function removeSession(key: string): void {
  memorySession.set(key, null);
  try {
    sessionStorage.removeItem(key);
    memorySession.delete(key);
  } catch {
    // The tombstone is newer than any stale persisted value removeItem left.
  }
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

function newClientMessageId(): string {
  const browserCrypto = crypto as unknown as {
    randomUUID?: () => string;
    getRandomValues<T extends ArrayBufferView>(array: T): T;
  };
  if (browserCrypto.randomUUID) return browserCrypto.randomUUID();
  // randomUUID is secure-context-only in some private-HTTP browsers;
  // getRandomValues remains available and gives the same collision posture.
  const bytes = browserCrypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function Composer({ mode, onSend, onStop, onUploadFiles, onAttachImage, filesUploading = false, focusToken, contextTokens, maxContextTokens = 0, softCompactTokens = 0, hardCompactTokens = 0, agentId = null, agentTerminated = false, maxWidthCss, children, details }: Props) {
  const t = useTranslations("common");
  const prevAgentIdRef = useRef(agentId);
  const [value, setValue] = useState(() => {
    if (agentId == null) return '';
    return readSession(draftKey(agentId)) ?? '';
  });
  // Caret offset in `value`. The slash dropdown and the instruction hint are
  // both scoped to the token / command segment the caret is in, so the caret is
  // state here, not just a DOM detail: every move has to re-render them. Kept
  // in sync by syncCaret (below) and by the writes that replace `value`
  // wholesale (draft restore, send, command selection).
  const [caret, setCaret] = useState(() => value.length);
  const [dragOver, setDragOver] = useState(false);
  const [images, setImages] = useState<PendingImage[]>([]);
  // sending: the window from submit() firing onSend until it resolves
  // (~50-200ms on localhost, possibly seconds on a flaky network).
  // turnActive only flips to busy once SSE inbound_arrived arrives
  // (~100ms RTT); in the meantime the button is still idle/send, so the
  // user can keep clicking send → keep the button disabled + spinner
  // during sending to physically block repeats.
  const [sending, setSending] = useState(false);
  const sendingRef = useRef(false);
  const [initialSendAttempt] = useState<SendAttempt | null>(() =>
    agentId == null ? null : readSendAttempt(agentId),
  );
  const sendAttemptRef = useRef<SendAttempt | null>(initialSendAttempt);
  const [uncertainAttempt, setUncertainAttempt] = useState<SendAttempt | null>(
    initialSendAttempt?.uncertain ? initialSendAttempt : null,
  );
  const activeUncertain = uncertainAttempt?.agentId === agentId;
  const composerLocked = sending || activeUncertain;
  // The command list is owned here (not in the dropdown) so it doubles as the
  // lookup for the committed command's instruction hint, shown once the name is
  // followed by whitespace and the dropdown has closed.
  const commandsCacheRef = useRef(new Map<number | null, CommandItem[]>());
  const [commands, setCommands] = useState<CommandItem[]>([]);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const acRef = useRef<SlashAutocompleteHandle>(null);
  // Single popover owner: the composer has two upward popovers over the same
  // spot (the context-breakdown panel + the slash dropdown); this state plus
  // the two handlers below keep them mutually exclusive — opening one closes
  // the other, so the z-50 layers can never stack.
  const [breakdownOpen, setBreakdownOpen] = useState(false);
  const handleBreakdownOpenChange = useCallback((open: boolean) => {
    setBreakdownOpen(open);
    if (open) acRef.current?.close();
  }, []);
  const handleAutocompleteOpenChange = useCallback((open: boolean) => {
    if (open) setBreakdownOpen(false);
  }, []);
  // Set by runCommand: the offset the next value commit puts the caret at —
  // just past the freshly-inserted `/<name> `, where the instruction goes.
  const pendingCaret = useRef<number | null>(null);
  const syncCaret = (ta: HTMLTextAreaElement) => setCaret(ta.selectionStart);

  const query = parseSlash(value, caret)?.query ?? null;

  // Whose instruction_hint the meta row shows while the instruction is being
  // typed. The caret picks it: the hint belongs to the command segment being
  // edited — the nearest `/name` token before the caret — so in a
  // multi-command message it follows the caret instead of being pinned to the
  // first command. Two edges: inside a name the dropdown is up and renders its
  // own copy, so the row stays quiet; and with the caret ahead of every command
  // (leading prose) the first hinted command stands in, since that is the one
  // the message opens with. Covers dropdown-selected and hand-typed names
  // alike — it is derived from the value, not from what was selected.
  const commandHint = useMemo(() => {
    const hintOf = (token: string) => {
      const hint = commands.find((c) => c.name === token.slice(1))?.instruction_hint;
      if (!hint) return null; // unknown command, or a command with no hint
      return hint;
    };
    if (parseSlash(value, caret)) return null;
    // #836: the hint follows the same first-token rule as the dropdown — only
    // a leading command (first whitespace-delimited run, starting with /)
    // shows its hint; a mid-message slash never does.
    const firstToken = /\S+/.exec(value);
    if (firstToken && firstToken[0].startsWith("/") && firstToken.index < caret) {
      return hintOf(firstToken[0]);
    }
    return null;
  }, [value, caret, commands]);

  // The selected agent owns the command catalog. Cache successful lists for
  // quick agent switching; a failed request stays a miss so returning retries.
  useEffect(() => {
    const cacheKey = agentId ?? null;
    const cachedCommands = commandsCacheRef.current.get(cacheKey);
    if (cachedCommands !== undefined) {
      setCommands(cachedCommands);
      return;
    }

    // Do not show the previous agent's commands while this catalog loads.
    setCommands([]);
    let alive = true;
    const request = agentId == null ? api.getCommands() : api.getCommands(agentId);
    request
      .then((c) => {
        commandsCacheRef.current.set(cacheKey, c);
        if (alive) setCommands(c);
      })
      .catch((e: unknown) => {
        // A missing list just means no autocomplete — don't surface it in the
        // composer. Log so a real backend failure (500 / shape change) isn't
        // indistinguishable from "gateway down".
        console.warn(`[composer] getCommands failed: ${errMsg(e)}`);
      });
    return () => {
      alive = false;
    };
  }, [agentId]);

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
    // `caret` is a dependency too: picking a command whose text is already
    // exactly right ("/recap " with the caret still in the name) changes only
    // the caret, and that commit still has to reach the DOM.
  }, [value, caret]);

  // Auto-grow relies on the textarea base component's
  // `field-sizing-content` (CSS) — Chrome 123+ / Safari 18.4+ /
  // Firefox 137+ — no more JS scrollHeight fallback. max-h-[50vh] cap,
  // overflow-y-auto for the inner scrollbar.

  // Revoke every attached image's object URL and drop them. Called on a clean
  // send and on unmount so the blobs don't leak.
  const clearImages = () => {
    setImages((prev) => {
      for (const img of prev) URL.revokeObjectURL(img.previewUrl);
      return [];
    });
  };
  useEffect(() => {
    // Unmount cleanup only — the callback closes over the latest images via the
    // functional updater, so no dependency on `images` is needed.
    return () => {
      setImages((prev) => {
        for (const img of prev) URL.revokeObjectURL(img.previewUrl);
        return prev;
      });
    };
  }, []);

  // Persist draft in sessionStorage so text survives agent switches (sidebar
  // agent change) and page refresh. Keyed by agentId.
  useEffect(() => {
    const prevId = prevAgentIdRef.current;
    // Save the current draft for the previous agent before switching.
    if (prevId != null && prevId !== agentId) {
      const trimmed = value.trim();
      if (trimmed) {
        writeSession(draftKey(prevId), value);
      } else {
        removeSession(draftKey(prevId));
      }
    }
    // Load the draft for the newly-selected agent.
    if (agentId != null && agentId !== prevId) {
      const draft = readSession(draftKey(agentId)) ?? "";
      setValue(draft);
      setCaret(draft.length); // resume at the end, like the browser does
      const attempt = readSendAttempt(agentId);
      sendAttemptRef.current = attempt;
      setUncertainAttempt(attempt?.uncertain ? attempt : null);
    }
    prevAgentIdRef.current = agentId;
    // `value` is intentionally omitted — we only want to react to agentId
    // changes, not to every keystroke. The closure captures the value at the
    // moment of the switch, which is the draft we want to save.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

  // Sync the current draft to sessionStorage as the user types.
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

  // Attach each image: show a thumbnail immediately, upload silently, then
  // stamp the resolved reference url (or mark the upload failed).
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
    forcedAttempt?: SendAttempt,
  ) => {
    if (sendingRef.current) return; // synchronous physical dedup against repeated dispatch
    sendingRef.current = true;
    setSending(true);
    const sendingAgentId = agentId;
    let activeAttempt: SendAttempt | null = forcedAttempt ?? null;
    let retryingUncertain = forcedAttempt?.uncertain === true;
    try {
      const signature = JSON.stringify([sendingAgentId, content, imageUrls]);
      let candidate = activeAttempt ?? sendAttemptRef.current;
      if (candidate?.signature !== signature && sendingAgentId != null) {
        const persisted = readSendAttempt(sendingAgentId);
        candidate = persisted?.signature === signature ? persisted : null;
      }
      retryingUncertain ||= candidate?.uncertain === true;
      const attempt: SendAttempt =
        candidate?.signature === signature
          ? { ...candidate, uncertain: false }
          : {
              signature,
              clientMessageId: newClientMessageId(),
              agentId: sendingAgentId,
              content,
              imageUrls: [...imageUrls],
            };
      activeAttempt = attempt;
      sendAttemptRef.current = attempt;
      setUncertainAttempt(null);
      if (sendingAgentId != null) {
        writeSession(sendAttemptKey(sendingAgentId), JSON.stringify(attempt));
        markMessageSent(sendingAgentId);
      }
      const ok = await onSend(content, imageUrls, attempt.clientMessageId);
      // Only clear on success — keep the user's text + images on failure
      // (a network error isn't the user's fault)
      if (ok) {
        track("composer-send");
        // A request for A can finish after the user switched to B. Clear only
        // the still-visible snapshot belonging to A, never B's current draft.
        if (prevAgentIdRef.current === sendingAgentId) {
          setValue((current) => (current.trim() === content ? "" : current));
          setCaret(0);
          clearImages();
        }
        // Clear the persisted draft so a future switch back doesn't restore
        // already-sent text.
        if (sendingAgentId != null) {
          removeSession(draftKey(sendingAgentId));
          removeSession(sendAttemptKey(sendingAgentId));
        }
        if (isAttempt(sendAttemptRef.current, attempt.clientMessageId)) {
          sendAttemptRef.current = null;
          setUncertainAttempt(null);
        }
      } else if (retryingUncertain) {
        const uncertain = { ...attempt, uncertain: true };
        sendAttemptRef.current = uncertain;
        if (uncertain.agentId != null) {
          writeSession(sendAttemptKey(uncertain.agentId), JSON.stringify(uncertain));
        }
        setUncertainAttempt(uncertain);
      } else if (sendingAgentId != null) {
        clearMessageSent(sendingAgentId);
      }
    } catch (error: unknown) {
      if (!(error instanceof MessageDeliveryUnknownError)) {
        if (activeAttempt?.agentId != null) clearMessageSent(activeAttempt.agentId);
        throw error;
      }
      if (activeAttempt != null) {
        const uncertain = { ...activeAttempt, uncertain: true };
        sendAttemptRef.current = uncertain;
        if (uncertain.agentId != null) {
          writeSession(sendAttemptKey(uncertain.agentId), JSON.stringify(uncertain));
        }
        setUncertainAttempt(uncertain);
      }
    } finally {
      sendingRef.current = false;
      setSending(false);
    }
  };

  // Reference urls of the images whose upload has resolved (flatMap narrows out
  // the still-uploading / failed ones without a non-null assertion).
  const readyImageUrls = images.flatMap((i) => (i.status === "ready" && i.url ? [i.url] : []));
  const uploadingImages = images.some((i) => i.status === "uploading");

  const submit = () => {
    if (composerLocked) return;
    const content = value.trim();
    if (uploadingImages) return; // wait for in-flight uploads so none are dropped
    if (!content && readyImageUrls.length === 0) return;
    void send(content, readyImageUrls);
  };

  const retryUncertain = () => {
    if (!activeUncertain) return;
    void send(
      uncertainAttempt.content,
      uncertainAttempt.imageUrls,
      uncertainAttempt,
    );
  };

  const abandonUncertain = () => {
    if (!activeUncertain) return;
    if (uncertainAttempt.agentId != null) {
      removeSession(sendAttemptKey(uncertainAttempt.agentId));
      clearMessageSent(uncertainAttempt.agentId);
    }
    if (sendAttemptRef.current?.clientMessageId === uncertainAttempt.clientMessageId) {
      sendAttemptRef.current = null;
    }
    setUncertainAttempt(null);
  };

  // Picking a command does NOT send — every command takes a natural-language
  // instruction after its name, so selection just drops `/<name> ` into the
  // composer and waits for the user to type that instruction. Enter then sends
  // the raw `/<name> <instruction>` text; the agent's claim node expands it.
  //
  // Only the caret's own token is replaced, so completing the second command of
  // a message leaves the first one (and its instruction, and any text after)
  // exactly as typed. Exactly one space follows the name — reusing the one
  // already there rather than doubling it — and the caret lands past that
  // space: that is both where the instruction goes and what closes the dropdown
  // (the caret is no longer inside a `/token`).
  const runCommand = (cmd: CommandItem) => {
    if (composerLocked) return;
    const token = parseSlash(value, caret);
    if (!token) return; // unreachable: the dropdown is only open on a token
    const head = `${value.slice(0, token.start)}/${cmd.name}`;
    const tail = value.slice(token.end);
    const pos = head.length + 1;
    pendingCaret.current = pos;
    setCaret(pos);
    setValue(`${head}${/^\s/.test(tail) ? "" : " "}${tail}`);
  };

  // Drag-and-drop + paste. Images become native attachments on this message
  // (attachImages); any other file goes to onUploadFiles (a path notification,
  // same as the paperclip button). When no image handler is wired, images fall
  // back to the file-upload path. dragOver drives the drop-zone highlight; only
  // file drags (not text / selection drags) arm it.
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

  // Button action derived from (mode, draft): busy + empty → stop (cancel
  // current turn); other send-capable combinations → submit. Stop is
  // narrowed to "empty input + busy" because once the user starts typing,
  // the intent is clearly to send a new message, not stop the turn —
  // while busy, send goes through the inbound queue (backend INSERTs a
  // pending inbound, agent consumes it naturally after the current turn),
  // which doesn't affect stop semantics. Enter always calls submit,
  // decoupled from the button: keyboard misfires hitting stop are
  // costly. Enter on busy + empty is absorbed by submit's trim early-
  // return no-op (the button in the same state goes through the onStop
  // branch; this trim fallback only serves Enter).
  const trimmed = value.trim();
  // A message is sendable with text OR at least one uploaded image. Stop is
  // narrowed to the truly-empty composer (no text, no attachments).
  const hasContent = !!trimmed || readyImageUrls.length > 0;
  const showStop = mode === "busy" && !hasContent && !composerLocked;
  const buttonDisabled =
    composerLocked || mode === "disabled" || uploadingImages || (!showStop && !hasContent);

  return (
    // No <form>: browser extensions (iOS Quark etc.) inject attributes
    // when they see a form, triggering a Next.js 16 hydration mismatch
    // that falls the root back to not-found. Use div + button onClick +
    // textarea Enter dispatching submit manually.
    //
    // suppressHydrationWarning is on the textarea (not the outer div) —
    // what extensions really inject into is input/textarea, not a plain div.
    <div
      data-testid="composer"
      onDrop={handleDrop}
      onDragOver={handleDragOver}
      onDragLeave={() => setDragOver(false)}
      style={maxWidthCss ? { maxWidth: maxWidthCss } : undefined}
      className={cn(
        // Composer sits at the bottom of the capped content column, aligned
        // with the timeline width (task #723-⑧ dropped the #715 48px-narrower
        // offset). The cap comes from the display.timeline_width_ratio setting
        // via the maxWidthCss prop (inline maxWidth: min(<ratio>vw, 1280px));
        // an absent maxWidthCss renders no inline maxWidth, so on narrow
        // viewports (mobile, #805) the composer is full-width.
        // Task #835-⑥ (user ruling 2026-08-05): the top divider no longer
        // runs edge to edge — the root carries px-4 so the divider (on the
        // inner wrapper) aligns with the timeline's px-4 content; the
        // textarea/upload rows indent a further 2px (px-0.5) from it, and
        // vertical rhythm is even (py-2 + gap-2).
        "mx-auto w-full px-4 pt-2 pb-3 transition-colors",
        MIN_W_0,
        dragOver && "bg-primary/5 ring-1 ring-inset ring-primary/40",
      )}
    >
      <div className={cn("gap-2 border-t border-border pt-2", FLEX, FLEX_COL)}>
      {/* `relative` anchors ContextButton's breakdown panel (absolute
          bottom-full → it expands upward over the timeline, never covering the
          composer below) — same anchor contract as SlashAutocomplete's. */}
      <div className={cn("relative items-center gap-2 px-0.5 text-xs text-muted-foreground h-4 leading-4", FLEX, MIN_W_0)}>
        {/* Task #723-⑩: the upload button lives at the LEFT of the context
            bar (children slot), so the meta row has a single left-aligned
            cluster. */}
        {children}
        {/* While composing a command's instruction the hint takes the row;
            otherwise the context-token readout (invisible spacer at 0 prevents
            layout jump). */}
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
              {/* eslint-disable-next-line @next/next/no-img-element -- local object-url preview, not a static asset */}
              <img
                src={img.previewUrl}
                alt={img.name}
                className={cn(
                  "h-14 w-14 rounded border border-border object-cover",
                  img.status === "uploading" && "opacity-50",
                  img.status === "error" && "ring-1 ring-destructive",
                )}
              />
              {img.status !== "ready" ? (
                <span
                  className={cn(
                    "absolute inset-0 items-center justify-center text-2xs",
                    img.status === "error" ? "text-destructive" : "text-muted-foreground",
                    FLEX
                  )}
                >
                  {img.status === "error" ? "Failed" : "…"}
                </span>
              ) : null}
              <button
                type="button"
                aria-label={t("removeImage", { name: img.name })}
                onClick={() => removeImage(img.key)}
                disabled={composerLocked}
                className={cn("absolute -right-1.5 -top-1.5 h-4 w-4 items-center justify-center rounded-full bg-foreground/80 text-background text-2xs leading-none", FLEX)}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      ) : null}
      {activeUncertain ? (
        <div
          role="status"
          className={cn(
            "flex-wrap items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1.5 text-xs",
            FLEX,
          )}
        >
          <span>{t("deliveryUnconfirmedStatus")}</span>
          <button type="button" className="underline" onClick={retryUncertain}>
            {t("retrySameMessage")}
          </button>
          <button type="button" className="underline" onClick={abandonUncertain}>
            {t("sendAnotherAnyway")}
          </button>
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
          placeholder={agentTerminated ? t("composerPlaceholderResurrect") : t("composerPlaceholder")}
          value={value}
          readOnly={composerLocked}
          onChange={(e) => {
            if (composerLocked) return;
            setValue(e.target.value);
            syncCaret(e.target);
          }}
          // Every other way the caret moves without the text changing: arrows /
          // Home / End (keyUp), pointer placement (click), drag-select and the
          // browser's own selection changes (select).
          onKeyUp={(e) => syncCaret(e.currentTarget)}
          onClick={(e) => syncCaret(e.currentTarget)}
          onSelect={(e) => syncCaret(e.currentTarget)}
          onPaste={handlePaste}
          disabled={mode === "disabled"}
          focusVisible={false}
          className={cn("font-mono text-base resize-none max-h-[50vh] overflow-y-auto min-h-9 py-1.5 leading-5", FLEX_1)}
          suppressHydrationWarning
          onKeyDown={(e) => {
            // During IME composition (CJK input methods composing words),
            // Enter commits the candidate — don't treat as submit. Older
            // Safari needs keyCode === 229 as a fallback.
            const composing = e.nativeEvent.isComposing || e.key === "Process";
            // While the slash-command dropdown is open, arrows / tab / enter /
            // escape drive it instead of the textarea: arrows move the
            // highlight, Tab and Enter both pick the active command (fill
            // `/<name> ` and wait, not send), Esc dismisses. Enter only falls
            // through to submit when the dropdown consumed nothing.
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
                // An open dropdown means the caret is inside a `/token`, i.e.
                // still typing a name — so Enter picks the command. Once the
                // caret moves into the instruction the dropdown is closed and
                // Enter never reaches here; it sends.
                e.preventDefault();
                if (ac.selectActive()) return;
              }
            }
            if (e.key === "Enter" && !e.shiftKey && !composing) {
              e.preventDefault();
              // Enter always submits, never onStop (core of decoupling
              // it from the button). In disabled mode the textarea is
              // itself disabled so keydown can't reach here; on busy +
              // empty input, submit's trim early-return absorbs it as a no-op.
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
