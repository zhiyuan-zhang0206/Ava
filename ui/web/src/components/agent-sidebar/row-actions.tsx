import { Loader2, RotateCw } from "lucide-react";
import { useTranslations } from "next-intl";

import type { AgentRow } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import type { PendingAction } from "../agents/agent-row";

// On-row feedback for in-flight actions (resurrect / restart / terminate /
// compact / force-expire). Ending a takeover is a context-menu-only action
// (agent-row.tsx's "End external takeover session" item) — no on-row button.
export function RowActions({
  agent,
  pending,
  onResurrect,
}: {
  agent: AgentRow;
  pending: PendingAction | undefined;
  onResurrect: () => void;
}) {
  const t = useTranslations("agentRow");
  // Leave room for the ScrollArea vertical scrollbar (10px).
  const wrapperCls =
    "absolute right-3 top-1/2 -translate-y-1/2 flex items-center gap-0.5";

  if (agent.status === "terminated") {
    return (
      <div className={wrapperCls}>
        {pending === "resurrecting" ? (
          <Spinner color="text-emerald-500" />
        ) : (
          <button
            type="button"
            onClick={onResurrect}
            disabled={pending !== undefined}
            className="p-0.5 rounded hover:bg-emerald-500/20 hover:text-emerald-500 text-muted-foreground disabled:opacity-30"
            aria-label={t("resurrectConfirm", { id: agent.agent_id })}
          >
            <RotateCw className="size-3" />
          </button>
        )}
      </div>
    );
  }

  if (pending === "restarting") {
    return (
      <div className={wrapperCls}>
        <Spinner color="text-emerald-500" />
      </div>
    );
  }
  if (pending === "terminating" || pending === "expiring") {
    return (
      <div className={wrapperCls}>
        <Spinner color="text-destructive" />
      </div>
    );
  }
  if (pending === "compacting") {
    return (
      <div className={wrapperCls}>
        <Spinner color="text-amber-500" />
      </div>
    );
  }

  return null;
}

function Spinner({ color }: { color: string }) {
  return (
    <span className="p-0.5 inline-flex items-center justify-center">
      <Loader2 className={cn("size-3 animate-spin", color)} />
    </span>
  );
}
