"use client";

// Live cluster status: service readiness, machine roster, and reported release
// observations. Visibility bounds the status poll; this page has no deployment
// mutation or source-checkout update preflight.

import { useQuery } from "@tanstack/react-query";
import { Loader2, Server } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ComponentType, type ReactNode } from "react";

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api } from "@/lib/api";
import { formatRelative } from "@/lib/time";
import type { ClusterPanel, MachineStatus, SystemStatus } from "@/lib/types";
import { SYSTEM_STATUS_QUERY_KEY } from "@/lib/use-cluster-health";

import { useSectionVisible } from "@/app/control/_visibility";
import { FLEX } from "@/lib/layout";
import { cn } from "@/lib/utils";

export default function StatusPage() {
  const t = useTranslations("insights.status");
  const visible = useSectionVisible();
  // Shares SYSTEM_STATUS_QUERY_KEY with the other /api/status observers (the
  // sidebar SpawnButton, machine badges, Config) — one key, one poll loop per
  // route. The app root deliberately does NOT poll /api/status (the health
  // hook watches /api/cluster/status only — see use-cluster-health.ts), so
  // this visibility-gated 15s interval is what keeps the Status view fresh
  // on this route.
  const { data, isLoading, error } = useQuery({
    queryKey: SYSTEM_STATUS_QUERY_KEY,
    queryFn: api.getSystemStatus,
    refetchInterval: 15_000,
    enabled: visible,
  });

  if (isLoading)
    return (
      <div className={cn("justify-center py-12", FLEX)}>
        <Loader2 className="size-6 animate-spin text-muted-foreground" />
      </div>
    );
  if (error && !data) {
    // Status auto-refetches every 15s, so a fetch miss is usually transient
    // (gateway restarting / momentary network). A cold failure gets a quiet
    // line; a failure WITH cached data keeps showing the data (stale-while-
    // error, Task #1051) instead of swapping the panel for an error page.
    return (
      <div className="p-8 text-center text-sm text-muted-foreground">
        {t("couldNotReach")}
      </div>
    );
  }
  if (!data) {
    return (
      <div className="p-8 text-center text-sm text-muted-foreground">
        {t("noData")}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <ServicesPanel data={data.cluster} />
      <GatewaySection data={data} />
    </div>
  );
}

// ── shared status verdicts ──
//
// One machine → one (label, tone) verdict, used by the gateway card and the
// runner table alike. Colors: green = online/running, amber = paused,
// red = error (offline / identity mismatch), muted = deliberate stop / unknown.

type StatusTone = "ok" | "warn" | "error" | "muted";

const TONE_TEXT: Record<StatusTone, string> = {
  ok: "text-green-600 dark:text-green-400",
  warn: "text-amber-600 dark:text-amber-400",
  error: "text-destructive",
  muted: "text-muted-foreground",
};

const TONE_DOT: Record<StatusTone, string> = {
  ok: "bg-green-500",
  warn: "bg-amber-500",
  error: "bg-destructive",
  muted: "bg-muted-foreground",
};

type MachineVerdictLabel =
  | "identityMismatch"
  | "statusUnknown"
  | "paused"
  | "running"
  | "stopped"
  | "offline";

function machineVerdict(m: MachineStatus): { label: MachineVerdictLabel; tone: StatusTone } {
  // identity_mismatch is a loud state: the probe reached an ops server that
  // answered under the WRONG machine_name, so this row's gateway_url points at
  // the wrong host. It outranks online/offline — never green.
  if (m.identity_mismatch) return { label: "identityMismatch", tone: "error" };
  if (m.online && m.paused === null) return { label: "statusUnknown", tone: "warn" };
  if (m.online && m.paused === true) return { label: "paused", tone: "warn" };
  if (m.online && m.paused === false) return { label: "running", tone: "ok" };
  // stopped_at, set by `ava stop` and cleared by `ava start`, separates a
  // deliberate stop from a crash — the live probe alone can't.
  if (m.stopped_at != null) return { label: "stopped", tone: "muted" };
  return { label: "offline", tone: "error" };
}

function StatusText({ m, runningLabel }: { m: MachineStatus; runningLabel: "running" | "online" }) {
  const t = useTranslations("insights.status");
  const v = machineVerdict(m);
  // running_sha is the code the live process loaded; head_sha is its checkout.
  // A drift means the checkout advanced (pull / rollout) but the process was
  // not restarted — a node can read pin ✓ yet still run stale code.
  const codeDrift =
    m.running_sha != null && m.head_sha != null && m.running_sha !== m.head_sha;
  return (
    <span className={`inline-flex items-center gap-1.5 ${TONE_TEXT[v.tone]}`}>
      <span className={`size-1.5 rounded-full ${TONE_DOT[v.tone]}`} />
      {v.label === "running" ? t(runningLabel) : t(v.label)}
      {codeDrift && (
        <span
          className="text-amber-600 dark:text-amber-400"
          title={t("codeDrift", { running: m.running_sha?.slice(0, 7) ?? "?", head: m.head_sha?.slice(0, 7) ?? "?" })}
        >
          ⚠{m.running_sha?.slice(0, 7)}
        </span>
      )}
      {!codeDrift && m.on_pin === false && (
        <span
          className="text-amber-600 dark:text-amber-400"
          title={t("offPin", { head: m.head_sha?.slice(0, 7) ?? "?" })}
        >
          {t("offPinBadge")}
        </span>
      )}
      {/* The live settle hold names this host. Recorded by the lease when the
          rollout exited (this host acked its self-update and had not finished
          converging), NOT a live check — the off-pin / code-drift badges beside it
          are the live verdicts, and its absence does not prove convergence. */}
      {m.settle_waited_on && (
        <span
          className="text-amber-600 dark:text-amber-400"
          title={t("settleHold")}
        >
          {t("settleHoldBadge")}
        </span>
      )}
    </span>
  );
}

// Daemon liveness → one health verdict for the gateway card. Both pidfile
// probes alive = healthy; any dead = degraded; unknown probes = "—".
function healthVerdict(m: MachineStatus): { label: "none" | "degraded" | "healthy"; tone: StatusTone } {
  if (!m.online) return { label: "none", tone: "muted" };
  if ((m.serve_agent_runner && m.agent_host_online === false) || m.supervisor_online === false)
    return { label: "degraded", tone: "warn" };
  if ((!m.serve_agent_runner || m.agent_host_online === true) && m.supervisor_online === true)
    return { label: "healthy", tone: "ok" };
  return { label: "none", tone: "muted" };
}

function daemonMark(ok: boolean | null | undefined): string {
  return ok === true ? "✓" : ok === false ? "✗" : "?";
}

// "Up since" for a live host — a boot/announce stamp, not a heartbeat, so it is
// only ever rendered, never freshness-tested (see MachineStatus.up_since_at). An
// offline host's row states the stop instead, which IS a "last seen".
// Services and agent-runner observations.

function ServicesPanel({ data }: { data: ClusterPanel }) {
  const t = useTranslations("insights.status");
  const runners = data.machines.filter((m) => m.serve_agent_runner);

  return (
    <div id="status-services" className="scroll-mt-4">
      <h3 className="mb-2 text-sm font-semibold">{t("services")}</h3>

      <div className="mb-3 text-xs text-muted-foreground">
        {t("thisHost", {
          host: data.current_machine,
          capabilities: [
            data.current_serve_gateway ? t("gateway") : null,
            data.current_serve_agent_runner ? t("agentRunner") : null,
            data.current_serve_observability_station ? t("observabilityStation") : null,
          ].filter(Boolean).join(" + ") || t("noCapability"),
        })}
        {data.current_paused && (
          <span className="ml-1 text-amber-600 dark:text-amber-400">{t("pausedDetail")}</span>
        )}
        {data.cluster_target_sha && (
          <span className="ml-1">{t("pinnedTo", { sha: data.cluster_target_sha.slice(0, 7) })}</span>
        )}
        {/* Recorded since the pin existed and shown nowhere until now — without it a
            rollback presents as the pin simply moving to an older commit, with
            nothing saying that commit is the anchor the cluster fell back to. */}
        {data.cluster_last_known_good_sha && (
          <span
            className="ml-1"
            title={t("rollbackAnchor")}
          >
            {t("lastKnownGood", { sha: data.cluster_last_known_good_sha.slice(0, 7) })}
          </span>
        )}

      </div>



      {data.machines.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          {t("noHostStarted")}
        </p>
      ) : (
        runners.length > 0 && <AgentRunnersCard runners={runners} />
      )}
    </div>
  );
}

