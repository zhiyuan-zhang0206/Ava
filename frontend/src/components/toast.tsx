"use client";

// ToastHost — the ONE renderer for the store's toast slot, mounted at the
// app root (Providers) so an error toast from ANY route (/control, /fleet,
// /insights, …) is actually visible. Previously the renderer lived only in
// app/page.tsx, so every non-Home route's showToast call vanished silently.
// role="alert" makes the toast reach a screen reader immediately (errors
// visible to every perception channel — R4 invariant 3).

import { useStore } from "@/lib/store";

export function ToastHost() {
  const toast = useStore((s) => s.toast);
  if (!toast) return null;
  return (
    <div
      role="alert"
      className="fixed bottom-4 right-4 bg-destructive text-destructive-foreground px-4 py-2 rounded-md shadow-lg text-sm z-50"
    >
      {toast}
    </div>
  );
}
