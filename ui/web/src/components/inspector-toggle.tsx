"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { useTranslations } from "next-intl";

import { useInspectorOpen } from "@/lib/inspector-panel-store";
import { cn } from "@/lib/utils";

/** Inspector toggle button — opens/closes the panel from the HeaderBar's
 *  children slot at the top-right of the content column. Closed means no
 *  inspect traffic: the panel's enabled query performs the first fetch only
 *  after this button opens it. */
export function InspectorToggle() {
  const { open, toggle } = useInspectorOpen();
  const t = useTranslations("inspector");
  return (
    <button
      type="button"
      onClick={toggle}
      data-inspector-toggle=""
      aria-label={open ? t("closeInspector") : t("openInspector")}
      className={cn(
        "inline-flex items-center gap-1.5 rounded border px-1.5 py-0.5 font-mono text-[11px] transition-colors select-none",
        open
          ? "border-border bg-accent text-accent-foreground"
          : "border-transparent text-muted-foreground/50 hover:border-border hover:text-muted-foreground",
      )}
    >
      {/* Closed points right to open the right-side panel; open points left to
          close it back and keeps the "Close inspector" semantics. The
          2026-08-24 user ruling supersedes the 8/6 and #1065 up-arrow ruling. */}
      {open ? <ChevronLeft className="size-3.5" /> : <ChevronRight className="size-3.5" />}
      <span className="hidden sm:inline">{t("toggle")}</span>
    </button>
  );
}
