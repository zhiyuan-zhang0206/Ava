"use client";

import { useTranslations } from "next-intl";

import {
  type DetailsMode,
  useContentToggle,
  useContentToggleReset,
} from "@/lib/content-toggle-store";
import { cn } from "@/lib/utils";
import { OVERFLOW_HIDDEN } from "@/lib/layout";

/**
 * Timeline Details control in the header — a compound button with a
 * "Details" label on the left and a small dropdown selector
 * (All / Last / None) on the right, styled as one unified control.
 *
 * The mode governs block expansion across every message kind:
 * All (always expanded), Last (only the most recent block, follows
 * streaming), None (always collapsed).
 */
export function ContentToggle() {
  const t = useTranslations("contentToggle");
  const { detailsMode, setDetailsMode } = useContentToggle();
  const bumpReset = useContentToggleReset((s) => s.bumpReset);

  return (
    <div
      className={cn("inline-flex rounded-md border border-border", OVERFLOW_HIDDEN)}
      role="group"
      aria-label={t("detailsMode")}
    >
      <span
        className="px-2 py-0.5 text-xs font-sans font-medium text-foreground/80 border-r border-border select-none"
      >
        {t("details")}
      </span>
      <select
        value={detailsMode}
        onChange={(e) => {
          setDetailsMode(e.target.value as DetailsMode);
          // Same-mode re-pick must still re-apply (collapse-all in None mode
          // after manual expansions) — bump the reset token unconditionally.
          bumpReset();
        }}
        className={cn(
          "appearance-none px-1.5 py-0.5 pr-4 text-xs font-sans font-medium",
          "bg-[url('data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20width%3D%228%22%20height%3D%225%22%20viewBox%3D%220%200%208%205%22%3E%3Cpath%20fill%3D%22none%22%20stroke%3D%22%236b7280%22%20stroke-width%3D%221.3%22%20d%3D%22M1%201l3%203%203-3%22%2F%3E%3C%2Fsvg%3E')]",
          "bg-[length:8px_5px] bg-[right_4px_center] bg-no-repeat",
          "text-foreground cursor-pointer outline-none",
          "hover:bg-accent/40 transition-colors",
        )}
        aria-label={t("detailsLevel")}
      >
        <option value="all">{t("all")}</option>
        <option value="last">{t("last")}</option>
        <option value="none">{t("none")}</option>
      </select>
    </div>
  );
}
