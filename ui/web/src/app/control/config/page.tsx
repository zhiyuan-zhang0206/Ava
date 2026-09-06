"use client";

// /control#config — runtime config management, per-machine.
//
// Selector: a machine dropdown sits above the group sections. Default is
// "Cluster (gateway)" — that view edits cluster-scope fields + this
// host's own host fields. Selecting an online agent-runner switches to that
// host's view: its host fields become the editable ones, while the
// cluster / agent fields render read-only (they're machine-independent;
// edit them from the Cluster view). A remote view also drops fields whose
// capability the machine doesn't carry (a pure agent-runner hides
// gateway-capability fields — they're edited on the Cluster view).
//
// Read: GET /api/config[?machine=] returns ConfigView (field metadata +
// raw_overrides). Write: PUT /api/config[?machine=] merges a patch into the
// override layer (reducer semantics: a value sets, null unsets, an absent key
// is left untouched) and returns a ConfigWriteResult (per-field ok/reason +
// restart_required).
//
// Layout: a sticky tag-filter bar, then the 17 semantic display groups from
// _config_groups.ts (a FRONTEND regrouping of the backend's domain groups —
// each group element id is a nav sub-anchor). Every row shows a sentence-case
// label derived from the env var (the env var itself rides in the title
// attribute) plus small tag badges: Runtime / CLI-only (effective
// editability), the raw scope, per-agent, "Startup: <process>", deprecated.
//
// Interactions:
// - bool fields → ON/OFF toggle, click PUTs immediately
// - string/int/float fields → click value to edit, Enter / blur to save
// - CLI-only (non-editable) fields → dimmed row, read-only value
// - sensitive fields (API keys) → write-only replace: never read back (masked
//   ••••••• + lock icon), but if the field is writable a "Replace" pencil opens
//   an empty password input. Submitting a non-empty value replaces the secret;
//   an empty submit is a no-op (keeps the current value). The backend round-trips
//   the unchanged-sentinel for every OTHER secret, so replacing one key never
//   touches another. A non-writable secret (db_url / redis_url) stays read-only.
// - a host bool field with can_enable === false → pre-greyed disabled, with
//   its reason shown inline (e.g. "no display detected")
//
// Write result: the PUT is atomic server-side. If a field's result is
// !ok, its reason is shown inline (red) and the displayed value reverts
// (the refetch restores authoritative pre-write state). restart_required
// from the result drives the per-machine restart banner.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronDown, Lock, Pencil, RefreshCw, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { api } from "@/lib/api";
import { errMsg } from "@/lib/errors";
import type {
  AgentMachineRow,
  ConfigFieldView,
  ConfigView,
  ConfigWriteResult,
} from "@/lib/types";
import { SYSTEM_STATUS_QUERY_KEY } from "@/lib/use-cluster-health";

import {
  CONFIG_DISPLAY_GROUPS,
  CONFIG_FILTERS,
  DEFAULT_MODEL_GROUP_ID,
  HIDDEN_ENV_VARS,
  PER_MODEL_GROUP_ID,
  displayGroupId,
  displayOrder,
  fieldFilterTags,
  fieldLabel,
  formatConfigValue,
  isDeprecated,
  isPerAgent,
} from "../_config_groups";
import { useSectionVisible } from "../_visibility";
import { DefaultModelPanel } from "./_default_model";
import { PerModelPanel } from "./_per_model";
import { FLEX, FLEX_1, FLEX_COL, MIN_W_0 } from "@/lib/layout";
import { cn } from "@/lib/utils";

// `null` selection = the cluster / gateway view (machine omitted from
// the request). A string = that agent-runner's name.
type MachineSelection = string | null;

const configQueryKey = (machine: MachineSelection) => ["config", machine] as const;
const MACHINES_QUERY_KEY = ["machines"] as const;
const COLLAPSED_GROUPS_STORAGE_KEY = "control.config.collapsedGroups";

// --- helpers ---

