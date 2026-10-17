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
// The mode is stored as display.expand_runs_mode (string).

import { useCallback } from "react";

import { useUserSettings } from "../state/use-user-settings";

export type DetailsMode = "all" | "last" | "none";

function normalizeMode(raw: unknown): DetailsMode {
  if (raw === "all" || raw === "last" || raw === "none") return raw;
  // Unknown value → the default mode.
  return "none";
}

interface ContentToggleState {
  detailsMode: DetailsMode;
  setDetailsMode: (mode: DetailsMode) => void;
  /** True until the DB-backed setting has arrived from the server. While
   *  loading, settings falls back to USER_SETTING_DEFAULTS, whose
   *  expand_runs_mode default is "none" (user ruling 2026-09-17) — but a
   *  consumer must not read that fallback as the user's real choice: a stale
   *  "all" read would flash every detail block open until the real value
   *  lands (the "details=none but blocks auto-expand" report). Consumers that
   *  render blocks (TimelineView) must treat isLoading as "mode unknown" and
   *  render the safe collapsed state. */
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


// meiva:ref ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md#details-mode-owner
import { create } from "zustand";

interface ContentToggleResetState {
  resetPending: boolean;
  bumpReset: () => void;
  consumeReset: () => void;
}

export const useContentToggleReset = create<ContentToggleResetState>((set) => ({
  resetPending: false,
  bumpReset: () => set({ resetPending: true }),
  consumeReset: () => set({ resetPending: false }),
}));
