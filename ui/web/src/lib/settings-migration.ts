"use client";

// One-time migration: the frontend used to persist display/behavior preferences
// in per-browser localStorage. They are DB-backed user settings now
// (user_settings, via useUserSettings) so they sync across every frontend
// entrypoint. This module carries any legacy localStorage values into the DB on
// first load after upgrade, then removes the dead keys.
//
// Runs once on app boot (mounted by <SettingsMigration/> in providers.tsx). For
// each legacy key: read it; if it holds a valid, non-default value, PUT it to
// the DB; then drop the key either way (a missing / default / malformed value
// just gets cleaned up — it already matches the DB default). "Last device to
// migrate wins" is fine for a one-shot: after migration every device reads the
// DB, and the localStorage copies are gone.
//
// EPHEMERAL selections are deliberately NOT migrated and keep their localStorage
// keys (per-device by design): the current mobile fleet tab
// (`ava.fleet.mobileTab`), the last-viewed agent (`ava.active.agent_id`), and
// the react-resizable-panels split ratios (`ava.fleet.split`,
// `ava.fleet.queue-split`).
//
// The zustand-persisted spawn-composer defaults (`ava-spawn-prefs`) ARE a
// durable preference and get migrated — but from a different shape (a zustand
// `{state:{...}}` blob → three behavior.* keys), handled separately below.

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { api } from "./api";
import { useAuth } from "./auth-context";
import { STATS_WINDOWS } from "./sidebar";
import { USER_SETTING_DEFAULTS } from "./types";
import { SETTINGS_QUERY_KEY } from "./use-user-settings";

// Write one setting to the DB. MUST reject on failure (e.g. a 401 / gateway
// error) so the caller keeps the legacy key for a later retry instead of
// dropping the user's value.
type WriteSetting = (key: string, value: unknown) => Promise<void>;

// A parser returns the value to store, or undefined to drop the key without
// writing (missing / malformed / out-of-range).
type Parse = (raw: string) => unknown;

const parseBool: Parse = (raw) => (raw === "true" ? true : raw === "false" ? false : undefined);
// (parseNumber was removed with the last numeric migration — sidebar width,
// which is now drop-only; re-add it if a future migration needs a number.)
const parseEnum =
  (allowed: readonly string[]): Parse =>
  (raw) =>
    allowed.includes(raw) ? raw : undefined;
const parseStatsWindow: Parse = (raw) => {
  const n = Number(raw);
  return (STATS_WINDOWS as readonly number[]).includes(n) ? n : undefined;
};
const parseJson: Parse = (raw) => {
  try {
    const v: unknown = JSON.parse(raw);
    return typeof v === "object" && v !== null ? v : undefined;
  } catch {
    return undefined;
  }
};

interface MigrationEntry {
  readonly legacyKey: string;
  // The DB setting key to write, or "" for a drop-only legacy key (removed,
  // never migrated).
  readonly settingKey: string;
  readonly parse?: Parse;
  // When the parsed value deep-equals this, drop the key without a redundant
  // PUT (the DB default already matches). Omitted → always write a valid value.
  readonly default?: unknown;
}

// Order is irrelevant — each entry is independent.
export const SETTINGS_MIGRATIONS: readonly MigrationEntry[] = [
  { legacyKey: "ava:inspector-open", settingKey: "display.inspector_open", parse: parseBool, default: false },
  // display.sidebar_width is a legacy value the sidebar ignores. Homepage
  // split ratios are now library-owned localStorage state — drop-only, never
  // migrate the old pixel width into user_settings.
  { legacyKey: "ava.sidebar.width", settingKey: "" },
  { legacyKey: "ava.sidebar.collapsed", settingKey: "display.sidebar_collapsed", parse: parseBool, default: false },
  { legacyKey: "ava.sidebar.viewMode", settingKey: "display.sidebar_view_mode", parse: parseEnum(["flat", "tree"]), default: "flat" },
  { legacyKey: "ava.sidebar.flatSort.v2", settingKey: "display.sidebar_sort", parse: parseJson },
  { legacyKey: "ava.sidebar.statsWindowHours", settingKey: "display.stats_window_hours", parse: parseStatsWindow, default: 24 },
  { legacyKey: "ava.sidebar.showTerminated", settingKey: "display.show_terminated", parse: parseBool, default: false },
  { legacyKey: "ava.fleet.leftView", settingKey: "display.fleet_left_view", parse: parseEnum(["graph", "tasks"]), default: "graph" },
  { legacyKey: "ava.fleet.queueCollapsed.v1", settingKey: "display.fleet_queue_collapsed", parse: parseBool, default: false },
  { legacyKey: "ava.fleet.taskGraphMode", settingKey: "display.task_graph_mode", parse: parseEnum(["graph", "kanban"]), default: "graph" },
  { legacyKey: "ava.fleet.taskShowDone", settingKey: "display.task_show_done", parse: parseBool, default: false },
  { legacyKey: "ava.fleet.taskShowCanceled", settingKey: "display.task_show_canceled", parse: parseBool, default: false },
  { legacyKey: "ava.fleet.forceParams", settingKey: "display.graph_force_params", parse: parseJson },
  // Task force params: read the live key (#651 bumped it to .v2 to shed the
  // stuck-gravity default; #739 bumped the DB key to .v2 as well — the node
  // geometry changed cards → squares, so pre-refactor tunings no longer fit);
  // the pre-v2 key is dead — drop it below.
  { legacyKey: "ava.fleet.taskForceParams.v2", settingKey: "display.task_force_params.v2", parse: parseJson },
  { legacyKey: "ava:shell-theme", settingKey: "display.shell_terminal_theme", parse: parseEnum(["system", "light", "dark"]), default: "system" },
  // Drop-only: superseded/dead localStorage keys — never read, just clean up.
  { legacyKey: "ava.sidebar.flatSort", settingKey: "" },
  { legacyKey: "ava.fleet.taskForceParams", settingKey: "" },
  // The Thinking/Code/Output "collapse by default" toggles they fed were
  // removed as dead (display.show_reasoning/show_code/show_output had no
  // reader) — drop any leftover pre-migration values instead of writing them
  // to a setting that no longer exists.
  { legacyKey: "ava:show-reasoning", settingKey: "" },
  { legacyKey: "ava:show-code", settingKey: "" },
  { legacyKey: "ava:show-output", settingKey: "" },
];

