"use client";

import { Settings2 } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { useContentToggle } from "@/lib/layout/content-toggle-store";
import { cn } from "@/lib/format/utils";
import { FLEX } from "@/lib/layout/layout";

export const DETAILS_SETTINGS_HREF = "/control#display-timeline";

// meiva:ref ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md#details-mode-owner
export function ContentToggle() {
  const t = useTranslations("contentToggle");
  const { detailsMode, isLoading } = useContentToggle();

  if (isLoading) return null;

  return (
    <Link
      href={DETAILS_SETTINGS_HREF}
      data-testid="details-summary"
      title={t("openSettings")}
      aria-label={t("detailsWithValue", { mode: t(detailsMode) })}
      className={cn(
        "items-center gap-1 rounded px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground transition-colors",
        "hover:bg-accent hover:text-foreground focus-visible:ring-1 focus-visible:ring-ring focus-visible:outline-none",
        FLEX,
      )}
    >
      <span>{t("details")}</span>
      <span aria-hidden className="text-muted-foreground/50">·</span>
      <span data-testid="details-summary-mode" className="text-foreground/70">{t(detailsMode)}</span>
      <Settings2 aria-hidden className="size-3 opacity-60" />
    </Link>
  );
}
