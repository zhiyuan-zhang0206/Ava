"use client";

import { Clock } from "lucide-react";
import { useTranslations } from "next-intl";

import { assetUrl } from "@/lib/transport/api";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";
import type { PendingInbound } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";

export function userPending(items: readonly PendingInbound[]): PendingInbound[] {
  return items.filter((it) => it.sender === "user");
}

export function PendingStrip({
  items,
  maxWidthCss,
}: {
  items: PendingInbound[];
  /** Composer column cap — the strip floats above the composer and its
   *  content aligns with the conversation column (user ruling 2026-08-06). */
  maxWidthCss?: string;
}) {
  const t = useTranslations("pendingStrip");
  const queued = userPending(items);
  if (queued.length === 0) return null;
  return (
    // User ruling 2026-08-06: same divider rhythm as the composer — px-4 on
    // the root, divider on the inner wrapper (never edge to edge).
    <div className="px-4 py-1.5 text-xs text-muted-foreground">
      <div
        style={maxWidthCss ? { maxWidth: maxWidthCss } : undefined}
        className="mx-auto border-t border-border pt-1.5"
      >
        <div className={cn("items-center gap-1.5 font-medium", FLEX)}>
          <Clock className="size-3 shrink-0" />
          {t("pending", { count: queued.length })}
        </div>
        <ul className="mt-0.5 max-h-24 overflow-y-auto space-y-0.5">
          {queued.map((it) => (
            <li key={it.id} className={cn("items-center gap-1.5 pl-4", FLEX, MIN_W_0)}>
              {/* An image-only message stores "[image]" as its text placeholder
                  (gateway/agents/state.py `_normalize_message_content`);
                  suppress that literal once real thumbnails render — the same
                  rule the timeline's EnvelopeContent uses. */}
              {it.images?.length && it.content === "[image]" ? null : (
                <span className="truncate">{it.content}</span>
              )}
              {it.images?.length ? (
                <span className={cn("shrink-0 gap-1", FLEX)}>
                  {it.images.map((src, idx) => (
                    // eslint-disable-next-line @next/next/no-img-element -- agent upload reference, not a static asset
                    <img
                      // index-composite: an identical url twice in one message must not collide keys.
                      key={`${idx}:${src}`}
                      src={assetUrl(src)}
                      alt={t("imageAlt")}
                      crossOrigin="use-credentials"
                      loading="lazy"
                      className="h-10 w-10 rounded border border-border object-cover"
                    />
                  ))}
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
