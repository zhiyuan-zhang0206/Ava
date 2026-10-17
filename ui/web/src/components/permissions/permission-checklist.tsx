"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Loader2, RefreshCw, ShieldCheck } from "lucide-react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api } from "@/lib/transport/api";
import { useStore } from "@/lib/state/store";
import { errMsg } from "@/lib/contracts/errors";
import { FLEX } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";
import type { HostPermissionsPayload, PermissionItemRow } from "@/lib/contracts/types";

import { HOST_PERMISSIONS_QUERY_KEY, useHostPermissions } from "./use-host-permissions";

const STATE_TONE: Record<string, string> = {
  granted: "text-emerald-600 dark:text-emerald-400",
  denied: "text-destructive",
  not_determined: "text-amber-600 dark:text-amber-400",
  unknown: "text-muted-foreground",
};

export function PermissionChecklist() {
  const t = useTranslations("control.permissions");
  const queryClient = useQueryClient();
  const showToast = useStore((s) => s.showToast);
  const { data, isLoading, isError, error } = useHostPermissions();

  const apply = (payload: HostPermissionsPayload) =>
    queryClient.setQueryData(HOST_PERMISSIONS_QUERY_KEY, payload);

  const recheck = useMutation({ mutationFn: () => api.recheckHostPermissions(), onSuccess: apply });
  const request = useMutation({
    mutationFn: () => api.requestHostPermissions(),
    onSuccess: (payload) => {
      apply(payload);
      showToast(payload.headline);
    },
    onError: (err) => showToast(errMsg(err)),
  });
  const defer = useMutation({
    mutationFn: () => api.deferHostPermissions(),
    onSuccess: (payload) => {
      apply(payload);
      showToast(t("deferred"));
    },
    onError: (err) => showToast(errMsg(err)),
  });

  if (isLoading) {
    return (
      <p className={cn("items-center gap-2 text-sm text-muted-foreground", FLEX)}>
        <Loader2 className="size-4 animate-spin" aria-hidden /> {t("loading")}
      </p>
    );
  }
  if (isError || !data) {
    return (
      <p className="text-sm text-destructive">
        {t("unreadable")} {error ? errMsg(error) : ""}
      </p>
    );
  }
  if (data.state === "not_applicable") {
    return <p className="text-sm text-muted-foreground">{t("notApplicable")}</p>;
  }

  const busy = recheck.isPending || request.isPending || defer.isPending;
  return (
    <div className="space-y-4">
      <div className={cn("items-center gap-2", FLEX)}>
        <ShieldCheck className={cn("size-4", data.state === "complete" ? "text-emerald-600" : "text-amber-600")} aria-hidden />
        <span className="text-sm font-medium">{data.headline}</span>
        <div className={cn("items-center gap-2", FLEX)} />
        <span className="text-xs text-muted-foreground">
          {t("checkedAt", { at: new Date(data.checked_at).toLocaleTimeString() })}
        </span>
        <Button variant="outline" size="sm" onClick={() => recheck.mutate()} disabled={busy}>
          <RefreshCw className={cn("size-3.5", recheck.isPending && "animate-spin")} aria-hidden />
          {t("recheck")}
        </Button>
        <Button
          size="sm"
          onClick={() => request.mutate()}
          disabled={busy || data.required_missing === 0}
          title={t("requestHint")}
        >
          {request.isPending && <Loader2 className="size-3.5 animate-spin" aria-hidden />}
          {t("request")}
        </Button>
        <Button variant="ghost" size="sm" onClick={() => defer.mutate()} disabled={busy || data.required_missing === 0}>
          {t("later")}
        </Button>
      </div>

      {data.diagnostic && <p className="text-sm text-muted-foreground">{data.diagnostic}</p>}

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-56">{t("columnPermission")}</TableHead>
            <TableHead className="w-28">{t("columnState")}</TableHead>
            <TableHead>{t("columnWhy")}</TableHead>
            <TableHead className="w-64">{t("columnFix")}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {data.items.map((row) => (
            <Row key={row.id} row={row} />
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function Row({ row }: { row: PermissionItemRow }) {
  const t = useTranslations("control.permissions");
  return (
    <TableRow>
      <TableCell className="align-top">
        <div className="font-medium">{row.title}</div>
        {!row.required && <div className="text-xs text-muted-foreground">{t("optional")}</div>}
      </TableCell>
      <TableCell className={cn("align-top text-xs font-medium", STATE_TONE[row.state])}>
        {row.state_label}
        <div className="text-[11px] font-normal text-muted-foreground">{row.mode}</div>
      </TableCell>
      <TableCell className="align-top text-xs text-muted-foreground">
        {row.why}
        <div className="mt-1">{row.verify}</div>
      </TableCell>
      <TableCell className="align-top text-xs">
        {row.blocking ? (
          <div className="space-y-1">
            <a
              className={cn("items-center gap-1 underline-offset-2 hover:underline", FLEX)}
              href={row.jump}
            >
              <ExternalLink className="size-3" aria-hidden /> {t("openSettings")}
            </a>
            {row.restart && <div className="text-muted-foreground">{row.restart}</div>}
          </div>
        ) : (
          <span className="text-muted-foreground">{t("nothingToDo")}</span>
        )}
      </TableCell>
    </TableRow>
  );
}
