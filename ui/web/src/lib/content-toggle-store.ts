"use client";

// Timeline Details toggle — DB-backed user setting. A single three-state
// Details mode (All / Last / None) that governs block expansion across
// every message kind.
//
// DetailsMode:
//   "all"  — all blocks expanded
//   "last" — only the last block expanded (follows streaming)
//   "none" — all blocks collapsed
//
// The mode is stored as display.expand_runs_mode (string). Older values
// are migrated transparently: "auto" → "last", true → "all", false → "none".

import { useCallback } from "react";

import { useUserSettings } from "./use-user-settings";

export type DetailsMode = "all" | "last" | "none";

function normalizeMode(raw: unknown): DetailsMode {
  if (raw === "all" || raw === "last" || raw === "none") return raw;
  // Migrate legacy "auto" → "last"
  if (raw === "auto") return "last";
  // Migrate legacy boolean: true → "all", false → "none"
  if (raw === true) return "all";
  if (raw === false) return "none";
  return "all";
}

interface ContentToggleState {
  detailsMode: DetailsMode;
  setDetailsMode: (mode: DetailsMode) => void;
  /** True until the DB-backed setting has arrived from the server. While
   *  loading, settings falls back to USER_SETTING_DEFAULTS, whose
   *  expand_runs_mode default is "all" — a consumer that renders expansion
   *  from detailsMode alone would flash every detail block open until the
   *  real value lands (the "details=none but blocks auto-expand" report).
   *  Consumers that render blocks (TimelineView) must treat isLoading as
   *  "mode unknown" and render the safe collapsed state. */
  isLoading: boolean;
}

export function useContentToggle(): ContentToggleState {
  const { settings, setSetting, isLoading } = useUserSettings();
  const detailsMode = normalizeMode(settings["display.expand_runs_mode"]);

  const setDetailsMode = useCallback(
    (mode: DetailsMode) => setSetting("display.expand_runs_mode", mode),
    [setSetting],
  );

  return { detailsMode, setDetailsMode, isLoading };
}


// Same-mode re-pick reset (user ruling 2026-08-06): picking the CURRENT
// details mode again must re-apply it — e.g. in None mode the user may have
// manually expanded some blocks, and re-picking None must collapse them all
// again. A plain `setDetailsMode(sameValue)` writes the same DB setting and
// never fires (the mode does not change), so the selector bumps this token on
// EVERY user selection; TimelineView clears its per-item/per-turn overrides
// whenever the token moves.
import { create } from "zustand";

interface ContentToggleResetState {
  resetToken: number;
  bumpReset: () => void;
}

export const useContentToggleReset = create<ContentToggleResetState>((set) => ({
  resetToken: 0,
  bumpReset: () => set((s) => ({ resetToken: s.resetToken + 1 })),
}));
