"use client";

// Compact strip above the composer listing inbounds the agent has queued
// but not yet started processing (status='pending'). Once claimed, a
// message moves into the timeline, so it leaves this strip. Sources are
// labeled so it's clear who queued each (User / another agent / a
// scheduled task). A multimodal message shows its image thumbnails, so a
// queued image is visible before the claim (its reference urls come from
// the /pending endpoint). Renders nothing when the queue is empty.

import { Clock } from "lucide-react";
import { useTranslations } from "next-intl";

import { assetUrl } from "@/lib/api";
import { FLEX, MIN_W_0 } from "@/lib/layout";
import type { PendingInbound } from "@/lib/types";
import { cn } from "@/lib/utils";

// Map the inbound `source` tag to a short human label. Tags:
// user / ui:page:<name> / agent:N / schedule:N[:expired] / system / self.
function sourceLabel(source: string | null): string {
  if (!source) return "system";
  // user typing, or a callback from an agent-rendered page — both are "User".
  if (source === "user" || source.startsWith("ui:page:")) return "User";
  if (source.startsWith("agent:")) return `Agent #${source.slice("agent:".length)}`;
  if (source.startsWith("schedule:")) return "scheduled";
  return source;
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
  if (items.length === 0) return null;
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
          {t("pending", { count: items.length })}
        </div>
        <ul className="mt-0.5 max-h-24 overflow-y-auto space-y-0.5">
          {items.map((it) => (
            <li key={it.id} className={cn("items-center gap-1.5 pl-4", FLEX, MIN_W_0)}>
              <span className="shrink-0 text-muted-foreground/70">· {sourceLabel(it.source)}</span>
              {/* An image-only message stores "[image]" as its text placeholder
                  (shared/db.py); suppress that literal once real thumbnails
                  render — the same rule the timeline's EnvelopeContent uses. */}
              {it.images?.length && it.content === "[image]" ? null : (
                <span className="truncate">{it.content}</span>
              )}
              {it.images?.length ? (
                <span className={cn("shrink-0 gap-1", FLEX)}>
                  {it.images.map((src) => (
                    // eslint-disable-next-line @next/next/no-img-element -- agent upload reference, not a static asset
                    <img
                      key={src}
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
