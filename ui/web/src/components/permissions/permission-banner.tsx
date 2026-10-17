"use client";

import { RefreshCw, ShieldAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { FLEX, FLEX_1, MIN_W_0 } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

import { useHostPermissions, isRestricted } from "./use-host-permissions";

export function PermissionBanner() {
  const t = useTranslations("control.permissions");
  const { data, isFetching, refetch } = useHostPermissions();
  if (!isRestricted(data) || !data) return null;

  return (
    <div
      role="status"
      className={cn(
        "z-30 shrink-0 items-center gap-3 border-b border-amber-500/40 bg-amber-500/10 px-4 py-1.5 text-sm text-amber-900 dark:text-amber-200",
        FLEX,
      )}
    >
      <ShieldAlert className="size-4 shrink-0" aria-hidden />
      <span className={cn("truncate", MIN_W_0)}>
        {data.headline} {t("missingCount", { missing: data.required_missing, total: data.required_total })}
      </span>
      <div className={cn(FLEX_1)} />
      <Button
        variant="ghost"
        size="sm"
        onClick={() => void refetch()}
        disabled={isFetching}
        title={t("recheck")}
      >
        <RefreshCw className={cn("size-3.5", isFetching && "animate-spin")} aria-hidden />
        {t("recheck")}
      </Button>
      <Link
        href="/control#permissions"
        className="rounded px-2 py-1 text-xs font-medium underline-offset-2 hover:underline"
      >
        {t("openChecklist")}
      </Link>
    </div>
  );
}
