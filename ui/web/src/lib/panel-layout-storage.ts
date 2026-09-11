// Bridge across the react-resizable-panels v3 -> v4 storage change.
//
// v3 persisted a group under `react-resizable-panels:<autoSaveId>` as a record
// of *shapes* — one entry per set of panel constraints/order — each holding a
// plain percentage array:
//
//   { "<shape key>": { expandToSizes: { ... }, layout: [30, 70] } }
//
// v4 (useDefaultLayout) stores a single Layout map keyed by PANEL ID under
// `react-resizable-panels:<id>`:
//
//   { "panel-sidebar": 30, "panel-main": 70 }
//
// The library converts a legacy record itself only when the shape key equals
// the panel ids (readLegacyLayout); our panels were never given ids, so without
// this bridge a first v4 mount would read nothing, paint the defaults, and —
// with persistence enabled — overwrite the user's dragged split with them. The
// stored layout array is in panel order, so the conversion maps it onto the
// current panel ids. It only fires when exactly one stored shape matches the
// panels rendered right now; an ambiguous record is left to the library (which
// degrades to the defaults rather than guessing).

import type { LayoutStorage } from "react-resizable-panels";

interface LegacyShape {
  layout: number[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function toLegacyShape(entry: unknown, panelCount: number): LegacyShape | null {
  if (!isRecord(entry)) return null;
  const layout = entry.layout;
  if (!Array.isArray(layout) || layout.length !== panelCount) return null;
  if (!layout.every((size) => typeof size === "number")) return null;
  return { layout };
}

/**
 * Normalize one stored value for `useDefaultLayout`:
 * - `null` when the value is unparseable (never hand the library JSON it would
 *   crash re-parsing);
 * - a converted Layout map when exactly one legacy shape matches `panelIds`;
 * - the raw value otherwise (already v4, or a shape the library can judge).
 */
export function normalizeStoredPanelLayout(
  raw: string,
  panelIds: readonly string[],
): string | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isRecord(parsed)) return null;

  const entries = Object.values(parsed);
  if (entries.every((entry) => typeof entry === "number")) {
    return raw;
  }

  const matches = entries
    .map((entry) => toLegacyShape(entry, panelIds.length))
    .filter((shape): shape is LegacyShape => shape !== null);
  if (matches.length !== 1) return raw;

  const layout: Record<string, number> = {};
  panelIds.forEach((panelId, index) => {
    layout[panelId] = matches[0].layout[index];
  });
  return JSON.stringify(layout);
}

function browserStorage(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    // Storage access can throw (e.g. blocked third-party storage).
    return null;
  }
}

/**
 * LayoutStorage for `useDefaultLayout` that reads through the v3 -> v4 bridge.
 * Also SSR-safe: the hook is called by components that server-render too, and
 * the server has no localStorage (the library's own `storage = localStorage`
 * default would throw there).
 */
export function panelLayoutStorage(panelIds: readonly string[]): LayoutStorage {
  return {
    getItem(key) {
      const storage = browserStorage();
      if (storage === null) return null;
      const raw = storage.getItem(key);
      return raw === null ? null : normalizeStoredPanelLayout(raw, panelIds);
    },
    setItem(key, value) {
      browserStorage()?.setItem(key, value);
    },
  };
}