function GatewayCard({ m, currentMachine }: { m: MachineStatus; currentMachine: string }) {
  const t = useTranslations("insights.status");
  const health = healthVerdict(m);
  const upSince = m.online
    ? formatRelative(m.up_since_at)
    : m.stopped_at != null
      ? t("stoppedAt", { time: formatRelative(m.stopped_at) })
      : "—";
  return (
    <div className="rounded-md border border-border" data-testid={`gateway-card-${m.name}`}>
      <div className="border-b border-border px-3 py-2">
        <h4 className="text-sm font-semibold">{t("gatewayTitle")}</h4>
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-2 px-3 py-2.5 sm:grid-cols-4">
        <div>
          <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{t("host")}</div>
          <div className="mt-0.5 text-sm font-medium">
            {m.name}
            {m.is_staging && (
              <span className="ml-1.5 rounded-sm border border-amber-500/50 px-1 text-[10px] font-normal text-amber-600 dark:text-amber-400" title={t("stagingTitle")}>
                {t("staging")}
              </span>
            )}
            {m.name === currentMachine && (
              <span className="font-normal text-muted-foreground">{t("currentHost")}</span>
            )}
          </div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{t("status")}</div>
          <div className="mt-0.5 text-sm font-medium">
            <StatusText m={m} runningLabel="running" />
          </div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{t("health")}</div>
          <div className={`mt-0.5 text-sm font-medium ${TONE_TEXT[health.tone]}`} title={t("daemonHealth", { agentHost: daemonMark(m.agent_host_online), supervisor: daemonMark(m.supervisor_online) })}>
            {health.label === "none" ? "—" : t(health.label)}
          </div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{t("upSince")}</div>
          <div className="mt-0.5 text-sm font-medium">{upSince}</div>
        </div>
      </div>
    </div>
  );
}

