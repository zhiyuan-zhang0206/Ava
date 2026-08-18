"use client";

// Per-item body renderer (ItemView) — the content that sits below a card header.
// The card frame (colored left border, background, the icon + title header, the
// collapse chevron, the timestamp) is owned by MessageCard + CardHeader in
// `./card`; ItemView renders only the body for each kind, unpadded, so the card
// body wrapper controls spacing uniformly.
//
// -- Color mapping -- lives entirely in `./card`:messageCardConfig now. ItemView
// no longer carries any per-kind border / background; it just emits the inner
// content (markdown / code / envelope / marker payload).
//
// system_marker is the one split kind: the lifecycle / memory / note families
// render their payload body here (their icon + title + color come from the card),
// while the ephemeral families (compact_done / cancelled / error / unrecognized)
// have no card and render bare through EphemeralSystemMarker.

import { useTranslations } from "next-intl";
import { memo } from "react";

import { CopyButton } from "@/components/copy-button";
import { ChatMarkdown } from "@/components/markdown";
import { PythonCode } from "@/components/python-code";
import { assetUrl } from "@/lib/api";
import type { BackendTimelineItem } from "@/lib/types";
import { SSE_EVENT_WINDOW_MS } from "@/lib/constants-generated";
import { useThrottledStreaming } from "@/lib/use-throttled-streaming";
import { cn } from "@/lib/utils";

import { EphemeralSystemMarker, MarkerBody, classifyMarker } from "./markers";
import { FLEX } from "@/lib/layout";

// Cap how often a streaming item re-parses its content (markdown / Prism).
// One re-parse per SSE event window (~25 FPS) reads as live while removing the
// per-chunk parse storm that pegs the mobile main thread. The window is the
// same constant the agent-side publisher coalesces into (generated from
// shared/live_events.py EVENT_COALESCE_MS). See use-throttled-streaming.ts.
const STREAM_PARSE_INTERVAL_MS = SSE_EVENT_WINDOW_MS;

// envelope wrap is always "<header>:\n\n<body>" (envelope.py wrap_inbound /
// _exec.py wrap_code_output both follow this). Split out the header as a metadata
// label and the body as the main content — preserve raw text, only layer
// visually. Empty header then entire payload is body.
function splitEnvelope(payload: string): { header: string; body: string } {
  const idx = payload.indexOf("\n\n");
  if (idx > 0 && idx < 200) {
    // Cap header at 200 chars so a long stdout first paragraph is not mistaken for the header
    return { header: payload.slice(0, idx), body: payload.slice(idx + 2) };
  }
  return { header: "", body: payload };
}

// Envelope content — dim single-line header + monospace body + optional image
// thumbnails. The card frame provides the colored border / background; this
// renders only the inner text, unpadded.
function EnvelopeContent({
  payload,
  images,
  showCopy = false,
}: {
  payload: string;
  // Image reference urls on a multimodal inbound — rendered as thumbnails below
  // the text. Gateway-relative, so each is resolved through assetUrl.
  images?: string[] | null;
  /** Show a copy button on the body pre block (for code_output). */
  showCopy?: boolean;
}) {
  const t = useTranslations("timeline");
  const { header, body } = splitEnvelope(payload);
  // An image-only message stores "[image]" as its text placeholder; suppress that
  // literal in the body when real thumbnails render below.
  const showBody = body && !(images?.length && body === "[image]");
  return (
    <>
      {header ? (
        <div className="mb-1.5">
          <span className="font-mono text-[13px] leading-relaxed text-foreground/90 [overflow-wrap:anywhere]">
            {header}
          </span>
        </div>
      ) : null}
      {showBody ? (
        showCopy ? (
          <div className="group relative">
            <CopyButton text={body} label={t("commandOutput")} />
            <pre className="whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-[13px] leading-relaxed text-foreground/90 m-0">
              {body}
            </pre>
          </div>
        ) : (
          <pre className="whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-[13px] leading-relaxed text-foreground/90 m-0">
            {body}
          </pre>
        )
      ) : null}
      {images?.length ? (
        <div className={cn("mt-1.5 flex-wrap gap-2", FLEX)}>
          {images.map((src) => (
            <a key={src} href={assetUrl(src)} target="_blank" rel="noreferrer">
              {/* eslint-disable-next-line @next/next/no-img-element -- user upload, not a static asset */}
              <img
                src={assetUrl(src)}
                alt={t("attachedImage")}
                crossOrigin="use-credentials"
                className="max-h-48 max-w-[16rem] rounded border border-border object-contain"
              />
            </a>
          ))}
        </div>
      ) : null}
    </>
  );
}

