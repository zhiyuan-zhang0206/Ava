"use client";

// The cross-machine Plugins / MCP inventory matrix.
//
// Read: GET /api/inventory returns an InventoryAggregate — every machine
// considered (the column set, plus an `unreachable` subset whose cells are
// unknown) and, per plugin / MCP server, its per-host state (present /
// enabled / can_enable / reason).
//
// Write: a cell click PUTs /api/inventory?machine=<m> a single-item delta
// (the targeted host only). enable-all / disable-all fan out one PUT per
// installed host. The PUT is atomic per host and returns InventoryWriteResult
// (per-item ok/reason + `applied`); on `applied === false` the rejection
// reason is surfaced via a toast.
//
// Plugins and MCP servers are two distinct settings-page sections
// (`PluginsInventory` / `McpInventory`), each a matrix over the same
// `/api/inventory` read (react-query dedupes the shared key; whichever section
// is on-screen drives the fetch). The default `InventoryPage` composes both for
// the bare `/control/inventory` route.
//
// Interactions per cell:
// - host unreachable -> muted "?" (no toggle)
// - item not present on the host -> muted "—" (not installed here)
// - present -> a ✓/✗ toggle reflecting `enabled`. A present-but-disabled cell
//   the host can't enable today (can_enable === false) is pre-greyed with its
//   reason as the title; an already-enabled cell stays clickable (you can
//   always turn OFF) even when can_enable is false.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Loader2, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback } from "react";

import { PackageDraftEntry } from "@/components/package-draft-entry";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api } from "@/lib/api";
import { errMsg } from "@/lib/errors";
import { useStore } from "@/lib/store";
import type {
  InventoryAggregate,
  InventoryItemAggregate,
  InventoryItemHostState,
  InventoryWriteResult,
} from "@/lib/types";

import { useSectionVisible } from "../_visibility";
import { FLEX } from "@/lib/layout";
import { cn } from "@/lib/utils";

const INVENTORY_QUERY_KEY = ["inventory"] as const;

// A single-item toggle on a single host. `machine` rides in the mutation
// variables (not a render closure) so a fan-out / racing refetch can't bind
// the wrong host — same fix the config page made.
interface ToggleVars {
  machine: string;
  kind: string;
  name: string;
  enabled: boolean;
}

// N of M: how many installed hosts have this item enabled, out of how many
// have it installed at all.
function summarize(hosts: Record<string, InventoryItemHostState>): {
  enabled: number;
  present: number;
} {
  let enabled = 0;
  let present = 0;
  for (const state of Object.values(hosts)) {
    if (!state.present) continue;
    present += 1;
    if (state.enabled) enabled += 1;
  }
  return { enabled, present };
}

interface InventoryController {
  data: InventoryAggregate | undefined;
  isLoading: boolean;
  error: unknown;
  busy: boolean;
  toggle: (vars: ToggleVars) => void;
  fanOut: (row: InventoryItemAggregate, machines: string[], target: boolean) => void;
}

// Shared query + write plumbing for both inventory sections. Gated to on-screen
// time (`enabled: visible`) so an off-screen Plugins/MCP section stops fetching.
function useInventoryController(): InventoryController {
  const t = useTranslations("inventory");
  const qc = useQueryClient();
  const showToast = useStore((s) => s.showToast);
  const visible = useSectionVisible();

  const { data, isLoading, error } = useQuery({
    queryKey: INVENTORY_QUERY_KEY,
    queryFn: api.getInventory,
    enabled: visible,
  });

  const toggle = useMutation({
    mutationFn: async ({ machine, kind, name, enabled }: ToggleVars) => {
      const body =
        kind === "plugin"
          ? { plugins: { [name]: enabled } }
          : { mcp_servers: { [name]: enabled } };
      return api.putInventory(body, machine);
    },
    onSuccess: (result: InventoryWriteResult) => {
      if (!result.applied) {
        // Atomic write rejected — surface the first item's reason.
        const reason =
          Object.values(result.plugin_results).find((r) => !r.ok)?.reason ??
          Object.values(result.mcp_results).find((r) => !r.ok)?.reason ??
          "rejected by host";
        showToast(t("changeNotApplied", { reason }));
      }
    },
    onError: (err: unknown) => {
      showToast(t("saveFailed", { error: errMsg(err) }));
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: INVENTORY_QUERY_KEY });
    },
  });

  // Fan out one PUT per installed host. For enable, skip hosts the item can't
  // be enabled on (can_enable === false) and hosts already in the target
  // state. Each call carries its own machine in the variables.
  const fanOut = useCallback(
    (row: InventoryItemAggregate, machines: string[], target: boolean) => {
      for (const m of machines) {
        // A machine column may be absent from a row's hosts map (e.g. an
        // unreachable host) — guard the lookup before reading state.
        const h = row.hosts[m] as InventoryItemHostState | undefined;
        if (!h?.present) continue;
        if (h.enabled === target) continue;
        if (target && h.can_enable === false) continue;
        toggle.mutate({ machine: m, kind: row.kind, name: row.name, enabled: target });
      }
    },
    [toggle],
  );

  return {
    data,
    isLoading,
    error,
    busy: toggle.isPending,
    toggle: (vars) => toggle.mutate(vars),
    fanOut,
  };
}

