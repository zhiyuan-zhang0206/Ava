"use client";

// Persistent user settings — loaded from the server-side user_settings
// DB table, with local defaults for every known key. Values are cached in
// React Query and mutated through optimistic updates + server PUT.
//
// Usage:
//   const { settings, setSetting, isLoading } = useUserSettings();
//   const showMachine = settings["display.show_machine_name"] as boolean;
//
// The hook returns the merged settings map (defaults + server values) and
// a setSetting(key, value) that optimistically updates the cache while
// PUTting to the server. Falls back to defaults when the server is
// unreachable.

"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "./api";
import { errMsg } from "./errors";
import { track } from "./telemetry";
import { USER_SETTING_DEFAULTS } from "./types";
import { useStore } from "./store";

export const SETTINGS_QUERY_KEY = ["user-settings"] as const;

export function useUserSettings(): {
  settings: Record<string, unknown>;
  setSetting: (key: string, value: unknown) => void;
  isLoading: boolean;
} {
  const queryClient = useQueryClient();

  const { data, isLoading } = useQuery({
    queryKey: SETTINGS_QUERY_KEY,
    queryFn: async () => {
      const res = await api.getSettings();
      // Merge server values on top of defaults
      const merged: Record<string, unknown> = { ...USER_SETTING_DEFAULTS };
      for (const s of res.settings) {
        merged[s.key] = s.value;
      }
      return merged;
    },
    // Settings change rarely; long stale time avoids refetching on every mount.
    staleTime: 60_000,
    // The sidebar keeps this query mounted for the whole session, and settings
    // are not SSE-pushed — so without a focus refetch a change made on another
    // device / window would never reach an already-open tab. Refetching on
    // window focus (past the 60s stale window) converges cross-client without
    // polling. Overrides the global refetchOnWindowFocus:false (that default
    // exists to stop stray GETs shadowing SSE-driven queries; this one has no
    // SSE feed to shadow).
    refetchOnWindowFocus: true,
  });

  const mutation = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) =>
      api.putSetting(key, value),
    onMutate: async ({ key, value }) => {
      // Cancel in-flight queries so they don't overwrite our optimistic update
      await queryClient.cancelQueries({ queryKey: SETTINGS_QUERY_KEY });
      // Snapshot previous value for rollback. Before the initial GET has
      // populated the cache (e.g. the sidebar quick toggle clicked right
      // after load), fall back to the defaults so onError still restores
      // a sane state instead of skipping the rollback.
      const prev =
        queryClient.getQueryData<Record<string, unknown>>(SETTINGS_QUERY_KEY) ??
        { ...USER_SETTING_DEFAULTS };
      // Optimistically update
      queryClient.setQueryData<Record<string, unknown>>(SETTINGS_QUERY_KEY, (old) => ({
        ...(old ?? USER_SETTING_DEFAULTS),
        [key]: value,
      }));
      return { prev };
    },
    onError: (err, _vars, context) => {
      // Roll back on failure — and say so: a silently reverted preference
      // looks like the app ignored the user (Task #1051).
      if (context?.prev) {
        queryClient.setQueryData(SETTINGS_QUERY_KEY, context.prev);
      }
      useStore.getState().showToast(`Failed to save setting: ${errMsg(err)}`);
    },
    onSettled: () => {
      // Refetch to reconcile with server truth (rare, but belt-and-suspenders)
      void queryClient.invalidateQueries({ queryKey: SETTINGS_QUERY_KEY });
    },
  });

  const setSetting = useCallback(
    (key: string, value: unknown) => {
      // User-modeling telemetry: every settings/layout/preference change is
      // one interaction. `value` is sanitized client-side (bool / number /
      // ≤64-char scalar); the settings key is the allowlisted vocabulary.
      track("setting-change", { key, value });
      mutation.mutate({ key, value });
    },
    [mutation],
  );

  return {
    settings: data ?? { ...USER_SETTING_DEFAULTS },
    setSetting,
    isLoading,
  };
}

// A DB-backed setting that a user drags (timeline width, force-layout sliders):
// the value changes many times per second, so a write-through PUT on every
// change would flood the server. This keeps a local copy for instant feedback
// and persists it debounced (one PUT after the drag settles), flushing any
// pending write on unmount so a value isn't lost mid-drag.
//
// The server value is adopted into the local copy on initial load and on
// cross-device changes, but never while the user is actively editing (a
// `dirty` window between the last edit and its flush) — so an in-flight drag is
// not clobbered by a background refetch.
export function useDebouncedSetting<T>(
  key: string,
  defaultValue: T,
  delayMs = 400,
): readonly [T, (value: T) => void] {
  const { settings, setSetting } = useUserSettings();
  const stored = settings[key] as T | undefined;
  const [local, setLocal] = useState<T>(stored ?? defaultValue);

  const dirty = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pending = useRef<{ value: T } | null>(null);
  // Latest closures in refs so the flush/unmount paths never fire a stale PUT.
  // Updated in an effect (writing a ref during render is disallowed); the
  // one-commit lag is irrelevant here — flush only runs from event handlers /
  // unmount, after the effect that captured the current values has committed.
  const setSettingRef = useRef(setSetting);
  const keyRef = useRef(key);
  useEffect(() => {
    setSettingRef.current = setSetting;
    keyRef.current = key;
  });

  const flush = useCallback(() => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
    if (pending.current) {
      setSettingRef.current(keyRef.current, pending.current.value);
      pending.current = null;
    }
    dirty.current = false;
  }, []);

  // Adopt the server value on load / cross-device change, unless mid-edit
  // (skipped while dirty so an active drag is not clobbered).
  useEffect(() => {
    if (dirty.current) return;
    setLocal(stored ?? defaultValue);
  }, [stored, defaultValue]);

  const setValue = useCallback(
    (value: T) => {
      dirty.current = true;
      setLocal(value);
      pending.current = { value };
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(flush, delayMs);
    },
    [delayMs, flush],
  );

  // Flush any pending write when the component unmounts (e.g. switching fleet
  // tabs mid-slider-drag) so the last value still reaches the server.
  useEffect(() => () => flush(), [flush]);

  return [local, setValue] as const;
}
