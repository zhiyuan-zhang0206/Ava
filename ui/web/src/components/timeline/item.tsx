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
import { Fragment, memo, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { CopyButton } from "@/components/copy-button";
import { ChatMarkdown } from "@/components/markdown";
import { PythonCode } from "@/components/python-code";
import { assetUrl } from "@/lib/api";
import type { BackendTimelineItem } from "@/lib/types";
import { SSE_EVENT_WINDOW_MS } from "@/lib/constants-generated";
import { useThrottledStreaming } from "@/lib/use-throttled-streaming";
import { useUserSettings } from "@/lib/use-user-settings";
import { cn } from "@/lib/utils";

import { EphemeralSystemMarker, MarkerBody, classifyMarker } from "./markers";
import { isLiveReasoning } from "./reasoning-clock";
import { FLEX } from "@/lib/layout";

// Cap how often a streaming item re-parses its content (markdown / Prism).
// One re-parse per SSE event window (~25 FPS) reads as live while removing the
// per-chunk parse storm that pegs the mobile main thread. The window is the
// same constant the agent-side publisher coalesces into (generated from
// shared/live_events.py EVENT_COALESCE_MS). See use-throttled-streaming.ts.
//
// A FIXED window is not enough: the parse is O(payload length), so as a code
// block streams, each flush re-highlights the whole accumulated text — total
// work is quadratic and the main thread saturates (user report 2026-09-06; bench: a 33-line
// block ≈ 69ms/highlight ≈ 1.7s of main-thread work per streaming second at
// 25Hz, a 300-line block ≈ 11.8s/s). The window therefore widens linearly with
// payload length — one extra SSE window per 2400 bytes, capped at 1s — so a
// 300-line block re-parses ~1x/s while chat/reasoning keep the live 40ms
// cadence. The settle path (use-throttled-streaming.ts, !live) still renders
// the final text in full immediately.
const STREAM_PARSE_INTERVAL_BASE_MS = SSE_EVENT_WINDOW_MS;
const STREAM_PARSE_INTERVAL_MAX_MS = 1000;
const STREAM_PARSE_INTERVAL_BYTES_PER_STEP = 2400;

export function streamingParseIntervalMs(payloadLength: number): number {
  const steps = Math.ceil(payloadLength / STREAM_PARSE_INTERVAL_BYTES_PER_STEP);
  return Math.min(
    STREAM_PARSE_INTERVAL_MAX_MS,
    Math.max(STREAM_PARSE_INTERVAL_BASE_MS, steps * STREAM_PARSE_INTERVAL_BASE_MS),
  );
}

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
//
// Attach items (attachMode) render through AttachContent: the backend caption
// lines interleave with their thumbnails (label → image → label → image), the
// "[system] …" notice renders dimmed, and every thumbnail opens a lightbox on
// click instead of navigating away. Multimodal inbound_chat keeps the
// envelope layout but shares the same lightbox thumbnails.
function Thumbnail({ src, alt }: { src: string; alt: string }) {
  // Click-to-zoom overlay: no navigation (user ruling 2026-08-27 — the old
  // <a target="_blank"> opened the raw data-URI in a new tab). Click anywhere
  // on the backdrop (or the image) or press Escape to close. While open the
  // dialog holds focus (Tab is trapped — the dialog is the only focusable
  // node), background scroll is locked, and closing returns focus to the
  // trigger button (QA review #831 nit).
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const prevOverflow = document.body.style.overflow;
    const trigger = buttonRef.current;
    document.body.style.overflow = "hidden";
    dialogRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
      } else if (e.key === "Tab") {
        // Focus trap: keep Tab inside the modal instead of escaping to the
        // background content behind the overlay.
        e.preventDefault();
        dialogRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
      trigger?.focus();
    };
  }, [open]);
  // Layout-shift guard (QA review-955): the button is the fixed-size layout node
  // (16rem × 12rem — exactly the max-h-48 / max-w-[16rem] cap of the content
  // below), so the row height is reserved from the first paint. The lazy <img> is
  // absolutely centered inside it, so its own size growth (2×2px while unloaded →
  // capped content size once decoded) never moves the flow — zero CLS on the
  // scroll path. Keep the button size in sync with the img caps.
  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen(true)}
        aria-label={alt}
        data-testid="attach-thumbnail"
        className="relative block h-48 w-64 cursor-zoom-in rounded border-0 bg-transparent p-0"
      >
        {/* eslint-disable-next-line @next/next/no-img-element -- user upload / attach media, not a static asset */}
        <img
          src={src}
          alt={alt}
          crossOrigin="use-credentials"
          loading="lazy"
          className="absolute inset-0 m-auto max-h-48 max-w-[16rem] rounded border border-border object-contain"
        />
      </button>
      {open
        ? createPortal(
            // Full-page image viewer (user ruling 2026-08-30, QA #1085): the
            // overlay must cover the whole viewport and sit above every
            // interactive control, and the enlarged image takes 60-80% of the
            // screen (big images scale down to fit, small ones keep their
            // natural size — contain, never stretched). It cannot be rendered
            // inline inside the timeline row: `.timeline-item` carries
            // `content-visibility: auto` (globals.css) and therefore
            // paint+layout containment, which turns the row into the
            // containing block of `fixed` descendants AND a stacking context
            // — the backdrop was clipped to the row's box (~440x861 vs the
            // 1182x839 viewport) and the z-30 scroll-to-bottom button painted
            // above the z-50 backdrop. Mounting at document.body (same
            // pattern the Radix dialogs use) restores viewport coverage +
            // root stacking order.
            <div
              ref={dialogRef}
              role="dialog"
              aria-modal="true"
              aria-label={alt}
              data-testid="attach-lightbox"
              tabIndex={-1}
              onClick={() => setOpen(false)}
              className={cn("fixed inset-0 z-50 cursor-zoom-out items-center justify-center bg-black/80 p-4 outline-none", FLEX)}
            >
              {/* eslint-disable-next-line @next/next/no-img-element -- user upload / attach media, not a static asset */}
              <img
                src={src}
                alt={alt}
                crossOrigin="use-credentials"
                loading="lazy"
                className="max-h-[80vh] max-w-[80vw] rounded object-contain shadow-2xl"
              />
            </div>,
            document.body,
          )
        : null}
    </>
  );
}

