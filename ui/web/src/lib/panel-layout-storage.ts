// Storage for react-resizable-panels v4 `useDefaultLayout`.
//
// v4 stores a group's layout as a single Layout map keyed by panel id under
// `react-resizable-panels:<id>`:
//
//   { "panel-sidebar": 30, "panel-main": 70 }
//
// The library's own `storage = localStorage` default is not usable here: the
// components that call the hook server-render too, and the server has no
// localStorage (the default would throw there). This wrapper keeps the
// browser-only access in one guarded place.

import type { LayoutStorage } from "react-resizable-panels";

function browserStorage(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    // Storage access can throw (e.g. blocked third-party storage).
    return null;
  }
}

/** SSR-safe LayoutStorage for `useDefaultLayout`. */
export function panelLayoutStorage(): LayoutStorage {
  return {
    getItem(key) {
      const storage = browserStorage();
      return storage === null ? null : storage.getItem(key);
    },
    setItem(key, value) {
      browserStorage()?.setItem(key, value);
    },
  };
}
