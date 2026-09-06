"use client";

// /insights#status — live cluster status. Services renders the cluster-wide
// Update / Restart actions and agent-runners table; Gateway combines the
// gateway card (host / status / health / up since) with its daemon list. The
// Resources block was removed 2026-08-24 because Grafana's "Host & data plane"
// row covers per-host resource charts. (The per-agent session tree was removed
// 2026-08-05 per user ruling — the Grafana embed shows runner health now.)
//
// Rendered as a section of the vertical Insights page; `useSectionVisible`
// starts the 15s status poll on first paint and pauses it once the Status
// section scrolls off-screen, so it doesn't keep hitting the gateway while
// unseen. The update check (a remote `git fetch` on the gateway) is NOT on an
// interval: it fetches when the section comes on screen and on the explicit
// re-check button — an update is a human-paced action, not a live signal.
// Also usable as the bare `/insights/status` route (no provider ⇒ visible
// defaults true).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, RefreshCw, RotateCcw, Server } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ComponentType, type ReactNode } from "react";

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
import { formatRelative } from "@/lib/time";
import type { ClusterPanel, ClusterUpdateCheck, MachineStatus, SystemStatus } from "@/lib/types";
import { CLUSTER_STATUS_QUERY_KEY, SYSTEM_STATUS_QUERY_KEY } from "@/lib/use-cluster-health";

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

type MachineVerdictLabel = "identityMismatch" | "statusUnknown" | "paused" | "running" | "stopped" | "offline";

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
  if (m.restarter_online === false || m.watchdog_online === false)
    return { label: "degraded", tone: "warn" };
  if (m.restarter_online === true && m.watchdog_online === true)
    return { label: "healthy", tone: "ok" };
  return { label: "none", tone: "muted" };
}

function daemonMark(ok: boolean | null | undefined): string {
  return ok === true ? "✓" : ok === false ? "✗" : "?";
}

// "Up since" for a live host — a boot/announce stamp, not a heartbeat, so it is
// only ever rendered, never freshness-tested (see MachineStatus.up_since_at). An
// offline host's row states the stop instead, which IS a "last seen".
// ── Services: agent-runner table + cluster-wide actions ──

