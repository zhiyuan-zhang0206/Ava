"use client";

// Terminate open-tasks notice (task #3374) — the store-slot host mirroring
// ToastHost: the terminate mutation stamps `openTasksNotice` when the response
// reports still-open tasks, and this root-mounted renderer shows it. The dialog
// module is lazily imported so routes that never terminate keep the Radix
// Dialog payload out of their graph (same rule as the row's prompt dialog).

import dynamic from "next/dynamic";

import { useStore } from "@/lib/store";

const LazyNoticeDialog = dynamic(
  () =>
    import("@/components/open-tasks-notice-dialog").then(
      (module) => module.OpenTasksNoticeDialog,
    ),
  { loading: () => null },
);

export function OpenTasksNoticeHost() {
  const notice = useStore((s) => s.openTasksNotice);
  const dismiss = useStore((s) => s.dismissOpenTasksNotice);
  if (!notice) return null;
  return <LazyNoticeDialog notice={notice} onClose={dismiss} />;
}
