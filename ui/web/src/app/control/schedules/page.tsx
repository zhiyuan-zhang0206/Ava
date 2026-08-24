"use client";

// /control/schedules — schedule management: list, status, logs, run history,
// create (natural-language via a writer agent, or a raw script), edit, and
// start/stop/restart/delete.
//
// Backed by /api/schedules (gateway/routers/schedules.py). The gateway's
// ScheduleManager supervises one session per enabled schedule; this page is
// the management surface. Supersedes the Cron tab (removed once migration lands).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CalendarClock,
  ChevronDown,
  ChevronRight,
  Loader2,
  Pause,
  Pencil,
  Play,
  Plus,
  RotateCcw,
  Sparkles,
  Trash2,
} from "lucide-react";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { PythonCode } from "@/components/python-code";
import { Input } from "@/components/ui/input";
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
import type { ScheduleCreate, ScheduleSummary, ScheduleUpdate } from "@/lib/types";

import { useSectionVisible } from "../_visibility";
import { FLEX } from "@/lib/layout";
import { cn } from "@/lib/utils";

const SCHEDULES_QUERY_KEY = ["schedules"] as const;

const EMPTY_FORM: ScheduleCreate = {
  name: "",
  script: "",
  command: "python schedule.py",
  description: null,
  enabled: true,
};

