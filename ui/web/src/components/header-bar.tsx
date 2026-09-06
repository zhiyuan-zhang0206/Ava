"use client";

// Top header bar — shows the current agent label and houses the sidebar
// hamburger (mobile). Navigation entries (Memory Graph / Fleet / Insights /
// Control) moved to the sidebar footer (2026-08-05, user ruling); the right-
// side children slot homes the Alerts badge and Inspector toggle.

import { Menu } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";
import { BAR_DIVIDER_CLASS, BAR_HEIGHT_CLASS, FLEX, FLEX_1, MIN_W_0 } from "@/lib/layout";

interface Props {
  label: string;
  onOpenSidebar: () => void;
  children?: ReactNode;
  /** Timeline column cap — the bar floats above the timeline surface and
   *  its inner content aligns with the conversation column (user ruling
   *  2026-08-06). Absent = full width (mobile). */
  maxWidthCss?: string;
}

export function HeaderBar({ label, onOpenSidebar, children, maxWidthCss }: Props) {
  const t = useTranslations("nav");

  return (
    <header
      className={cn(
        // Floating bar (user ruling 2026-08-06): absolute over the
        // full-bleed timeline surface, translucent + backdrop blur so the
        // timeline it covers shows through with a frosted-glass feel —
        // same treatment as the search button. The bar no longer reserves a
        // row; the timeline scrolls beneath it.
        "absolute inset-x-0 top-0 z-20 bg-background/80 backdrop-blur-md",
        "px-4 py-2 font-mono text-sm text-muted-foreground",
        BAR_DIVIDER_CLASS,
        BAR_HEIGHT_CLASS,
      )}
    >
      <div
        style={maxWidthCss ? { maxWidth: maxWidthCss } : undefined}
        className={cn("mx-auto h-full w-full items-center justify-between gap-2", FLEX)}
      >
        <div className={cn("items-center gap-2", FLEX, MIN_W_0, FLEX_1)}>
          <button
            onClick={onOpenSidebar}
            className="md:hidden p-1 -ml-1 rounded hover:bg-sidebar-accent shrink-0"
            aria-label={t("openSidebar")}
          >
            <Menu className="size-5" />
          </button>
          <span className="truncate">{label}</span>
        </div>
        <div className={cn("items-center gap-2", FLEX)}>{children}</div>
      </div>
    </header>
  );
}