function ServicesPanel({ data }: { data: ClusterPanel }) {
  const t = useTranslations("insights.status");
  const visible = useSectionVisible();
  const showToast = useStore((s) => s.showToast);
  const queryClient = useQueryClient();
  const isGateway = data.current_serve_gateway;

  // Preflight check — drives the Update button's "no updates" state. Gateway
  // only (the endpoint 400s elsewhere); the frontend always runs on the
  // gateway, but gate defensively. No interval: each call runs a remote
  // `git fetch` on the gateway, and "commits behind origin" only changes at
  // human pace — so fetch when the section comes on screen (staleTime bounds
  // re-entry churn), on the explicit re-check button below, and via the
  // invalidation after an update is triggered.
  const check = useQuery({
    queryKey: ["cluster-update-check"],
    queryFn: api.checkClusterUpdate,
    staleTime: 5 * 60_000,
    enabled: isGateway && visible,
  });
  const behind = check.data?.behind;
  const needsReplay = check.data?.needs_replay === true;
  const noUpdates = behind === 0 && !needsReplay;
  const restartSides = (details: ClusterUpdateCheck) => {
    const sides = [
      details.frontend_changed ? t("frontend") : null,
      details.backend_changed ? t("backend") : null,
    ].filter((side): side is string => side != null);
    return sides.length > 0 ? sides.join(" + ") : t("nothing");
  };

  const inFlightMsg = (msg: string) =>
    msg.includes("409") ||
    msg.toLowerCase().includes("in progress") ||
    msg.toLowerCase().includes("in flight");

  // The detached rollout/restart session is alive by the time the trigger
  // POST returns, so refetching status immediately picks up
  // `current_orchestration` and disables the actions with no poll-interval gap a
  // second click could slip through.
  const refreshClusterState = () => {
    // Refresh this view's roster (/api/status) AND the app-root "updating"
    // banner's snapshot (/api/cluster/status — the health hook's only poll)
    // so both pick up `current_orchestration` immediately, with no
    // poll-interval gap a second click could slip through.
    void queryClient.invalidateQueries({ queryKey: SYSTEM_STATUS_QUERY_KEY });
    void queryClient.invalidateQueries({ queryKey: CLUSTER_STATUS_QUERY_KEY });
    void queryClient.invalidateQueries({ queryKey: ["cluster-update-check"] });
  };

  const update = useMutation({
    mutationFn: api.triggerClusterRollout,
    onSuccess: (result) => {
      refreshClusterState();
      showToast(
        t("rolloutStarted", { session: result.session, log: result.log }),
      );
    },
    onError: (err: unknown) => {
      const msg = errMsg(err);
      showToast(
        inFlightMsg(msg)
          ? t("rolloutInFlight")
          : t("rolloutFailed", { message: msg }),
      );
    },
  });

  const restart = useMutation({
    mutationFn: api.triggerClusterRestart,
    onSuccess: (result) => {
      refreshClusterState();
      showToast(
        t("restartStarted", { session: result.session, log: result.log }),
      );
    },
    onError: (err: unknown) => {
      const msg = errMsg(err);
      showToast(
        inFlightMsg(msg)
          ? t("restartInFlight")
          : t("restartFailed", { message: msg }),
      );
    },
  });

  // A rollout/restart runs for minutes in a detached session after the request
  // returns. `current_orchestration` (the live orchestration session) is the
  // durable in-flight signal — it flips true the moment the session spawns and
  // stays true for the whole run, so the actions stay disabled across it (not just
  // for the POST blink). `current_paused` is kept as a secondary guard; firing a
  // second rollout into an active one just 409s either way.
  const orchestration = data.current_orchestration;
  const updating = update.isPending || orchestration === "rollout" || orchestration === "update";
  const restarting = restart.isPending || orchestration === "restart";
  const busy = updating || restarting || data.current_paused;

  const onUpdate = () => {
    if (noUpdates) return;
    // Native confirm — cluster-wide restart is irreversible-in-progress; one
    // misclick stops every agent. Native dialog is mobile-friendly, blocks
    // the event loop until decided, and adds no headless-component baggage.
    const sides = check.data
      ? needsReplay
        ? t("replaySides")
        : t("restartSides", { sides: restartSides(check.data) })
      : "";
    const n = needsReplay
      ? t("replayRequired")
      : typeof behind === "number"
        ? t("commitsBehind", { count: behind })
        : "";
    const ok = window.confirm(t("rolloutConfirm", { behind: n, sides }));
    if (ok) update.mutate();
  };

  const onRestart = () => {
    const ok = window.confirm(t("restartConfirm"));
    if (ok) restart.mutate();
  };

  const runners = data.machines.filter((m) => m.serve_agent_runner);

  return (
    <div id="status-services" className="scroll-mt-4">
      <div className={cn("mb-2 items-center justify-between", FLEX)}>
        <h3 className="text-sm font-semibold">{t("services")}</h3>
        <div className={cn("items-center gap-2", FLEX)}>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={onRestart}
            disabled={busy}
          >
            {restarting ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <RotateCcw className="size-3.5" />
            )}
            <span className="ml-1.5">{restarting ? t("restarting") : t("restart")}</span>
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={onUpdate}
            disabled={busy || !isGateway || noUpdates}
          >
            {updating ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <RefreshCw className="size-3.5" />
            )}
            <span className="ml-1.5">
              {updating
                ? t("updating")
                : needsReplay
                  ? t("replayUpdate")
                  : noUpdates
                    ? t("upToDate")
                    : behind
                      ? t("updateWithCount", { count: behind })
                      : t("update")}
            </span>
          </Button>
        </div>
      </div>

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
        {isGateway && (
          <span className="ml-2">
            {orchestration ? (
              <span className="inline-flex items-center gap-1.5 text-amber-600 dark:text-amber-400">
                <Loader2 className="size-3 animate-spin" />
                {orchestration === "restart"
                  ? t("restartInProgress")
                  : t("rolloutInProgress")}
              </span>
            ) : check.isLoading ? (
              <span>{t("checkingUpdates")}</span>
            ) : check.error ? (
              <span>{t("checkUnavailable")}</span>
            ) : needsReplay ? (
              <span className="text-amber-600 dark:text-amber-400">{t("replayRequired")}</span>
            ) : noUpdates ? (
              <span className="text-green-600 dark:text-green-400">{t("upToDateOrigin")}</span>
            ) : check.data ? (
              <span className="text-amber-600 dark:text-amber-400">
                {t("updateRestarts", { count: check.data.behind, sides: restartSides(check.data) })}
              </span>
            ) : null}
            {/* Explicit re-check — the update check has no poll interval
                (it runs a remote `git fetch`), so this button is how the
                verdict refreshes without leaving the section. */}
            {!orchestration && (
              <button
                type="button"
                onClick={() => void check.refetch()}
                disabled={check.isFetching}
                aria-label={t("checkUpdates")}
                title={t("checkUpdatesNow")}
                className="ml-1 inline-flex rounded p-0.5 align-middle text-muted-foreground hover:text-foreground disabled:opacity-50"
              >
                <RefreshCw className={cn("size-3", check.isFetching && "animate-spin")} />
              </button>
            )}
          </span>
        )}
      </div>

      <LastUpdateBanner record={data.last_update ?? null} />

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