/** Apply a single-field delta on top of raw_overrides, return a new overrides object. */
function applyDelta(
  prev: Record<string, unknown>,
  name: string,
  value: unknown,
): Record<string, unknown> {
  const next = { ...prev };
  next[name] = value;
  return next;
}

function restartLabel(
  target: string,
  t: ReturnType<typeof useTranslations<"config">>,
): string {
  switch (target) {
    case "agent":
      return t("agentProcess");
    case "ops":
      return t("opsDaemon");
    case "gateway":
      return t("gatewayProcess");
    case "all":
      return t("allProcesses");
    default:
      return target;
  }
}

/** Whether a field is editable for the current selection. Sensitive fields follow
 * the SAME writability gate as any other field — a writable secret (an API key)
 * is editable write-only (replace, never read back), a non-writable secret
 * (db_url / redis_url) stays read-only. The masking + write-only input live in
 * ConfigRow; this only answers "may the user change it at all".
 * - Cluster (self) selected: editable iff `field.writable` — cluster fields +
 *   this host's own host fields; agent fields are read-only.
 * - An agent-runner selected: editable iff a host field with remote_writable;
 *   cluster + agent fields are read-only (machine-independent).
 */
function isFieldEditable(field: ConfigFieldView, selection: MachineSelection): boolean {
  if (selection === null) return field.writable;
  return field.scope === "host" && field.remote_writable;
}

// --- page ---