// One inventory kind (plugins or mcp servers) as a standalone matrix — resolves
// its rows from the shared aggregate and handles the tri-state. No heading: the
// enclosing settings section supplies it.
function InventoryKind({ kind }: { kind: "plugin" | "mcp" }) {
  const t = useTranslations("inventory");
  const ctrl = useInventoryController();

  // The install entry stays mounted through every state — the matrix only shows
  // what is already here, so a failed read must not block adding something new.
  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground">
        {t("intro", { noun: kind === "plugin" ? t("plugins") : t("mcpServers") })}
      </p>
      <PackageDraftEntry kind={kind} />
      <InventoryBody kind={kind} controller={ctrl} />
    </div>
  );
}

function InventoryBody({
  kind,
  controller,
}: {
  kind: "plugin" | "mcp";
  controller: InventoryController;
}) {
  const t = useTranslations("inventory");
  if (controller.isLoading) {
    return (
      <div className={cn("justify-center py-12", FLEX)}>
        <Loader2 className="size-6 animate-spin text-muted-foreground" />
      </div>
    );
  }
  if (controller.error) {
    return (
      <p className="text-sm text-muted-foreground">
        {t("couldntLoad")}
      </p>
    );
  }
  if (!controller.data) return <p className="text-sm text-muted-foreground">{t("noData")}</p>;

  const rows = kind === "plugin" ? controller.data.plugins : controller.data.mcp_servers;
  return <InventoryMatrix rows={rows} aggregate={controller.data} controller={controller} />;
}

/** Plugins matrix — a top-level settings section. */
export function PluginsInventory() {
  return <InventoryKind kind="plugin" />;
}

/** MCP servers matrix — a top-level settings section. */
export function McpInventory() {
  return <InventoryKind kind="mcp" />;
}

// The bare `/control/inventory` route: both matrices under their own small
// headings. The shell renders the two sections separately instead.
export default function InventoryPage() {
  const t = useTranslations("inventory");
  return (
    <div className="space-y-6">
      <section>
        <h2 className="text-xs uppercase tracking-wide text-muted-foreground mb-2">{t("plugins")}</h2>
        <PluginsInventory />
      </section>
      <section>
        <h2 className="text-xs uppercase tracking-wide text-muted-foreground mb-2">{t("mcpServers")}</h2>
        <McpInventory />
      </section>
    </div>
  );
}

