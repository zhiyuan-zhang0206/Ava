"use client";

import { Search, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect } from "react";

import type { AgentRow } from "@/lib/types";
import { FLEX, OVERFLOW_HIDDEN } from "@/lib/layout";
import { cn } from "@/lib/utils";

// Task #723: search moved from an inline sidebar box to a floating overlay —
// a dimmed backdrop + a centered, shadowed panel (command-palette style), so
// search stays reachable even when the sidebar is collapsed. Task #750 (user
// ruling): the overlay is fully independent — the sidebar list is NOT filtered
// while typing; the query lives in the store only so the overlay input keeps
// its text across open/close. Results are agent-id DESCENDING. Picking a
// result selects the agent and closes.
export function SearchOverlay({
  open,
  onClose,
  searchQuery,
  onSearchQueryChange,
  agents,
  onSelect,
}: {
  open: boolean;
  onClose: () => void;
  searchQuery: string;
  onSearchQueryChange: (q: string) => void;
  agents: AgentRow[];
  onSelect: (id: number) => void;
}) {
  const t = useTranslations("sidebar");
  // Esc closes the overlay.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const q = searchQuery.trim().toLowerCase();
  // #750 (user ruling): the overlay list is always ordered by agent id
  // DESCENDING (newest first), matching the sidebar's flat-list default —
  // both with and without a query.
  const results = (q
    ? agents.filter(
        (a) =>
          (a.label?.toLowerCase().includes(q) ?? false) ||
          String(a.agent_id).includes(q),
      )
    : agents
  ).slice()
    .sort((a, b) => b.agent_id - a.agent_id);

  return (
    <div className="fixed inset-0 z-50">
      {/* Slight dim + click-to-close backdrop */}
      <div
        data-testid="search-overlay-backdrop"
        className="absolute inset-0 bg-black/30"
        onClick={onClose}
        aria-hidden="true"
      />
      <div className={cn("relative mx-auto mt-[12vh] w-[min(30rem,calc(100vw-2rem))] rounded-lg border border-border bg-background shadow-2xl", OVERFLOW_HIDDEN)}>
        <div className={cn("items-center gap-2 border-b border-border px-3", FLEX)}>
          <Search className="size-4 shrink-0 text-muted-foreground" />
          <input
            autoFocus
            type="text"
            value={searchQuery}
            onChange={(e) => onSearchQueryChange(e.target.value)}
            placeholder={t("searchPlaceholder")}
            aria-label={t("searchAgent")}
            className="w-full bg-transparent py-2.5 text-sm focus:outline-none placeholder:text-muted-foreground/60"
          />
          {searchQuery && (
            <button
              onClick={() => onSearchQueryChange("")}
              className="rounded-sm p-0.5 text-muted-foreground hover:bg-sidebar-accent"
              aria-label={t("clearSearch")}
            >
              <X className="size-3.5" />
            </button>
          )}
        </div>
        <ul className="max-h-[45vh] overflow-y-auto py-1">
          {results.length === 0 ? (
            <li className="px-3 py-2.5 text-xs text-muted-foreground">
              {t("noMatch", { query: searchQuery })}
            </li>
          ) : (
            results.map((a) => (
              <li key={a.agent_id}>
                <button
                  type="button"
                  data-testid={`overlay-result-${a.agent_id}`}
                  onClick={() => {
                    onSelect(a.agent_id);
                    onClose();
                  }}
                  className={cn("w-full items-center px-3 py-2 text-left font-sans text-xs text-muted-foreground hover:bg-sidebar-accent/60 hover:text-foreground", FLEX)}
                >
                  {/* Same shape as sidebar rows (task #750): `#id · label`, or
                      bare `#id` when there is no label. */}
                  <span className="shrink-0 font-mono">#{a.agent_id}</span>
                  {a.label ? (
                    <>
                      {" · "}
                      <span className="truncate">{a.label}</span>
                    </>
                  ) : null}
                </button>
              </li>
            ))
          )}
        </ul>
      </div>
    </div>
  );
}

// ── Expanded sidebar body ──
