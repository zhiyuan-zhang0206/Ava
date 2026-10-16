"use client";

// Pulls /api/status to inspect cluster.machines. Agent processes only run on
// agent-runner machines (the gateway refuses local spawn with HTTP 400 —
// see `gateway/agents/router.py:post_agents`), so the spawn target list requires
// online, unpaused agent-runner rows with an observed live agent host.
//
// - 0 spawnable → button disabled with a tooltip explaining
// - 1 eligible, spawnable → plain button passes that machine's name to onSpawn
//   (we never call onSpawn(undefined) on a gateway-hosted frontend,
//   because the backend would interpret it as "spawn here" and reject)
// - >=2 eligible → Popover trigger with host-down entries disabled + explained
//
// The model picker is a custom Popover dropdown (not a native <select>) so each
// option can lay out the model name, pricing and vendor TPS in aligned columns —
// the aligned price column makes cross-model price comparison easy.
//
// QueryKey ["status"] is shared with the Control page — TanStack Query
// dedupes automatically. We set refetchInterval: 15000 so the spawnable
// list stays fresh while the sidebar is mounted. Without it, a host that
// transiently dropped offline (probe timeout, sleep, network blip) at
// mount time would leave the picker stuck on the single-spawnable path
// until the user reloaded — meaning the next click skips the picker.

import * as Popover from "@radix-ui/react-popover";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import { Fragment, useState } from "react";

import { api } from "@/lib/transport/api";
import { MODEL_PICKER_COLUMNS, ModelPickerRow } from "./model-picker-row";
import { groupedModels, isSuperseded, providerLabel } from "@/lib/contracts/models";
import { useUserSettings } from "@/lib/state/use-user-settings";
import { FLEX, FLEX_1, MIN_W_0 } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

interface SpawnOpts {
  machine?: string;
  model?: string;
  reasoning_effort?: string;
}

interface Props {
  onSpawn: (opts: SpawnOpts) => void;
  /** 'sm' = full button for the expanded sidebar header; 'icon' = pure icon for the collapsed sidebar. */
  variant: "sm" | "icon";
}