function InventoryMatrix({
  rows,
  aggregate,
  controller,
}: {
  rows: InventoryItemAggregate[];
  aggregate: InventoryAggregate;
  controller: InventoryController;
}) {
  const t = useTranslations("inventory");
  const machines = aggregate.machines;
  const unreachable = new Set(aggregate.unreachable);

  if (rows.length === 0) {
    return <p className="text-xs text-muted-foreground">{t("none")}</p>;
  }

  return (
    <div className="border border-border rounded-md overflow-x-auto">
      <Table className="[&_th]:border-r [&_th]:border-border [&_th:last-child]:border-r-0 [&_td]:border-r [&_td]:border-border [&_td:last-child]:border-r-0">
        <TableHeader>
          <TableRow>
            <TableHead className="min-w-48">{t("item")}</TableHead>
            {machines.map((m) => (
              <TableHead key={m} className="text-center max-w-32 truncate">
                {m}
              </TableHead>
            ))}
            <TableHead className="text-right">{t("all")}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => (
            <InventoryRow
              key={row.name}
              row={row}
              machines={machines}
              unreachable={unreachable}
              busy={controller.busy}
              onToggle={controller.toggle}
              onFanOut={controller.fanOut}
            />
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function InventoryRow({
  row,
  machines,
  unreachable,
  busy,
  onToggle,
  onFanOut,
}: {
  row: InventoryItemAggregate;
  machines: string[];
  unreachable: Set<string>;
  busy: boolean;
  onToggle: (vars: ToggleVars) => void;
  onFanOut: (row: InventoryItemAggregate, machines: string[], target: boolean) => void;
}) {
  const { enabled, present } = summarize(row.hosts);

  return (
    <TableRow>
      <TableCell className="align-top min-w-48 max-w-sm whitespace-normal">
        <div className="space-y-0.5">
          <div className={cn("items-center gap-2 flex-wrap", FLEX)}>
            <span className="font-mono font-medium break-all">{row.name}</span>
            <span
              className="text-xs text-muted-foreground border border-border rounded px-1 whitespace-nowrap"
              data-testid={`summary-${row.name}`}
            >
              {enabled}/{present} enabled
            </span>
          </div>
          {row.description && (
            <p className="text-xs text-muted-foreground leading-snug [overflow-wrap:anywhere]">
              {row.description}
            </p>
          )}
        </div>
      </TableCell>
      {machines.map((m) => (
        <TableCell key={m} className="text-center align-top">
          <InventoryCell
            machine={m}
            row={row}
            host={row.hosts[m]}
            unreachable={unreachable.has(m)}
            busy={busy}
            onToggle={onToggle}
          />
        </TableCell>
      ))}
      <TableCell className="text-right align-top">
        <div className="inline-flex gap-1">
          <Button
            variant="outline"
            size="sm"
            disabled={busy || present === 0}
            data-testid={`enable-all-${row.name}`}
            onClick={() => onFanOut(row, machines, true)}
          >
            Enable all
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={busy || present === 0}
            data-testid={`disable-all-${row.name}`}
            onClick={() => onFanOut(row, machines, false)}
          >
            Disable all
          </Button>
        </div>
      </TableCell>
    </TableRow>
  );
}

function InventoryCell({
  machine,
  row,
  host,
  unreachable,
  busy,
  onToggle,
}: {
  machine: string;
  row: InventoryItemAggregate;
  host: InventoryItemHostState | undefined;
  unreachable: boolean;
  busy: boolean;
  onToggle: (vars: ToggleVars) => void;
}) {
  const t = useTranslations("inventory");
  if (unreachable) {
    return (
      <span className="text-muted-foreground" aria-label={t("hostUnreachable")}>
        ?
      </span>
    );
  }
  if (!host?.present) {
    return (
      <span className="text-muted-foreground" aria-label={t("notInstalled")}>
        —
      </span>
    );
  }

  // Can't turn ON something the host can't enable; but an enabled item can
  // always be turned OFF even when can_enable is false.
  const cantEnable = !host.enabled && host.can_enable === false;
  const title = cantEnable
    ? (host.reason ?? t("cantEnable"))
    : host.enabled
      ? t("clickDisable")
      : t("clickEnable");

  return (
    <Button
      variant={host.enabled ? "default" : "outline"}
      size="sm"
      className="size-7 p-0"
      disabled={busy || cantEnable}
      aria-pressed={host.enabled}
      aria-label={title}
      data-testid={`cell-${row.name}-${machine}`}
      onClick={() =>
        onToggle({
          machine,
          kind: row.kind,
          name: row.name,
          enabled: !host.enabled,
        })
      }
    >
      {host.enabled ? <Check className="size-3.5" /> : <X className="size-3.5" />}
    </Button>
  );
}
