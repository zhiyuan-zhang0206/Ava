import { Loader2, PowerOff, RotateCw } from "lucide-react";
import { useTranslations } from "next-intl";

import { FLEX } from "@/lib/layout";
import type { AgentRow } from "@/lib/types";
import { cn } from "@/lib/utils";
import type { PendingAction } from "../agent-row";

// On-row feedback and direct actions. A takeover can be ended without the
// context menu; the button stays visible on touch screens.
export function RowActions({
  agent,
  pending,
  onResurrect,
  onForceExpire,
}: {
  agent: AgentRow;
  pending: PendingAction | undefined;
  onResurrect: () => void;
  onForceExpire: () => void;
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

  if (agent.open_impersonation_session_id != null && pending === undefined) {
    return (
      <div className={wrapperCls}>
        <button
          type="button"
          onClick={onForceExpire}
          className={cn(FLEX, "items-center gap-0.5 rounded px-1 py-0.5 text-[10px] text-destructive hover:bg-destructive/10")}
          aria-label={t("forceExpire")}
          title={t("forceExpire")}
        >
          <PowerOff className="size-3" /> {t("endTakeover")}
        </button>
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
