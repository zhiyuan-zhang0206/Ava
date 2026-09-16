"use client";

// Hidden pages suspend live subscriptions and selected timeline reads. Returning
// to visibility reopens the streams and repairs their visible read models.

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