export default function ConfigPage() {
  const t = useTranslations("config");
  const qc = useQueryClient();
  const visible = useSectionVisible();
  const [selection, setSelection] = useState<MachineSelection>(null);
  const [filter, setFilter] = useState<string>("all");
  const [collapsedState, setCollapsedState] = useState<{
    groups: Set<string>;
    hydrated: boolean;
  }>({ groups: new Set(), hydrated: false });

  useEffect(() => {
    let groups = new Set<string>();
    try {
      const stored = localStorage.getItem(COLLAPSED_GROUPS_STORAGE_KEY);
      if (stored != null) {
        const parsed: unknown = JSON.parse(stored);
        if (Array.isArray(parsed)) {
          groups = new Set(parsed.filter((id): id is string => typeof id === "string"));
        }
      }
    } catch {
      // A corrupt or unavailable per-device store must not hide config fields.
    }
    // eslint-disable-next-line react-hooks/set-state-in-effect -- SSR-safe localStorage hydration: the first paint stays fully expanded to avoid a hydration mismatch
    setCollapsedState({ groups, hydrated: true });
  }, []);

  useEffect(() => {
    if (!collapsedState.hydrated) return;
    try {
      // Per-device navigation state: group disclosure is intentionally exempt
      // from DB-backed user settings and follows the task's localStorage contract.
      localStorage.setItem(
        COLLAPSED_GROUPS_STORAGE_KEY,
        JSON.stringify([...collapsedState.groups]),
      );
    } catch {
      // Config remains usable when storage is blocked or full.
    }
  }, [collapsedState]);

  const { data, isLoading, error } = useQuery({
    queryKey: configQueryKey(selection),
    queryFn: () => api.getConfig(selection ?? undefined),
    enabled: visible,
  });

  // Machine list (name + live) drives the selector's agent-runner options.
  const machines = useQuery({
    queryKey: MACHINES_QUERY_KEY,
    queryFn: () => api.getMachines(),
    enabled: visible,
  });
  // Gateway machine name — labels the Cluster option (which machine owns the
  // cluster config). Machines list uniformly by role: a single box that carries
  // both capabilities appears as both the Cluster option and an agent-runner row,
  // with no single-box special-casing.
  const status = useQuery({
    queryKey: SYSTEM_STATUS_QUERY_KEY,
    queryFn: () => api.getSystemStatus(),
    enabled: visible,
  });
  const gatewayName = status.data?.cluster.current_machine ?? null;

  const [saveError, setSaveError] = useState<string | null>(null);
  const [editingField, setEditingField] = useState<string | null>(null);
  const [editValue, setEditValue] = useState<string>("");
  // Per-field write rejections (red inline note), keyed by field name. Set
  // from a ConfigWriteResult; cleared when a new write for that field starts.
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  // Restart targets reported by the last successful write — drives the
  // per-machine banner instead of the always-on field-scan derivation.
  const [restartTargets, setRestartTargets] = useState<string[]>([]);
  // The field the per-model view sent us to; its row keeps a ring until another
  // link is followed, so a long group list doesn't swallow the landing spot.
  const [focusedField, setFocusedField] = useState<string | null>(null);

  // A write reported per-field verdicts. Mark any !ok field with its reason;
  // the query invalidate (onSettled) restores authoritative pre-write values
  // (the server write is atomic, so a rejected field persisted nothing).
  const applyWriteResult = useCallback((names: string[], result: ConfigWriteResult) => {
    const failures: Record<string, string> = {};
    for (const name of names) {
      // `results` only carries the fields the host actually evaluated; a
      // passed field may be absent. Optional-chain through the lookup.
      const r = result.results[name] as
        | ConfigWriteResult["results"][string]
        | undefined;
      if (r?.ok === false) failures[name] = r.reason ?? "rejected";
    }
    const written = new Set(names);
    setFieldErrors((prev) => ({
      // Clear any prior error on the just-written fields, then overlay the
      // fresh failures (an unrelated field's error is left untouched).
      ...Object.fromEntries(Object.entries(prev).filter(([k]) => !written.has(k))),
      ...failures,
    }));
    setRestartTargets(result.applied ? result.restart_required : []);
  }, []);

  // No optimistic writes. The toggle / input shows a spinner while the PUT
  // is in flight; on settle we invalidate so the next render shows
  // authoritative state — which also reverts a server-rejected field.
  //
  // `machine` is included in the mutation variables so onSettled can
  // invalidate the exact query key that was written — even if the user
  // switches machines while the PUT is in-flight.
  const toggleBool = useMutation({
    mutationFn: async ({
      name,
      value,
      machine,
    }: {
      name: string;
      value: boolean;
      machine: MachineSelection;
    }) => {
      const current = qc.getQueryData<ConfigView>(configQueryKey(machine));
      if (!current) throw new Error("config data not loaded");
      const result = await api.putConfig(
        applyDelta(current.raw_overrides, name, value),
        machine ?? undefined,
      );
      return { name, result };
    },
    onSuccess: ({ name, result }) => applyWriteResult([name], result),
    onError: (err: unknown) => {
      setSaveError(`Save failed: ${errMsg(err)}`);
    },
    onSettled: (_data, _err, variables) => {
      void qc.invalidateQueries({ queryKey: configQueryKey(variables.machine) });
    },
  });

  const saveText = useMutation({
    mutationFn: async ({
      name,
      value,
      machine,
    }: {
      name: string;
      value: string;
      machine: MachineSelection;
    }) => {
      const current = qc.getQueryData<ConfigView>(configQueryKey(machine));
      if (!current) throw new Error("config data not loaded");
      const field = current.fields.find((f) => f.name === name);
      if (!field) throw new Error(`unknown field: ${name}`);
      // Type conversion
      let parsed: unknown = value;
      if (field.field_type === "int") parsed = parseInt(value, 10);
      else if (field.field_type === "float") parsed = parseFloat(value);
      const result = await api.putConfig(
        applyDelta(current.raw_overrides, name, parsed),
        machine ?? undefined,
      );
      return { name, result };
    },
    onMutate: () => {
      // Close the inline editor immediately so the input doesn't keep
      // capturing keystrokes while the PUT is in flight.
      setEditingField(null);
    },
    onSuccess: ({ name, result }) => applyWriteResult([name], result),
    onError: (err: unknown) => {
      setSaveError(`Save failed: ${errMsg(err)}`);
    },
    onSettled: (_data, _err, variables) => {
      void qc.invalidateQueries({ queryKey: configQueryKey(variables.machine) });
    },
  });

  // A per-model row links to the field's editor down in the group list. The
  // field may be filtered out, so reset the filter first; the scroll runs in an
  // effect, i.e. after that re-render has put the row back in the DOM.
  const focusField = useCallback((name: string) => {
    setFilter("all");
    setFocusedField(name);
    const field = data?.fields.find((candidate) => candidate.name === name);
    if (field) {
      const groupId = displayGroupId(field);
      setCollapsedState((prev) => {
        if (!prev.groups.has(groupId)) return prev;
        const groups = new Set(prev.groups);
        groups.delete(groupId);
        return { ...prev, groups };
      });
    }
  }, [data]);
  useEffect(() => {
    if (!focusedField) return;
    // `center` rather than an anchor jump: the filter bar above is sticky, so a
    // top-aligned target lands underneath it.
    document
      .getElementById(`config-field-${focusedField}`)
      ?.scrollIntoView({ block: "center" });
  }, [focusedField]);

  const onSelectMachine = useCallback((next: MachineSelection) => {
    setSelection(next);
    setSaveError(null);
    setEditingField(null);
    setFieldErrors({});
    setRestartTargets([]);
  }, []);

  const startEdit = useCallback((field: ConfigFieldView) => {
    setEditingField(field.name);
    // A secret is write-only: start blank (never seed the input with the mask or
    // any read-back value). A normal field seeds with its current value.
    setEditValue(field.sensitive ? "" : String(field.current_value ?? ""));
  }, []);

  const cancelEdit = useCallback(() => {
    setEditingField(null);
    setEditValue("");
  }, []);

  const commitEdit = useCallback(
    (field: ConfigFieldView) => {
      if (field.sensitive) {
        // Write-only replace: an empty submit keeps the current secret (never
        // clobber it with ""); a non-empty value replaces it. Never compare
        // against current_value — for a secret that is the mask, not cleartext.
        if (editValue === "") {
          cancelEdit();
          return;
        }
        saveText.mutate({ name: field.name, value: editValue, machine: selection });
        return;
      }
      if (editValue === String(field.current_value ?? "")) {
        cancelEdit();
        return;
      }
      saveText.mutate({ name: field.name, value: editValue, machine: selection });
    },
    [editValue, cancelEdit, saveText, selection],
  );

  const onlineAgentRunners = machines.data
    ? machines.data.filter((m: AgentMachineRow) => m.live)
    : [];

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">{t("loading")}</p>;
  }
  // stale-while-error (Task #1051): a failed post-write refetch (e.g. the
  // gateway restarting) must not replace the config the user is editing
  // with an error line — only a cold failure shows the error state.
  if (error && !data) {
    return (
      <p className="text-sm text-muted-foreground">
        {t("couldntLoad")}
      </p>
    );
  }
  if (!data) return <p className="text-sm text-muted-foreground">(no data)</p>;

  // On a remote (agent-runner) view, non-host fields are machine-independent
  // and cannot be edited here.
  const isRemote = selection !== null;

  // ── bucket fields by display group (_config_groups.ts) ──
  // A remote view drops fields of capabilities the selected machine doesn't
  // carry (common always stays): a pure agent-runner hides gateway-capability
  // fields — they're edited on the Cluster view and its gateway daemons don't
  // run there. The Cluster (self) view shows everything: it edits every
  // capability's cluster fields, which live in the gateway's .env regardless
  // of what this box runs.
  const machineCapabilities = data.machine_capabilities;
  const shownFields = data.fields.filter(
    (f) =>
      !HIDDEN_ENV_VARS.has(f.env_var) &&
      (!isRemote || f.capability === "common" || machineCapabilities.includes(f.capability)),
  );

  const buckets = new Map<string, ConfigFieldView[]>();
  for (const field of shownFields) {
    const gid = displayGroupId(field);
    const bucket = buckets.get(gid);
    if (bucket) bucket.push(field);
    else buckets.set(gid, [field]);
  }
  for (const bucket of buckets.values()) {
    // Curated prototype order first; fields the map doesn't know append
    // after, alphabetically by env var.
    bucket.sort(
      (a, b) => displayOrder(a) - displayOrder(b) || a.env_var.localeCompare(b.env_var),
    );
  }

  // The filter hides non-matching rows; a group with no visible rows hides
  // entirely. Editability feeds the runtime/cli-only tags, so it's computed
  // once per field here.
  const matchesFilter = (field: ConfigFieldView) =>
    filter === "all" ||
    fieldFilterTags(field, isFieldEditable(field, selection)).has(filter);

  const groupSections = CONFIG_DISPLAY_GROUPS.map(({ id, label }) => {
    const all = buckets.get(id) ?? [];
    return { id, label, fields: all.filter(matchesFilter) };
  }).filter((g) => g.fields.length > 0);
  const shownCount = groupSections.reduce((n, g) => n + g.fields.length, 0);

  return (
    <div className="space-y-4">
      <MachineSelector
        selection={selection}
        agentRunners={onlineAgentRunners}
        gatewayName={gatewayName}
        onChange={onSelectMachine}
      />

      {/* The cluster's default model — a DB row, not a `.env` field, so it sits
          above the field list rather than inside a group. */}
      <DefaultModelPanel id={DEFAULT_MODEL_GROUP_ID} />

      {/* Read-only resolution view. Above the filter bar because it answers a
          different question than the rows below ("what does model X get")
          and its own rows link down into them. */}
      <PerModelPanel id={PER_MODEL_GROUP_ID} onFocusField={focusField} />

      {/* Sticky tag-filter bar — stays pinned while the long field list scrolls. */}
      <div className={cn("sticky top-0 z-10 flex-wrap items-center gap-1.5 border-b border-border bg-background py-2", FLEX)}>
        <span className="mr-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Filter
        </span>
        {CONFIG_FILTERS.map((f) => (
          <button
            key={f.id}
            type="button"
            onClick={() => setFilter(f.id)}
            aria-pressed={filter === f.id}
            data-testid={`filter-${f.id}`}
            className={`h-6 rounded border px-2 text-xs transition-colors ${
              filter === f.id
                ? "border-primary bg-primary font-medium text-primary-foreground"
                : "border-border bg-background text-muted-foreground hover:bg-muted hover:text-foreground"
            }`}
          >
            {f.label}
          </button>
        ))}
        {filter !== "all" && (
          <span className="ml-auto text-xs text-muted-foreground">
            {shownCount} setting{shownCount === 1 ? "" : "s"}
          </span>
        )}
      </div>

      {isRemote && (
        <p className="text-xs italic text-muted-foreground">
          Editing {selection}&apos;s host fields; cluster fields are read-only here — edit
          them on the Cluster view.
        </p>
      )}

      {groupSections.map(({ id, label, fields }) => {
        const collapsed = collapsedState.groups.has(id);
        const fieldsId = `${id}-fields`;
        return (
          <div key={id} id={id} className="scroll-mt-4" data-testid={`config-group-${id}`}>
            <h3 className="mb-2 text-sm font-semibold text-muted-foreground">
              <button
                type="button"
                aria-expanded={!collapsed}
                aria-controls={fieldsId}
                onClick={() =>
                  setCollapsedState((prev) => {
                    const groups = new Set(prev.groups);
                    if (collapsed) groups.delete(id);
                    else groups.add(id);
                    return { ...prev, groups };
                  })
                }
                className={cn("w-full items-center justify-between rounded px-1 py-0.5 text-left hover:bg-muted/50", FLEX)}
              >
                <span>
                  {label}{" "}
                  <span className="ml-1 font-normal text-muted-foreground/70">({fields.length})</span>
                </span>
                <ChevronDown
                  className={cn("size-4 shrink-0 transition-transform", collapsed && "-rotate-90")}
                  aria-hidden
                />
              </button>
            </h3>
            {!collapsed ? (
              <div id={fieldsId} className="border border-border rounded-md divide-y divide-border">
                {fields.map((field) => (
                  <ConfigRow
                    key={field.name}
                    field={field}
                    editable={isFieldEditable(field, selection)}
                    highlighted={focusedField === field.name}
                    remoteReadOnly={isRemote && field.scope !== "host"}
                    fieldError={fieldErrors[field.name] ?? null}
                    editingField={editingField}
                    editValue={editValue}
                    busy={toggleBool.isPending || saveText.isPending}
                    onToggle={(v) => {
                      // Drop any prior error on this field before retrying.
                      setFieldErrors((prev) =>
                        Object.fromEntries(
                          Object.entries(prev).filter(([k]) => k !== field.name),
                        ),
                      );
                      toggleBool.mutate({ name: field.name, value: v, machine: selection });
                    }}
                    onSelectEnum={(v) => {
                      // Enum picks PUT immediately (like the bool toggle), no inline
                      // edit step. Drop any prior error on this field before retrying.
                      setFieldErrors((prev) =>
                        Object.fromEntries(
                          Object.entries(prev).filter(([k]) => k !== field.name),
                        ),
                      );
                      saveText.mutate({ name: field.name, value: v, machine: selection });
                    }}
                    onStartEdit={() => startEdit(field)}
                    onCancelEdit={cancelEdit}
                    onEditValueChange={setEditValue}
                    onCommitEdit={() => commitEdit(field)}
                    onEditKeyDown={(e) => {
                      if (e.key === "Enter") commitEdit(field);
                      else if (e.key === "Escape") cancelEdit();
                    }}
                  />
                ))}
              </div>
            ) : null}
          </div>
        );
      })}

      {saveError && (
        <p className="text-sm text-destructive">{saveError}</p>
      )}

      {restartTargets.length > 0 && (
        <div className={cn("items-start gap-2 text-xs text-muted-foreground border-t border-border pt-3", FLEX)}>
          <RefreshCw className="size-3.5 mt-0.5 shrink-0" />
          <span>
            {selection ?? "Cluster"}: restart the following processes for changes to take
            effect: {restartTargets.map((x) => restartLabel(x, t)).join(", ")}
          </span>
        </div>
      )}
    </div>
  );
}

