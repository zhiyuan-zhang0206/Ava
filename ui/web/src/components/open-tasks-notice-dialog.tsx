"use client";

// The terminate open-tasks notice body (task #3374): shown after a terminate
// response carried `open_tasks` — advisory only (the termination request was
// already accepted), never auto-dismissed. Rows are the server's most recently
// updated tasks (already truncated to five); `more` counts the rest — the
// frontend does not truncate again.

import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { FLEX, MIN_W_0 } from "@/lib/layout";
import { formatRelativeTime } from "@/lib/sidebar";
import type { OpenTasksHint } from "@/lib/types";
import { cn } from "@/lib/utils";

export interface OpenTasksNoticeDialogProps {
  notice: OpenTasksHint;
  onClose: () => void;
}

export function OpenTasksNoticeDialog({ notice, onClose }: OpenTasksNoticeDialogProps) {
  const t = useTranslations("openTasksNotice");
  const tTask = useTranslations("fleet.task");
  const statusLabel = (status: string) =>
    status === "in_progress" ? tTask("status.inProgress") : tTask("status.ongoing");

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <DialogContent aria-describedby={undefined} className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{t("title", { count: notice.count })}</DialogTitle>
        </DialogHeader>
        <ul className="max-h-48 space-y-1 overflow-y-auto font-mono text-xs">
          {notice.tasks.map((task) => (
            <li key={task.id} className={cn("items-baseline gap-1.5", FLEX)} title={task.title}>
              <span className="shrink-0">{`#${task.id} |`}</span>
              <span className={cn("truncate", MIN_W_0)}>{task.title}</span>
              <span className="shrink-0">
                {`| ${statusLabel(task.status)} | ${formatRelativeTime(task.updated_at)}`}
              </span>
            </li>
          ))}
        </ul>
        {notice.more > 0 ? (
          <p className="text-muted-foreground text-xs">{t("more", { more: notice.more })}</p>
        ) : null}
        <DialogFooter>
          <Button size="sm" onClick={onClose}>
            {t("dismiss")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
