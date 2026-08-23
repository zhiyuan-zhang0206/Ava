"use client";

// Inspector-panel open/closed flag. Two behaviors, one hook:
//
// - Desktop (≥ lg): the panel is a fixed right-side panel (user ruling
//   2026-08-23, superseding the 2026-08-05 floating overlay), so "I want the
//   inspector open" is still a workspace preference — a DB-backed user setting
//   (display.inspector_open) shared across browsers/machines. Default: CLOSED;
//   the composer's toggle opens it.
//
// - Mobile (< lg): the panel is a full-screen overlay that hides the
//   timeline, so its open state is per-session view state (like the mobile
//   sidebar drawer) — default CLOSED, kept in the zustand store, and never
//   written back to the shared setting: opening the inspector on a phone
//   must not yank the desktop panel open, and closing it there must not
//   close the desktop one.

import { useCallback } from "react";

import { useBreakpoint } from "./breakpoint";
import { useStore } from "./store";
import { useUserSettings } from "./use-user-settings";

export function useInspectorOpen(): { open: boolean; toggle: () => void } {
  const { isLarge } = useBreakpoint();
  const { settings, setSetting } = useUserSettings();
  const mobileOpen = useStore((s) => s.mobileInspectorOpen);
  const setMobileOpen = useStore((s) => s.setMobileInspectorOpen);

  const open = isLarge
    ? settings["display.inspector_open"] === true
    : mobileOpen;

  const toggle = useCallback(() => {
    if (isLarge) {
      setSetting("display.inspector_open", !open);
    } else {
      setMobileOpen(!mobileOpen);
    }
  }, [isLarge, open, mobileOpen, setSetting, setMobileOpen]);

  return { open, toggle };
}
