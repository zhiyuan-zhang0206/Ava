"use client";

// Window visibility — hidden tabs close their SSE streams: the per-origin
// HTTP/1.1 connection budget is 6, and three streams per visible page
// saturate it at two tabs. Every stream yields its slot while hidden and
// reopens on return; each stream's reopen repair (fold reconcile / poll
// snapshots) reconciles whatever was missed in between. Shared by both
// useEventStream Providers and use-alerts' AlertsProvider.

import { useEffect, useState } from "react";

export function useDocumentVisible(): boolean {
  const [isVisible, setIsVisible] = useState(
    () => typeof document === "undefined" || document.visibilityState === "visible",
  );
  useEffect(() => {
    const syncVisibility = () => setIsVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", syncVisibility);
    syncVisibility();
    return () => document.removeEventListener("visibilitychange", syncVisibility);
  }, []);
  return isVisible;
}