// The gateway service as one card: host / status / health / last seen.
// A failed rollout used to reach this page only as a COLOUR: the amber pin/head
// mismatch a few lines up. That mismatch is equally produced by a node that missed
// a rollout, by a checkout that moved without a restart, and by a rollout that
// failed and rolled back — so on 2026-07-30 the operator was left to work out which
// (#1012). This states the fact in a sentence, from the record the rollout itself
// wrote, and says nothing at all when the last update succeeded: a permanent
// "last update: ok" line is one people stop reading, and this is the line that has
// to be read the one time it appears.
function LastUpdateBanner({ record }: { record: ClusterPanel["last_update"] }) {
  const t = useTranslations("insights.status");
  if (!record?.failed) return null;
  const target = record.target_sha ? t("target", { sha: record.target_sha.slice(0, 7) }) : "";
  const when = record.started_at ? new Date(record.started_at).toLocaleString() : null;
  // How stale the failure is changes what to do with it: minutes old is a live
  // incident, days old is a cluster nobody has updated since.
  const age = record.started_at ? formatRelative(record.started_at) : null;
  // A recovered update failed and then put the cluster back on a commit that
  // works. It still has to be stated — 2026-07-30 is the incident where a silent
  // recovery left the operator reading a sha mismatch as a live fault — but it is
  // not the same call to action as a failure nobody has handled, so it renders
  // amber rather than destructive.
  const recovered = record.outcome === "recovered";
  return (
    <div
      role="alert"
      className={
        recovered
          ? "mb-3 rounded-md border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-xs"
          : "mb-3 rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs"
      }
    >
      <p className={recovered ? "font-semibold text-amber-600" : "font-semibold text-destructive"}>
        {t("lastUpdateFailed", { target })}
        {recovered ? t("clusterRecovered") : ""}
        {when ? t("started", { when, age: age ? t("ageSuffix", { age }) : "" }) : ""}
      </p>
      <p className="mt-1 text-muted-foreground">
        {record.outcome === "orphaned"
          ? t("orchestrationDied")
          : record.failing_step
            ? t("stoppedAtStep", { step: record.failing_step })
            : t("endedOutcome", { outcome: record.outcome })}{" "}
        {recovered
          ? t("recoveredDetail")
          : record.pin_advanced
            ? t("pinAdvancedDetail")
            : t("pinUnchangedDetail")}
      </p>
      {record.observed_by && (
        <p className="mt-1 text-muted-foreground">
          {t("sinceThen", { host: record.observed_by })}
        </p>
      )}
      <p className="mt-1 text-muted-foreground">
        {t("rolloutLogBefore")} {" "}
        <code>{record.log_path ?? "$AVA_HOME/logs/rollout-<epoch>.log"}</code>
        {t("rolloutLogAfter")}
      </p>
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
          <div className="text-xs uppercase tracking-wide text-muted-foreground">{t("host")}</div>
          <div className="mt-0.5 text-sm font-medium">
            {m.name}
            {m.is_staging && (
              <span className="ml-1.5 rounded-sm border border-amber-500/50 px-1 text-2xs font-normal text-amber-600 dark:text-amber-400" title={t("stagingTitle")}>
                {t("staging")}
              </span>
            )}
            {m.name === currentMachine && (
              <span className="font-normal text-muted-foreground">{t("currentHost")}</span>
            )}
          </div>
        </div>
        <div>
          <div className="text-xs uppercase tracking-wide text-muted-foreground">{t("status")}</div>
          <div className="mt-0.5 text-sm font-medium">
            <StatusText m={m} runningLabel="running" />
          </div>
        </div>
        <div>
          <div className="text-xs uppercase tracking-wide text-muted-foreground">{t("health")}</div>
          <div className={`mt-0.5 text-sm font-medium ${TONE_TEXT[health.tone]}`} title={t("daemonHealth", { restarter: daemonMark(m.restarter_online), watchdog: daemonMark(m.watchdog_online) })}>
            {health.label === "none" ? "—" : t(health.label)}
          </div>
        </div>
        <div>
          <div className="text-xs uppercase tracking-wide text-muted-foreground">{t("upSince")}</div>
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
                  <span className="ml-1.5 rounded-sm border border-amber-500/50 px-1 text-2xs font-normal text-amber-600 dark:text-amber-400" title={t("stagingTitle")}>
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
// Per-host daemons (restarter, watchdog) ride each machine's probe and feed
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