// Attach item body: the caption lines rendered one per row, each delivered
// image's thumbnail directly under its own line (label1 → image1 → label2 →
// image2 — user ruling 2026-08-27). Pairing is structural: `imageCaptions`
// holds the exact backend-generated caption line of each image (1:1 with
// `images`), so a line matches at most one image and skipped entries (no
// image, "not delivered" reason) fall through as plain rows. The leading
// "[system] …" notice renders dimmed and small.
function AttachContent({
  payload,
  images,
  imageCaptions,
}: {
  payload: string;
  images: string[] | null;
  imageCaptions: string[] | null;
}) {
  const t = useTranslations("timeline");
  const lines = payload.split("\n");
  let imgIdx = 0;
  const rows = lines.map((line, i) => {
    const hasImage = imageCaptions?.[imgIdx] === line;
    const src = hasImage && images != null ? images[imgIdx++] : null;
    const isNotice = line.startsWith("[system]");
    return (
      <Fragment key={i}>
        <div
          className={cn(
            "whitespace-pre-wrap [overflow-wrap:anywhere] font-mono leading-relaxed m-0",
            isNotice
              ? "text-[11px] text-muted-foreground/70"
              : "text-sm text-foreground/90",
          )}
        >
          {line}
        </div>
        {src ? (
          <div className="mt-1">
            <Thumbnail src={src} alt={t("attachedImage")} />
          </div>
        ) : null}
      </Fragment>
    );
  });
  // Safety net: an image whose caption never matched a line (defensive — the
  // backend contract guarantees alignment) still renders, so no thumbnail is
  // ever dropped.
  const trailing = images?.slice(imgIdx) ?? [];
  return (
    <div className="space-y-1.5">
      {rows}
      {trailing.length ? (
        <div className={cn("flex-wrap gap-2", FLEX)}>
          {trailing.map((src) => (
            <Thumbnail key={src} src={src} alt={t("attachedImage")} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function EnvelopeContent({
  payload,
  images,
  showCopy = false,
  attachMode = false,
  imageCaptions = null,
}: {
  payload: string;
  // Image reference urls on a multimodal inbound — rendered as thumbnails below
  // the text. Gateway-relative, so each is resolved through assetUrl.
  images?: string[] | null;
  /** Show a copy button on the body pre block (for code_output). */
  showCopy?: boolean;
  /** Attach items render their caption lines interleaved with thumbnails. */
  attachMode?: boolean;
  /** Backend caption line per image (1:1 with `images`) — attach items only. */
  imageCaptions?: string[] | null;
}) {
  const t = useTranslations("timeline");
  const { header, body } = splitEnvelope(payload);
  // An image-only message stores "[image]" as its text placeholder; suppress that
  // literal in the body when real thumbnails render below.
  const showBody = body && !(images?.length && body === "[image]");
  // Attach items carry data URIs (self.attach media) — those render raw;
  // gateway-relative urls (user uploads) resolve through assetUrl.
  const resolveImageSrc = (src: string) => (src.startsWith("data:") ? src : assetUrl(src));
  // Attach items render through AttachContent: per-image captions interleave
  // label → thumbnail; without captions (legacy checkpoints, caption-only
  // attaches) every image falls to the trailing row and the lines stay plain
  // rows — the old layout, minus the navigating <a> wrappers.
  if (attachMode) {
    return (
      <AttachContent
        payload={payload}
        images={images ?? null}
        imageCaptions={imageCaptions?.length ? imageCaptions : null}
      />
    );
  }
  return (
    <>
      {header ? (
        <div className="mb-1.5">
          <span className="font-mono text-sm leading-relaxed text-foreground/90 [overflow-wrap:anywhere]">
            {header}
          </span>
        </div>
      ) : null}
      {showBody ? (
        showCopy ? (
          <div className="group relative">
            <CopyButton text={body} label={t("commandOutput")} />
            <pre className="whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-sm leading-relaxed text-foreground/90 m-0">
              {body}
            </pre>
          </div>
        ) : (
          <pre className="whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-sm leading-relaxed text-foreground/90 m-0">
            {body}
          </pre>
        )
      ) : null}
      {images?.length ? (
        <div className={cn("mt-1.5 flex-wrap gap-2", FLEX)}>
          {images.map((src) => (
            <Thumbnail key={src} src={resolveImageSrc(src)} alt={t("attachedImage")} />
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
    <div className="mt-1 font-sans text-xs text-amber-700 dark:text-amber-400 select-none">
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
  const { settings } = useUserSettings();
  // Throttle the heavy-parse content (chat markdown / python highlight) while it
  // streams. The hook is called unconditionally (rules of hooks); `live` is
  // false for every other kind, making it a no-op pass-through.
  const live =
    (item.kind === "agent_chat" && !!item.partial) ||
    isLiveReasoning(item) ||
    (item.kind === "agent_code" && streaming);
  const streamingPayload = useThrottledStreaming(
    item.payload,
    live,
    streamingParseIntervalMs(item.payload.length),
  );

  switch (item.kind) {
    case "inbound_chat":
    case "inbound_compact_summary":
    case "inbound_compact_request":
      return <EnvelopeContent payload={item.payload} images={item.images} />;

    case "attach":
      return (
        <EnvelopeContent
          payload={item.payload}
          images={item.images}
          attachMode
          imageCaptions={item.image_captions}
        />
      );

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
      return settings["display.render_reasoning_markdown"] as boolean ? (
        <>
          <div
            className={cn(
              "text-muted-foreground/90",
              item.partial && "italic opacity-75",
              item.interrupted && "border-b border-dashed border-amber-500/60 pb-1",
            )}
          >
            {item.partial ? <span className="text-muted-foreground">… </span> : null}
            <ChatMarkdown content={streamingPayload} />
          </div>
          {item.interrupted ? <InterruptedNotice /> : null}
        </>
      ) : (
        <>
          <pre
            className={cn(
              "whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-xs leading-relaxed text-muted-foreground/90 m-0",
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
        <pre className="whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-xs leading-relaxed text-muted-foreground/90 m-0">
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
    <div className="text-muted-foreground font-sans text-xs">{t("unknownEventType")}</div>
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
