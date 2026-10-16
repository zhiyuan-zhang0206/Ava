"use client";

import { useCallback } from "react";

import { useBreakpoint } from "../layout/breakpoint";
import { useStore } from "../state/store";
import { useUserSettings } from "../state/use-user-settings";

export interface InspectorWindowState {
  inspectorHours: number | null;
  setInspectorHours: (hours: number | null) => void;
}

export function useInspectorHours(): InspectorWindowState {
  const inspectorHours = useStore((s) => s.inspectorHours);
  const setInspectorHours = useStore((s) => s.setInspectorHours);
  return { inspectorHours, setInspectorHours };
}

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