// Every agent-runner service as one table: host / status / agents / up since.
function AgentRunnersCard({ runners }: { runners: MachineStatus[] }) {
  const t = useTranslations("insights.status");
  return (
    <div className="rounded-md border border-border" data-testid="agent-runners-card">
      <div className="border-b border-border px-3 py-2">
        <h4 className="text-sm font-semibold">{t("agentRunners")}</h4>
      </div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{t("host")}</TableHead>
            <TableHead>{t("status")}</TableHead>
            <TableHead>{t("agents")}</TableHead>
            <TableHead>{t("upSince")}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {runners.map((m) => (
            <TableRow key={m.name}>
              <TableCell className="font-medium">
                {m.name}
                {m.is_staging && (
                  <span className="ml-1.5 rounded-sm border border-amber-500/50 px-1 text-[10px] font-normal text-amber-600 dark:text-amber-400" title={t("stagingTitle")}>
                    {t("staging")}
                  </span>
                )}
              </TableCell>
              <TableCell className="text-xs">
                <StatusText m={m} runningLabel="online" />
              </TableCell>
              <TableCell className="tabular-nums">{m.agent_count}</TableCell>
              <TableCell className="text-xs text-muted-foreground">
                {m.online
                  ? formatRelative(m.up_since_at)
                  : m.stopped_at != null
                    ? t("stoppedAt", { time: formatRelative(m.stopped_at) })
                    : "—"}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

// Gateway host status plus gateway-only daemons (labeler, memory indexer).
// Per-host daemons (agent-host, watchdog) ride each machine's probe and feed
// the Gateway card's health verdict instead.
function GatewaySection({ data }: { data: SystemStatus }) {
  const t = useTranslations("insights.status");
  const gateways = data.cluster.machines.filter((m) => m.serve_gateway);
  const services = data.services.items;
  const cur = data.cluster.current_machine;
  return (
    <StatusSection id="status-gateway" icon={Server} title={t("gatewayTitle")} subtitle={`(${cur})`}>
      <div className="space-y-3">
        {gateways.map((m) => (
          <GatewayCard key={m.name} m={m} currentMachine={cur} />
        ))}
        {services.length === 0 ? (
          <p className="text-xs text-muted-foreground">{t("noServiceData")}</p>
        ) : (
          <div className="border border-border rounded-md overflow-x-auto">
            <Table className="[&_th]:border-r [&_th]:border-border [&_th:last-child]:border-r-0 [&_td]:border-r [&_td]:border-border [&_td:last-child]:border-r-0">
              <TableBody>
                {services.map((svc) => (
                  <TableRow key={svc.name}>
                    <TableCell className="w-full">
                      <span className="inline-flex items-center gap-2">
                        <span
                          className={`size-2 rounded-full ${
                            svc.online === true
                              ? "bg-green-500"
                              : svc.online === false
                                ? "bg-destructive"
                                : "bg-muted-foreground/30"
                          }`}
                        />
                        {svc.label}
                      </span>
                    </TableCell>
                    <TableCell className="text-right text-muted-foreground font-mono">
                      {svc.detail ?? (svc.pid != null ? t("pid", { pid: svc.pid }) : "—")}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </div>
    </StatusSection>
  );
}

// Unified chrome for the secondary status blocks: bordered card + icon +
// title + optional dim subtitle + optional right-aligned action.
function StatusSection({
  id,
  icon: Icon,
  title,
  subtitle,
  action,
  children,
}: {
  id?: string;
  icon: ComponentType<{ className?: string }>;
  title: ReactNode;
  subtitle?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div id={id} className="scroll-mt-4 rounded-md border border-border p-4">
      <div className={cn("items-center gap-2 mb-3", FLEX)}>
        <Icon className="size-4" />
        <h3 className="text-sm font-semibold">{title}</h3>
        {subtitle ? (
          <span className="text-xs text-muted-foreground">{subtitle}</span>
        ) : null}
        {action ? <div className="ml-auto">{action}</div> : null}
      </div>
      {children}
    </div>
  );
}