// The spawn-composer defaults were persisted by zustand as a single
// `ava-spawn-prefs` blob (`{state:{spawnModel,spawnPreset,spawnReasoningEffort},
// version}`), not one key per value. Pull each string out into its behavior.*
// setting, then drop the blob.
const SPAWN_PREFS_KEY = "ava-spawn-prefs";
const SPAWN_PREFS_MAP: Record<string, string> = {
  spawnModel: "behavior.spawn_model",
  spawnPreset: "behavior.spawn_preset",
  spawnReasoningEffort: "behavior.spawn_reasoning_effort",
};

async function migrateSpawnPrefs(write: WriteSetting): Promise<void> {
  let raw: string | null;
  try {
    raw = localStorage.getItem(SPAWN_PREFS_KEY);
  } catch {
    return;
  }
  if (raw === null) return;

  const toWrite: [string, string][] = [];
  try {
    const parsed = JSON.parse(raw) as { state?: Record<string, unknown> };
    const state = parsed.state ?? {};
    for (const [field, settingKey] of Object.entries(SPAWN_PREFS_MAP)) {
      const v = state[field];
      if (typeof v === "string" && v !== "") toWrite.push([settingKey, v]);
    }
  } catch {
    // malformed blob — nothing to write; fall through and drop it.
  }
  try {
    for (const [key, value] of toWrite) await write(key, value);
  } catch {
    // A write failed (e.g. 401 pre-login) — keep the blob and retry next load.
    return;
  }
  try {
    localStorage.removeItem(SPAWN_PREFS_KEY);
  } catch {
    // ignore
  }
}

/** Carry legacy localStorage preference values into the DB, then remove the
 *  keys. Each key is dropped ONLY after its write succeeds (or when no write is
 *  needed) — a failed write (401 before login, gateway hiccup) keeps the key so
 *  a later load retries instead of losing the value. Idempotent: after every key
 *  is migrated they are gone and it does nothing. Pure (takes the writer) so it
 *  is unit-testable. */
export async function migrateLegacyLocalStorageSettings(write: WriteSetting): Promise<void> {
  await migrateSpawnPrefs(write);
  for (const entry of SETTINGS_MIGRATIONS) {
    let raw: string | null;
    try {
      raw = localStorage.getItem(entry.legacyKey);
    } catch {
      // Storage unavailable (private mode / SSR / test without --localstorage-file).
      return;
    }
    if (raw === null) continue;

    if (entry.settingKey !== "") {
      const value = entry.parse ? entry.parse(raw) : raw;
      const isDefault = "default" in entry && JSON.stringify(value) === JSON.stringify(entry.default);
      if (value !== undefined && !isDefault) {
        try {
          await write(entry.settingKey, value);
        } catch {
          // Write failed — keep the key for a later retry, don't drop the value.
          continue;
        }
      }
    }
    try {
      localStorage.removeItem(entry.legacyKey);
    } catch {
      // ignore — the write above already landed
    }
  }
}

/** Runs the one-time migration once, AFTER the user is authenticated. Mounted
 *  above AuthGuard (in providers), so gating on auth avoids firing PUTs that
 *  would 401 on the login page — and each legacy key is dropped only once its
 *  write lands, so a failed write never loses the value. Renders nothing. */
export function SettingsMigration(): null {
  const { status } = useAuth();
  const queryClient = useQueryClient();
  const done = useRef(false);

  useEffect(() => {
    if (done.current) return;
    if (status !== "authenticated") return;
    done.current = true;
    void migrateLegacyLocalStorageSettings(async (key, value) => {
      // Throws on a non-2xx response → the migration keeps the legacy key.
      await api.putSetting(key, value);
      // Reflect the migrated value in the cache so it shows without a refetch.
      queryClient.setQueryData<Record<string, unknown>>(SETTINGS_QUERY_KEY, (old) => ({
        ...(old ?? USER_SETTING_DEFAULTS),
        [key]: value,
      }));
    });
  }, [status, queryClient]);

  return null;
}