// --- machine selector ---

function MachineSelector({
  selection,
  agentRunners,
  gatewayName,
  onChange,
}: {
  selection: MachineSelection;
  agentRunners: AgentMachineRow[];
  gatewayName: string | null;
  onChange: (next: MachineSelection) => void;
}) {
  const t = useTranslations("config");
  // Native <select> styled with Tailwind — the established dropdown pattern in
  // this codebase (see the spawn-button model picker); no extra component lib.
  // value "" = the Cluster (gateway) view.
  const clusterLabel = gatewayName
    ? t("clusterGateway", { name: gatewayName })
    : t("clusterGatewayPlain");
  return (
    <div className={cn("items-center gap-2", FLEX)}>
      <label htmlFor="config-machine" className="text-xs text-muted-foreground">
        {t("machine")}
      </label>
      <select
        id="config-machine"
        aria-label={t("machine")}
        value={selection ?? ""}
        onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)}
        className="text-xs bg-transparent border border-border rounded px-1.5 py-1 text-foreground hover:text-foreground focus:outline-none focus:ring-1 focus:ring-ring cursor-pointer"
      >
        <option value="">{clusterLabel}</option>
        {agentRunners.map((m) => (
          <option key={m.name} value={m.name}>
            {m.name}
          </option>
        ))}
      </select>
    </div>
  );
}

