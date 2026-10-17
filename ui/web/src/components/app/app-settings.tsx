"use client";

import { Smartphone } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState, useSyncExternalStore } from "react";

import { Button } from "@/components/ui/button";
import { FLEX, FLEX_COL } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

interface TauriInternals {
  invoke: (command: string, args?: unknown) => Promise<unknown>;
}

export interface AppBridge {
  openSettings: () => Promise<unknown>;
}

let lastInternals: TauriInternals | null = null;
let lastBridge: AppBridge | null = null;

export function readAppBridge(): AppBridge | null {
  if (typeof window === "undefined") return null;
  const internals = (window as unknown as { __TAURI_INTERNALS__?: TauriInternals })
    .__TAURI_INTERNALS__;
  if (!internals || typeof internals.invoke !== "function") {
    lastInternals = null;
    lastBridge = null;
    return null;
  }
  if (internals !== lastInternals) {
    lastInternals = internals;
    lastBridge = { openSettings: () => internals.invoke("app_open_settings") };
  }
  return lastBridge;
}

function subscribeToNothing(): () => void {
  return () => undefined;
}

function serverSnapshot(): AppBridge | null {
  return null;
}

export function useAppBridge(): AppBridge | null {
  return useSyncExternalStore(subscribeToNothing, readAppBridge, serverSnapshot);
}

export function AppSettingsEntry({ bridge }: { bridge: AppBridge }) {
  const t = useTranslations("control");
  const [failed, setFailed] = useState(false);
  return (
    <div className={cn("items-start gap-2", FLEX, FLEX_COL)}>
      <Button
        size="sm"
        variant="secondary"
        onClick={() => {
          setFailed(false);
          void bridge.openSettings().catch(() => setFailed(true));
        }}
      >
        <Smartphone className="size-4" aria-hidden />
        {t("appOpenSettings")}
      </Button>
      {failed ? (
        <p role="status" className="text-xs text-destructive">
          {t("appOpenSettingsFailed")}
        </p>
      ) : null}
    </div>
  );
}
