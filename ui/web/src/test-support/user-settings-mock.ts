"use client";

// Reactive test double for @/lib/use-user-settings.
//
// The real hooks are backed by React Query + the settings API; under test we
// want the same public surface (useUserSettings / useDebouncedSetting) but with
// a synchronous, in-memory store so a setSetting flips the value and re-renders
// consumers immediately (no debounce, no network). Wire it up per test file:
//
//   vi.mock("@/lib/use-user-settings", () => import("@/test-support/user-settings-mock"));
//   import { resetMockSettings, setMockSetting } from "@/test-support/user-settings-mock";
//   beforeEach(() => resetMockSettings());              // back to defaults
//   beforeEach(() => resetMockSettings({ "display.x": 1 })); // seed overrides
//
// The store is module-level, so it is shared across the tests in one file
// (vitest isolates modules per file) — reset it in beforeEach.

import { useCallback, useSyncExternalStore } from "react";

import { USER_SETTING_DEFAULTS } from "@/lib/types";

let store: Record<string, unknown> = { ...USER_SETTING_DEFAULTS };
const listeners = new Set<() => void>();
const setSettingSpy: { key: string; value: unknown }[] = [];

function emit(): void {
  for (const l of listeners) l();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function snapshot(): Record<string, unknown> {
  return store;
}

/** Reset the store to defaults (plus optional seeded overrides) and clear the
 *  recorded setSetting calls. Call in beforeEach. */
export function resetMockSettings(initial: Record<string, unknown> = {}): void {
  store = { ...USER_SETTING_DEFAULTS, ...initial };
  setSettingSpy.length = 0;
  emit();
}

/** Imperatively set a value (as if the server pushed it). */
export function setMockSetting(key: string, value: unknown): void {
  store = { ...store, [key]: value };
  setSettingSpy.push({ key, value });
  emit();
}

/** Every setSetting the component made, in order. */
export function mockSetSettingCalls(): readonly { key: string; value: unknown }[] {
  return setSettingSpy;
}

export function useUserSettings(): {
  settings: Record<string, unknown>;
  setSetting: (key: string, value: unknown) => void;
  isLoading: boolean;
} {
  const settings = useSyncExternalStore(subscribe, snapshot, snapshot);
  const setSetting = useCallback((key: string, value: unknown) => setMockSetting(key, value), []);
  return { settings, setSetting, isLoading: false };
}

export function useDebouncedSetting<T>(
  key: string,
  defaultValue: T,
): readonly [T, (value: T) => void] {
  const settings = useSyncExternalStore(subscribe, snapshot, snapshot);
  const stored = settings[key] as T | undefined;
  const value = stored ?? defaultValue;
  const setValue = useCallback((v: T) => setMockSetting(key, v), [key]);
  return [value, setValue] as const;
}