export function SpawnButton({ onSpawn, variant }: Props) {
  const t = useTranslations("spawn");
  const { data: statusData } = useQuery({
    queryKey: ["status"],
    queryFn: () => api.getSystemStatus(),
    refetchInterval: 15_000,
  });
  const modelsQuery = useQuery({
    queryKey: ["models"],
    queryFn: () => api.getModels(),
    // Config writes invalidate this catalog, including its effective birth default.
    staleTime: Infinity,
  });
  const modelsData = modelsQuery.error ? undefined : modelsQuery.data;
  const [open, setOpen] = useState(false);
  const [modelOpen, setModelOpen] = useState(false);
  // Picker selections are DB-backed user preferences (behavior.spawn_*) so they
  // survive remounts, are shared across the collapsed/expanded instances, AND
  // sync across frontends — the user can spawn many agents on one model without
  // re-picking. `null` in the DB means "no override"; expose it as `undefined`
  // to the picker logic below. Written back as `?? null`.
  const { settings, setSetting } = useUserSettings();
  const selectedModel = (settings["behavior.spawn_model"] as string | null) ?? undefined;
  const setSelectedModel = (v: string | undefined) => setSetting("behavior.spawn_model", v ?? null);
  const selectedReasoningEffort = (settings["behavior.spawn_reasoning_effort"] as string | null) ?? undefined;
  const setSelectedReasoningEffort = (v: string | undefined) =>
    setSetting("behavior.spawn_reasoning_effort", v ?? null);

  // Ops reachability is not agent-host readiness. Keep the existing host-alive
  // verdict's meaning, and show non-spawnable hosts in the picker with a reason.
  const allMachines = statusData?.cluster.machines ?? [];
  const eligible = allMachines.filter(
    (m) => m.serve_agent_runner && m.online && m.paused === false,
  );
  const spawnable = eligible.filter((m) => m.agent_host_online === true);

  // Provider-grouped model list for the picker — providers in response
  // order, models within each provider in order (see lib/models.ts).
  // User-hidden models (setting models.hidden) and registry-superseded models
  // (superseded_by — replaced by a newer model, still config-valid) are
  // excluded from the picker.
  const hiddenModels: string[] = (settings["models.hidden"] as string[] | undefined) ?? [];
  const rosterModels = new Set(Object.values(modelsData?.providers ?? {}).flat());
  const hiddenModelCount = new Set(
    hiddenModels.filter((model) => rosterModels.has(model)),
  ).size;
  const modelGroups = groupedModels(
    modelsData,
    (m) => !hiddenModels.includes(m) && !isSuperseded(modelsData, m),
  );
  const defaultModel = modelsData?.default;

  const effectiveModel = selectedModel;

  // Resolved model name: explicit selection or cluster default. Used to look
  // up model info so the effort dropdown shows for the default model on first
  // load without requiring a manual model switch.
  const resolvedModelName = selectedModel ?? defaultModel;
  const selectedModelInfo = resolvedModelName ? modelsData?.models[resolvedModelName] : undefined;
  // The effort ladder this model offers, with all three "no ladder" shapes
  // collapsed into one empty list: the model carries no effort knob (wire
  // null), the models query has not resolved yet (the stored spawn_model
  // arrives from user settings first, so this window is every page load), and
  // the resolved name is absent from the catalog (a stored spawn_model naming
  // a model since renamed or retired).
  const effortLevels = selectedModelInfo?.reasoning_effort_options ?? [];
  // The model's concrete default effort, published by GET /api/models from the
  // registry's per-model tuning (every catalog model pins one — validated
  // server-side). The picker pre-selects it, so "select a model" immediately
  // shows the level that model runs at; there is no synthetic "Effort: default"
  // option.
  const modelDefaultEffort =
    typeof selectedModelInfo?.reasoning_effort_default === "string"
      ? selectedModelInfo.reasoning_effort_default
      : null;
  // Re-derived from the resolved model on every render rather than trusted
  // from the stored setting: a level the current model does not offer must not
  // reach the spawn request. Explicit selection wins, then the model's own
  // default; a model the catalog has not resolved publishes no default, and
  // nothing is sent for it. The stored setting is left as the user last set it,
  // so switching back to a model that offers it restores it.
  const effectiveReasoningEffort: string | undefined =
    selectedReasoningEffort !== undefined && effortLevels.includes(selectedReasoningEffort)
      ? selectedReasoningEffort
      : (modelDefaultEffort ?? undefined);

  function buildOpts(machine?: string): SpawnOpts {
    return {
      machine,
      model: effectiveModel,
      reasoning_effort: effectiveReasoningEffort,
    };
  }

  // Custom Popover-based model picker — native <select> can't flexbox-align
  // option text, so we use a dropdown with model, price and TPS columns.
  // Grouped by provider: one header row per provider, then its models, all
  // inside a single <ul> (keeps one "list" role for the picker — the header
  // rows are plain non-interactive <li>s, not their own nested lists).
  const modelPicker =
    modelGroups.length > 0 ? (
      <Popover.Root open={modelOpen} onOpenChange={setModelOpen}>
        <Popover.Trigger asChild>
          <button
            type="button"
            aria-label={t("model")}
            className={cn("w-[120px] inline-flex items-center gap-0.5 truncate text-xs bg-transparent border border-border rounded px-1 py-0.5 text-muted-foreground hover:text-foreground focus:outline-none focus:ring-1 focus:ring-ring cursor-pointer", MIN_W_0)}
          >
            <span className="truncate">{selectedModel ?? defaultModel ?? ""}</span>
            <ChevronDown className="size-3 shrink-0 opacity-50" />
          </button>
        </Popover.Trigger>
        <Popover.Portal>
          <Popover.Content
            sideOffset={6}
            align="start"
            className="z-50 w-[480px] max-w-[90vw] rounded-md border border-border bg-popover text-popover-foreground shadow-md outline-none"
          >
            <div className={cn(MODEL_PICKER_COLUMNS, "px-3 py-2 border-b border-border text-xs font-medium text-muted-foreground")}>
              <span>{t("models")}</span>
              <span className="text-right">{t("pricePerMillion")}</span>
                        <span className="text-right" title={t("referenceTps")}>{t("tps")}</span>
            </div>
            <ul className="py-1 max-h-[320px] overflow-y-auto">
              {modelGroups.map(([provider, models]) => (
                <Fragment key={provider}>
                  <li className="px-3 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground/70 first:pt-1">
                    {providerLabel(provider)}
                  </li>
                  {models.map((m) => {
                    const info = modelsData?.models[m];
                    const isSelected = m === (selectedModel ?? defaultModel);
                    return (
                      <li key={m}>
                        <ModelPickerRow
                          model={m}
                          info={info}
                          selected={isSelected}
                          onSelect={() => {
                            setSelectedModel(m);
                            setModelOpen(false);
                          }}
                        />
                      </li>
                    );
                  })}
                </Fragment>
              ))}
            </ul>
            {hiddenModelCount > 0 && (
              <div className="px-3 py-2 border-t border-border text-xs font-medium text-muted-foreground">
                {t("hiddenModels", { count: hiddenModelCount })}
              </div>
            )}
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
    ) : null;

  // Reasoning effort dropdown — the ladder comes per model from GET
  // /api/models (registry `effort_levels`; even providers with only a binary
  // thinking on/off, like mimo / claude-haiku-4-5-20251001, publish a
  // two-value ladder), and the model's default effort rides along
  // (`reasoning_effort_default`, the registry's per-model tuning value — a
  // spawn with no explicit effort runs at it). Rendered only when the model
  // offers levels: a model with no ladder has nothing to control. The select
  // shows ONLY concrete ladder values with the model's default pre-selected
  // (task #568) — no synthetic "Effort: default" row: every catalog model pins
  // a concrete default (registry-checked), so no fallback row is needed.
  const reasoningEffortSelect =
    effortLevels.length > 0 ? (
      <select
        aria-label={t("reasoningEffort")}
        value={effectiveReasoningEffort ?? ""}
        onChange={(e) =>
          setSelectedReasoningEffort(e.target.value || undefined)
        }
        className={cn("w-[80px] truncate text-xs bg-transparent border border-border rounded px-1 py-0.5 text-muted-foreground hover:text-foreground focus:outline-none focus:ring-1 focus:ring-ring cursor-pointer", MIN_W_0)}
      >
        {effortLevels.map((effort) => (
          <option key={effort} value={effort}>
            {effort}
          </option>
        ))}
      </select>
    ) : null;

  if (modelsQuery.error) {
    return (
      <div className={cn(FLEX, "items-center gap-2 text-xs text-muted-foreground")}>
        {variant === "sm" && <span role="status">{t("modelsUnavailable")}</span>}
        <TriggerButton variant={variant} disabled title={t("modelsUnavailable")} />
      </div>
    );
  }

  if (spawnable.length === 0) {
    return (
      <span
        className="inline-flex"
        title={eligible.length === 1
          ? eligible[0].agent_host_online === false ? t("hostUnavailable") : t("hostUnknown")
          : t("noReadyRunner")}
      >
        <TriggerButton variant={variant} disabled />
      </span>
    );
  }

  if (spawnable.length === 1 && eligible.length === 1) {
    const only = spawnable[0];
    if (variant === "icon") {
      // Icon variant has no room to render the model select; the selection
      // (effectiveModel via buildOpts) is still preserved and passed on spawn.
      return (
        <TriggerButton
          variant={variant}
          onClick={() => onSpawn(buildOpts(only.name))}
        />
      );
    }
    return (
      // flex-nowrap + min-w-0: as the resizable sidebar narrows, the pickers
      // shrink instead of pushing the row wider than its panel.
      <div className={cn("flex-nowrap items-center gap-1 w-full", FLEX, MIN_W_0)}>
        {modelPicker}
        {reasoningEffortSelect}
        <span className={cn(FLEX_1, MIN_W_0)} aria-hidden="true" />
        <TriggerButton
          variant={variant}
          onClick={() => onSpawn(buildOpts(only.name))}
        />
      </div>
    );
  }

  // Multiple eligible runners: the picker includes host-down entries so the
  // unavailable reason is visible instead of silently hiding the machine.
  const sorted = [...eligible].sort((a, b) => a.name.localeCompare(b.name));

  if (variant === "icon") {
    return (
      <Popover.Root open={open} onOpenChange={setOpen}>
        <Popover.Trigger asChild>
          <TriggerButton variant={variant} />
        </Popover.Trigger>
        <Popover.Portal>
          <Popover.Content
            sideOffset={6}
            align="end"
            className="z-50 min-w-[180px] max-w-[90vw] rounded-md border border-border bg-popover text-popover-foreground shadow-md outline-none"
          >
            <div className="px-3 py-2 border-b border-border text-xs font-medium text-muted-foreground">
              {t("spawnOn")}
            </div>
            <ul className="py-1">
              {sorted.map((m) => (
                <li key={m.name}>
                  <button
                    type="button"
                    disabled={m.agent_host_online !== true}
                    title={m.agent_host_online === true ? undefined
                      : m.agent_host_online === false ? t("hostUnavailable") : t("hostUnknown")}
                    onClick={() => {
                      onSpawn(buildOpts(m.name));
                      setOpen(false);
                    }}
                    className={cn("w-full text-left px-3 py-1.5 text-sm hover:bg-sidebar-accent disabled:opacity-50 items-center justify-between gap-3", FLEX)}
                  >
                    <span className="truncate">{m.name}</span>
                    {m.agent_host_online !== true && <span className="text-xs">
                      {m.agent_host_online === false ? t("hostUnavailable") : t("hostUnknown")}
                    </span>}
                  </button>
                </li>
              ))}
            </ul>
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
    );
  }

  return (
    // flex-nowrap + min-w-0: same shrink-with-the-panel contract as the
    // single-machine branch above.
    <div className={cn("flex-nowrap items-center gap-1 w-full", FLEX, MIN_W_0)}>
      {modelPicker}
      {reasoningEffortSelect}
      <span className={cn(FLEX_1, MIN_W_0)} aria-hidden="true" />
      <Popover.Root open={open} onOpenChange={setOpen}>
        <Popover.Trigger asChild>
          <TriggerButton variant={variant} />
        </Popover.Trigger>
        <Popover.Portal>
          <Popover.Content
            sideOffset={6}
            align="end"
            className="z-50 min-w-[180px] max-w-[90vw] rounded-md border border-border bg-popover text-popover-foreground shadow-md outline-none"
          >
            <div className="px-3 py-2 border-b border-border text-xs font-medium text-muted-foreground">
              {t("spawnOn")}
            </div>
            <ul className="py-1">
              {sorted.map((m) => (
                <li key={m.name}>
                  <button
                    type="button"
                    disabled={m.agent_host_online !== true}
                    title={m.agent_host_online === true ? undefined
                      : m.agent_host_online === false ? t("hostUnavailable") : t("hostUnknown")}
                    onClick={() => {
                      onSpawn(buildOpts(m.name));
                      setOpen(false);
                    }}
                    className={cn("w-full text-left px-3 py-1.5 text-sm hover:bg-sidebar-accent disabled:opacity-50 items-center justify-between gap-3", FLEX)}
                  >
                    <span className="truncate">{m.name}</span>
                    {m.agent_host_online !== true && <span className="text-xs">
                      {m.agent_host_online === false ? t("hostUnavailable") : t("hostUnknown")}
                    </span>}
                  </button>
                </li>
              ))}
            </ul>
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
    </div>
  );
}

interface TriggerProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant: "sm" | "icon";
}

function TriggerButton({ variant, ...rest }: TriggerProps) {
  const t = useTranslations("spawn");
  if (variant === "icon") {
    return (
      <button
        type="button"
        className="p-1.5 rounded hover:bg-sidebar-accent text-primary"
        aria-label={t("spawnAgent")}
        {...rest}
      >
        <Plus className="size-4" />
      </button>
    );
  }
  return (
    <button
      type="button"
      className="group/button inline-flex shrink-0 items-center justify-center gap-1.5 rounded-lg border border-transparent bg-secondary px-2 py-0.5 text-xs font-medium whitespace-nowrap transition-all outline-none select-none text-secondary-foreground hover:bg-secondary/80 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 active:not-aria-[haspopup]:translate-y-px disabled:pointer-events-none disabled:opacity-50"
      aria-label={t("spawnAgent")}
      {...rest}
    >
      <Plus className="size-4" />
      <span>{t("spawn")}</span>
    </button>
  );
}