// Shown at the tail of partial + interrupted items to distinguish "the message
// simply ends here" vs "streaming interrupted; content may be incomplete".
// ConnectionNotice pops up at the same time, but the banner is in a separate UI
// region from timeline content; this inline hint makes the cause-and-effect visible.
function InterruptedNotice() {
  const t = useTranslations("timeline");
  return (
    <div className="mt-1 text-[11px] text-amber-700 dark:text-amber-400 font-mono select-none">
      {t("streamingInterrupted")}
    </div>
  );
}

export const ItemView = memo(function ItemView({
  item,
  streaming,
}: {
  item: BackendTimelineItem;
  streaming: boolean;
}) {
  const t = useTranslations("timeline");
  // Throttle the heavy-parse content (chat markdown / python highlight) while it
  // streams. Reasoning / output items render as plain <pre> with no parse, so
  // they bypass this. Hook is called unconditionally (rules of hooks); `live` is
  // false for every other kind, making it a no-op pass-through.
  const live =
    (item.kind === "agent_chat" && !!item.partial) ||
    (item.kind === "agent_code" && streaming);
  const streamingPayload = useThrottledStreaming(
    item.payload,
    live,
    STREAM_PARSE_INTERVAL_MS,
  );

  switch (item.kind) {
    case "inbound_chat":
    case "inbound_compact_summary":
    case "inbound_compact_request":
      return <EnvelopeContent payload={item.payload} images={item.images} />;

    case "agent_chat":
      return (
        <>
          <div
            className={cn(
              item.partial && "italic opacity-80",
              item.interrupted && "border-b border-dashed border-amber-500/60 pb-1",
            )}
          >
            {item.partial ? <span className="text-muted-foreground">… </span> : null}
            <ChatMarkdown content={streamingPayload} />
          </div>
          {item.interrupted ? <InterruptedNotice /> : null}
        </>
      );

    case "agent_code":
      return (
        <>
          <div
            className={cn(
              item.partial && "italic opacity-80",
              item.interrupted && "border-b border-dashed border-amber-500/60 pb-1",
            )}
          >
            <PythonCode code={streamingPayload} streaming={streaming} />
          </div>
          {item.interrupted ? <InterruptedNotice /> : null}
        </>
      );

    case "agent_reasoning":
      return (
        <>
          <pre
            className={cn(
              "whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-[12px] leading-relaxed text-muted-foreground/90 m-0",
              item.partial && "italic opacity-75",
              item.interrupted && "border-b border-dashed border-amber-500/60 pb-1",
            )}
          >
            {item.partial ? "… " : ""}
            {item.payload}
          </pre>
          {item.interrupted ? <InterruptedNotice /> : null}
        </>
      );

    case "code_output":
      return <EnvelopeContent payload={item.payload} showCopy />;

    case "system_prompt":
      // The agent's system prompt (state.messages[0]) — thousands of lines, so it
      // stays collapsed by default. Body-only monospace dump.
      return (
        <pre className="whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-[12px] leading-relaxed text-muted-foreground/90 m-0">
          {item.payload}
        </pre>
      );

    case "system_marker": {
      // Card marker families render their payload body here; the ephemeral
      // families have no card and render their own bare output.
      const cls = classifyMarker(item.source);
      if (cls.kind === "ephemeral") {
        return <EphemeralSystemMarker source={item.source} payload={item.payload} />;
      }
      return <MarkerBody payload={item.payload} />;
    }
  }

  // Exhaustiveness guard: TS narrows item to never at compile time; this is the
  // runtime fallback.
  /* v8 ignore next 3 */
  const _exhaustive: never = item.kind;
  console.warn("[timeline] ItemView: unknown item kind", _exhaustive);
  return (
    <div className="text-muted-foreground font-mono text-xs">{t("unknownEventType")}</div>
  );
},
// Custom comparator: only re-render when item reference or content changes.
// ItemView is a pure function (DOM derived from item); same item + same streaming
// -> same output, so re-renders can be skipped. This matters especially for
// code_delta streaming: previously committed items are unchanged, only the last
// streaming item payload is changing — every preceding item skips re-render.
(prev, next) =>
  prev.item === next.item && prev.streaming === next.streaming,
);