export default function SchedulesPage() {
  const t = useTranslations("schedules");
  const queryClient = useQueryClient();
  const showToast = useStore((s) => s.showToast);
  const setActiveId = useStore((s) => s.setActiveId);
  const router = useRouter();
  const visible = useSectionVisible();

  const { data: schedules, isLoading, error } = useQuery({
    queryKey: SCHEDULES_QUERY_KEY,
    queryFn: api.listSchedules,
    refetchInterval: 15_000,
    enabled: visible,
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: SCHEDULES_QUERY_KEY });
  };
  const onErr = (verb: string) => (err: unknown) => showToast(`${verb} failed: ${errMsg(err)}`);

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteSchedule(id),
    onSuccess: invalidate,
    onError: onErr("Delete"),
  });
  const startMutation = useMutation({
    mutationFn: (id: number) => api.startSchedule(id),
    onSuccess: invalidate,
    onError: onErr("Start"),
  });
  const stopMutation = useMutation({
    mutationFn: (id: number) => api.stopSchedule(id),
    onSuccess: invalidate,
    onError: onErr("Stop"),
  });
  const restartMutation = useMutation({
    mutationFn: (id: number) => api.restartSchedule(id),
    onSuccess: () => {
      invalidate();
      showToast(t("restarted"));
    },
    onError: onErr("Restart"),
  });

  // --- natural-language create: spawn a writer agent, open its conversation ---
  const [nl, setNl] = useState("");
  const draftMutation = useMutation({
    mutationFn: (text: string) => api.draftSchedule(text),
    onSuccess: (res) => {
      setNl("");
      setActiveId(res.agent_id);
      showToast(`Schedule writer #${res.agent_id} started — finish it in the conversation`);
      router.push("/");
    },
    onError: onErr("Draft"),
  });

  // --- raw create form ---
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<ScheduleCreate>(EMPTY_FORM);
  const createMutation = useMutation({
    mutationFn: (body: ScheduleCreate) => api.createSchedule(body),
    onSuccess: () => {
      invalidate();
      setShowForm(false);
      setForm(EMPTY_FORM);
      showToast("Schedule created");
    },
    onError: onErr("Create"),
  });

  const [expandedId, setExpandedId] = useState<number | null>(null);

  if (isLoading) {
    return (
      <div className={cn("justify-center py-12", FLEX)}>
        <Loader2 className="size-6 animate-spin text-muted-foreground" />
      </div>
    );
  }
  // stale-while-error (Task #1051): a failed 15s poll must not swap the
  // table the user is looking at for an error page — only a cold failure
  // (nothing loaded yet) shows the error state.
  if (error && !schedules) {
    return (
      <div className="p-8 text-center text-sm text-muted-foreground">
        {t("couldntLoad")}
      </div>
    );
  }

  const list = schedules ?? [];

  return (
    <div className="space-y-4">
      <div className={cn("items-center justify-between", FLEX)}>
        <div className={cn("items-center gap-2", FLEX)}>
          <CalendarClock className="size-4" />
          <h3 className="text-sm font-semibold">{t("pageTitle")}</h3>
          <span className="text-xs text-muted-foreground">
            ({list.length} schedule{list.length !== 1 ? "s" : ""})
          </span>
        </div>
        <Button type="button" size="sm" variant="outline" onClick={() => setShowForm((v) => !v)}>
          <Plus className="size-3.5 mr-1" />
          {t("newRaw")}
        </Button>
      </div>

      {/* --- Natural-language create --- */}
      <div className={cn("items-center gap-2", FLEX)}>
        <Input
          className="h-8 text-xs"
          placeholder="Describe a scheduled task — e.g. “consolidate memory nightly, or when it grows a lot”"
          value={nl}
          onChange={(e) => setNl(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && nl.trim()) draftMutation.mutate(nl.trim());
          }}
        />
        <Button
          type="button"
          size="sm"
          disabled={!nl.trim() || draftMutation.isPending}
          onClick={() => draftMutation.mutate(nl.trim())}
        >
          {draftMutation.isPending ? (
            <Loader2 className="size-3.5 animate-spin mr-1" />
          ) : (
            <Sparkles className="size-3.5 mr-1" />
          )}
          Describe
        </Button>
      </div>

      {/* --- Raw create form --- */}
      {showForm && (
        <ScheduleForm
          title={t("createTitle")}
          value={form}
          onChange={setForm}
          submitting={createMutation.isPending}
          submitLabel="Create"
          canSubmit={!!form.name && !!form.script}
          onSubmit={() => createMutation.mutate(form)}
          onCancel={() => setShowForm(false)}
        />
      )}

      {/* --- Table --- */}
      {list.length === 0 ? (
        <p className="text-xs text-muted-foreground py-4 text-center">{t("noSchedules")}</p>
      ) : (
        <div className="border border-border rounded-md overflow-x-auto">
          <Table className="[&_th]:border-r [&_th]:border-border [&_th:last-child]:border-r-0 [&_td]:border-r [&_td]:border-border [&_td:last-child]:border-r-0">
            <TableHeader>
              <TableRow>
                <TableHead className="w-6" />
                <TableHead className="w-16">{t("status")}</TableHead>
                <TableHead>{t("name")}</TableHead>
                <TableHead>{t("description")}</TableHead>
                <TableHead>{t("updated")}</TableHead>
                <TableHead className="w-28" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {list.map((s) => (
                <ScheduleRow
                  key={s.id}
                  schedule={s}
                  expanded={expandedId === s.id}
                  onExpand={() => setExpandedId((id) => (id === s.id ? null : s.id))}
                  // Edit must ENSURE the row is expanded (toggle semantics
                  // collapsed an already-expanded row's details — Task #1051).
                  onEnsureExpanded={() => setExpandedId(s.id)}
                  onToggle={() =>
                    (s.enabled ? stopMutation : startMutation).mutate(s.id)
                  }
                  onRestart={() => restartMutation.mutate(s.id)}
                  onDelete={() => {
                    if (window.confirm(t("deleteConfirm", { name: s.name }))) deleteMutation.mutate(s.id);
                  }}
                  onEdited={invalidate}
                />
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}

const STATUS_DOT: Record<string, string> = {
  running: "bg-green-500",
  stopped: "bg-muted-foreground/40",
  error: "bg-red-500",
  completed: "bg-blue-500", // clean exit (rc=0) — finished on its own, not a crash
};

function ScheduleRow({
  schedule,
  expanded,
  onExpand,
  onEnsureExpanded,
  onToggle,
  onRestart,
  onDelete,
  onEdited,
}: {
  schedule: ScheduleSummary;
  expanded: boolean;
  onExpand: () => void;
  onEnsureExpanded: () => void;
  onToggle: () => void;
  onRestart: () => void;
  onDelete: () => void;
  onEdited: () => void;
}) {
  const t = useTranslations("schedules");
  const [editing, setEditing] = useState(false);

  return (
    <>
      <TableRow>
        <TableCell className="pr-0">
          <button
            type="button"
            onClick={onExpand}
            className="text-muted-foreground hover:text-foreground"
            aria-label={expanded ? "Collapse" : "Expand"}
          >
            {expanded ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
          </button>
        </TableCell>
        <TableCell>
          <span className={cn("items-center gap-1.5 text-xs text-muted-foreground", FLEX)}>
            <span
              className={`inline-block size-2 rounded-full ${STATUS_DOT[schedule.status] ?? "bg-muted-foreground/40"}`}
            />
            {schedule.status}
          </span>
        </TableCell>
        <TableCell className="font-mono text-xs font-medium">
          {schedule.name}
          {!schedule.enabled && (
            <span className="ml-1.5 text-muted-foreground/60">{t("off")}</span>
          )}
        </TableCell>
        <TableCell
          className="text-xs max-w-[280px] truncate text-muted-foreground"
          title={schedule.description ?? undefined}
        >
          {schedule.description ?? "—"}
        </TableCell>
        <TableCell className="text-xs text-muted-foreground">
          {formatRelative(schedule.updated_at)}
        </TableCell>
        <TableCell>
          <div className={cn("items-center gap-0.5", FLEX)}>
            <IconButton
              label={schedule.enabled ? "Stop" : "Start"}
              onClick={onToggle}
              icon={
                schedule.enabled ? (
                  <Pause className="size-3.5" />
                ) : (
                  <Play className="size-3.5" />
                )
              }
            />
            <IconButton label={t("restart")} onClick={onRestart} icon={<RotateCcw className="size-3.5" />} />
            <IconButton
              label={t("edit")}
              onClick={() => {
                onEnsureExpanded();
                setEditing(true);
              }}
              icon={<Pencil className="size-3.5" />}
            />
            <IconButton label={t("delete")} onClick={onDelete} icon={<Trash2 className="size-3.5" />} />
          </div>
        </TableCell>
      </TableRow>
      {expanded && (
        <TableRow>
          <TableCell colSpan={6} className="bg-muted/30">
            <ScheduleDetail
              id={schedule.id}
              editing={editing}
              onDoneEditing={() => {
                setEditing(false);
                onEdited();
              }}
            />
          </TableCell>
        </TableRow>
      )}
    </>
  );
}

function ScheduleDetail({
  id,
  editing,
  onDoneEditing,
}: {
  id: number;
  editing: boolean;
  onDoneEditing: () => void;
}) {
  const t = useTranslations("schedules");
  const showToast = useStore((s) => s.showToast);
  // This detail block mounts when a row is expanded. Two intents combined:
  //   - enabled: visible — gate the detail queries (especially the 15s logs poll)
  //     to on-screen time, so an expanded schedule scrolled out of view stops
  //     polling;
  //   - staleTime: 0 — so the moment the section becomes visible (expand) the
  //     query pulls fresh instead of serving a stale cached schedule/logs/runs
  //     (the global 5min staleTime; full/runs don't poll, so a re-expand would
  //     otherwise show minutes-old data). staleTime 0 — not refetchOnMount:
  //     "always", which the `enabled` gate can defeat on the enable transition.
  const visible = useSectionVisible();
  const full = useQuery({
    queryKey: ["schedule", id],
    queryFn: () => api.getSchedule(id),
    enabled: visible,
    staleTime: 0,
  });
  const logs = useQuery({
    queryKey: ["schedule-logs", id],
    queryFn: () => api.scheduleLogs(id),
    refetchInterval: 15_000,
    enabled: visible,
    staleTime: 0,
  });
  const runs = useQuery({
    queryKey: ["schedule-runs", id],
    queryFn: () => api.scheduleRuns(id),
    enabled: visible,
    staleTime: 0,
  });

  const [edit, setEdit] = useState<ScheduleUpdate | null>(null);
  const updateMutation = useMutation({
    mutationFn: (body: ScheduleUpdate) => api.updateSchedule(id, body),
    onSuccess: () => {
      showToast("Schedule updated");
      onDoneEditing();
    },
    onError: (err: unknown) => showToast(`Update failed: ${errMsg(err)}`),
  });

  if (full.isLoading || !full.data) {
    return <Loader2 className="size-4 animate-spin text-muted-foreground my-3 mx-auto" />;
  }
  const s = full.data;

  // Lazily seed the edit form from the loaded schedule.
  const editForm: ScheduleUpdate = edit ?? {
    name: s.name,
    description: s.description,
    script: s.script,
    command: s.command,
  };

  return (
    <div className="space-y-3 py-2 text-xs">
      {editing ? (
        <div className="rounded-md border border-border p-3 space-y-2">
          <label className="block text-muted-foreground">
            Description
            <Input
              className="mt-1 h-8 text-xs"
              value={editForm.description ?? ""}
              onChange={(e) => setEdit({ ...editForm, description: e.target.value || null })}
            />
          </label>
          <label className="block text-muted-foreground">
            Script
            <textarea
              className="mt-1 w-full rounded border border-border bg-background p-2 font-mono text-xs"
              rows={12}
              value={editForm.script ?? ""}
              onChange={(e) => setEdit({ ...editForm, script: e.target.value })}
            />
          </label>
          <div className={cn("items-center gap-2", FLEX)}>
            <Button
              type="button"
              size="sm"
              disabled={updateMutation.isPending}
              onClick={() => updateMutation.mutate(editForm)}
            >
              {updateMutation.isPending ? <Loader2 className="size-3.5 animate-spin mr-1" /> : null}
              Save
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={onDoneEditing}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <>
          {s.last_error && (
            <div>
              <div className="font-medium text-red-500">{t("lastError")}</div>
              <pre className="mt-1 max-h-40 overflow-auto rounded bg-background p-2 font-mono text-[11px] text-red-400">
                {s.last_error}
              </pre>
            </div>
          )}
          <div>
            <div className="font-medium text-muted-foreground">{t("script")}</div>
            <div className="mt-1 max-h-64 overflow-auto rounded bg-background p-2">
              <PythonCode code={s.script} />
            </div>
          </div>
          <div>
            <div className="font-medium text-muted-foreground">
              Logs {logs.data ? `(${logs.data.source})` : ""}
            </div>
            <pre className="mt-1 max-h-48 overflow-auto rounded bg-background p-2 font-mono text-[11px] text-muted-foreground">
              {logs.data && logs.data.lines.length > 0 ? logs.data.lines.join("\n") : "—"}
            </pre>
          </div>
          <div>
            <div className="font-medium text-muted-foreground">
              Run history ({runs.data?.length ?? 0})
            </div>
            {runs.data && runs.data.length > 0 ? (
              <ul className="mt-1 space-y-0.5 text-muted-foreground">
                {runs.data.map((r) => (
                  <li key={r.id}>
                    {formatRelative(r.ran_at)} · {r.ok === null ? "…" : r.ok ? "✓" : "✗"}
                    {r.note ? ` · ${r.note}` : ""}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-1 text-muted-foreground/60">{t("noRuns")}</p>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function ScheduleForm({
  title,
  value,
  onChange,
  submitting,
  submitLabel,
  canSubmit,
  onSubmit,
  onCancel,
}: {
  title: string;
  value: ScheduleCreate;
  onChange: (v: ScheduleCreate) => void;
  submitting: boolean;
  submitLabel: string;
  canSubmit: boolean;
  onSubmit: () => void;
  onCancel: () => void;
}) {
  const t = useTranslations("schedules");
  return (
    <div className="rounded-md border border-border p-4 space-y-3">
      <h4 className="text-xs font-medium text-muted-foreground">{title}</h4>
      <div className="grid grid-cols-2 gap-3">
        <label className="text-xs">
          {t("nameRequired")}
          <Input
            className="mt-1 h-8 text-xs font-mono"
            placeholder="memory-arbiter"
            value={value.name}
            onChange={(e) => onChange({ ...value, name: e.target.value })}
          />
        </label>
        <label className="text-xs">
          {t("command")}
          <Input
            className="mt-1 h-8 text-xs font-mono"
            placeholder="python schedule.py"
            value={value.command}
            onChange={(e) => onChange({ ...value, command: e.target.value })}
          />
        </label>
        <label className="text-xs col-span-2">
          {t("description")}
          <Input
            className="mt-1 h-8 text-xs"
            placeholder={t("whatThisDoes")}
            value={value.description ?? ""}
            onChange={(e) => onChange({ ...value, description: e.target.value || null })}
          />
        </label>
        <label className="text-xs col-span-2">
          {t("scriptRequired")}
          <textarea
            className="mt-1 w-full rounded border border-border bg-background p-2 font-mono text-xs"
            rows={12}
            placeholder={"import time\nwhile True:\n    ...\n    time.sleep(60)"}
            value={value.script}
            onChange={(e) => onChange({ ...value, script: e.target.value })}
          />
        </label>
      </div>
      <div className={cn("items-center gap-2", FLEX)}>
        <Button type="button" size="sm" disabled={!canSubmit || submitting} onClick={onSubmit}>
          {submitting ? <Loader2 className="size-3.5 animate-spin mr-1" /> : null}
          {submitLabel}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

function IconButton({
  label,
  onClick,
  icon,
}: {
  label: string;
  onClick: () => void;
  icon: React.ReactNode;
}) {
  return (
    <Button
      type="button"
      size="icon"
      variant="ghost"
      className="size-7 text-muted-foreground"
      onClick={onClick}
      aria-label={label}
    >
      {icon}
    </Button>
  );
}