// Small neutral tag badge on a config row.
function TagBadge({ children }: { children: React.ReactNode }) {
  return (
    <span className="rounded border border-border px-1 text-xs text-muted-foreground">
      {children}
    </span>
  );
}

function ConfigRow({
  field,
  editable,
  highlighted,
  remoteReadOnly,
  fieldError,
  editingField,
  editValue,
  busy,
  onToggle,
  onSelectEnum,
  onStartEdit,
  onCancelEdit,
  onEditValueChange,
  onCommitEdit,
  onEditKeyDown,
}: {
  field: ConfigFieldView;
  editable: boolean;
  // Landed here from the per-model view — ringed so the row is findable.
  highlighted: boolean;
  remoteReadOnly: boolean;
  fieldError: string | null;
  editingField: string | null;
  editValue: string;
  busy: boolean;
  onToggle: (v: boolean) => void;
  onSelectEnum: (v: string) => void;
  onStartEdit: () => void;
  onCancelEdit: () => void;
  onEditValueChange: (v: string) => void;
  onCommitEdit: () => void;
  onEditKeyDown: (e: React.KeyboardEvent) => void;
}) {
  const t = useTranslations("config");
  const isEditing = editingField === field.name;
  const displayValue = displayFieldValue(field);
  const defaultText = formatConfigValue(field.default_value);
  // A gated host bool the host can't enable today (e.g. no display) is
  // pre-greyed disabled with its reason shown inline.
  const capabilityBlocked = field.field_type === "bool" && field.can_enable === false;
  const toggleDisabled = busy || capabilityBlocked;

  return (
    <div
      id={`config-field-${field.name}`}
      className={`flex items-start justify-between gap-4 px-3 py-2 scroll-mt-12 ${
        editable ? "" : "bg-muted/40"
      } ${highlighted ? "ring-1 ring-primary ring-inset" : ""}`}
    >
      <div className={cn("space-y-0.5", FLEX_1, MIN_W_0)}>
        <div className={cn("items-center gap-2 flex-wrap", FLEX)}>
          <span className="font-medium break-all" title={field.env_var}>
            {fieldLabel(field.env_var)}
          </span>
          {field.writable ? (
            <TagBadge>{t("runtime")}</TagBadge>
          ) : (
            <span className="rounded border border-border bg-muted px-1 text-xs font-medium">
              {t("cliOnly")}
            </span>
          )}
          <TagBadge>{field.scope}</TagBadge>
          {isPerAgent(field) && <TagBadge>{t("perAgent")}</TagBadge>}
          {field.restart_required && <TagBadge>Startup: {field.restart_required}</TagBadge>}
          {isDeprecated(field) && <TagBadge>{t("deprecated")}</TagBadge>}
          {remoteReadOnly && (
            <span className="text-xs text-muted-foreground italic">
              {t("editOnCluster")}
            </span>
          )}
          {!editable && field.writable && !remoteReadOnly && (
            <span className="text-xs text-muted-foreground italic">
              {t("readOnly")}
            </span>
          )}
        </div>
        {field.description && (
          <p className="text-xs text-muted-foreground leading-snug">
            {field.description}
          </p>
        )}
        {editable && capabilityBlocked && field.reason && (
          <p className="text-xs text-muted-foreground italic" data-testid={`capability-${field.name}`}>
            {field.reason}
          </p>
        )}
        {fieldError && (
          <p className="text-xs text-destructive" data-testid={`error-${field.name}`}>
            {fieldError}
          </p>
        )}
      </div>

      <div className={cn("shrink-0 items-end gap-1", FLEX, FLEX_COL)}>
        <div className={cn("items-center gap-1", FLEX)}>
        {field.sensitive ? (
          // Sensitive → write-only. Value is NEVER read back: shown masked with a
          // lock. A writable secret (an API key) can be REPLACED — a pencil opens
          // an empty password input; a non-empty submit replaces it, an empty
          // submit is a no-op. A non-writable secret (db_url) stays read-only.
          isEditing ? (
            <>
              <Input
                autoFocus
                type="password"
                value={editValue}
                aria-label={t("replace", { env: field.env_var })}
                placeholder={t("newValue")}
                onChange={(e) => onEditValueChange(e.target.value)}
                onKeyDown={onEditKeyDown}
                className="h-7 w-48 text-xs font-mono"
                data-testid={`input-${field.name}`}
              />
              <Button
                variant="ghost"
                size="sm"
                onClick={onCommitEdit}
                disabled={busy}
                className="h-7 px-1"
                data-testid={`save-${field.name}`}
              >
                <Check className="size-3" />
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={onCancelEdit}
                className="h-7 px-1"
              >
                <X className="size-3" />
              </Button>
            </>
          ) : (
            <>
              <span
                className={cn(
                  "items-center gap-1 px-1 text-xs text-muted-foreground",
                  editable && "cursor-text rounded hover:bg-muted/50",
                  FLEX,
                )}
                data-testid={`sensitive-${field.name}`}
              >
                <Lock className="size-3" aria-hidden />
                {field.current_value ? "••••••••" : "(not set)"}
              </span>
              {editable && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={onStartEdit}
                  disabled={busy}
                  className="h-7 px-1"
                  aria-label={`Replace ${field.env_var}`}
                  data-testid={`edit-${field.name}`}
                >
                  <Pencil className="size-3" />
                </Button>
              )}
            </>
          )
        ) : field.field_type === "bool" ? (
          // bool → Switch. A non-editable field (read-only / remote-read-only)
          // renders a disabled Switch reflecting the current value — same control
          // everywhere, no separate ON/OFF text representation.
          <Switch
            checked={!!field.current_value}
            disabled={!editable || toggleDisabled}
            data-testid={`toggle-${field.name}`}
            aria-label={field.env_var}
            onCheckedChange={(v) => onToggle(v)}
          />
        ) : field.field_type === "enum" ? (
          // enum → native <select> of the fixed choices; picking one PUTs
          // immediately. No free-text input, so an out-of-set value is
          // unreachable from the UI.
          editable ? (
            <select
              value={String(field.current_value ?? "")}
              disabled={busy}
              data-testid={`select-${field.name}`}
              onChange={(e) => onSelectEnum(e.target.value)}
              className="text-xs bg-transparent border border-border rounded px-1.5 py-1 text-foreground focus:outline-none focus:ring-1 focus:ring-ring cursor-pointer disabled:opacity-50"
            >
              {(field.current_value == null || field.current_value === "") && (
                <option value="" disabled>
                  {t("notSet")}
                </option>
              )}
              {(field.choices ?? []).map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          ) : (
            <span className="text-xs text-muted-foreground px-2">
              {displayValue}
            </span>
          )
        ) : isEditing ? (
          // Editing
          <>
            <Input
              autoFocus
              type={field.field_type === "int" || field.field_type === "float" ? "number" : "text"}
              value={editValue}
              onChange={(e) => onEditValueChange(e.target.value)}
              onKeyDown={onEditKeyDown}
              className="h-7 w-48 text-xs font-mono"
              data-testid={`input-${field.name}`}
            />
            <Button
              variant="ghost"
              size="sm"
              onClick={onCommitEdit}
              disabled={busy}
              className="h-7 px-1"
              data-testid={`save-${field.name}`}
            >
              <Check className="size-3" />
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={onCancelEdit}
              className="h-7 px-1"
            >
              <X className="size-3" />
            </Button>
          </>
        ) : (
          // Display value + edit button
          <>
            <span
              className={cn(
                "block max-w-48 truncate px-1 text-xs",
                editable
                  ? "cursor-text rounded hover:bg-muted/50"
                  : "text-muted-foreground",
              )}
              title={displayValue}
            >
              {displayValue}
            </span>
            {editable && (
              <Button
                variant="ghost"
                size="sm"
                onClick={onStartEdit}
                disabled={busy}
                className="h-7 px-1"
                data-testid={`edit-${field.name}`}
              >
                <Pencil className="size-3" />
              </Button>
            )}
          </>
        )}
        </div>
        <span className="text-xs text-muted-foreground">Default: {defaultText}</span>
      </div>
    </div>
  );
}

function displayFieldValue(field: ConfigFieldView): string {
  return formatConfigValue(field.current_value);
}
