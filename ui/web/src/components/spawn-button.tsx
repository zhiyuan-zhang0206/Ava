"use client";

// Spawn button + cross-machine placement picker + model/preset/reasoning-effort selectors.
//
// Pulls /api/status to inspect cluster.machines. Agent processes only run on
// agent-runner machines (the gateway refuses local spawn with HTTP 400 —
// see `gateway/app.py:post_agents`), so the spawn target list is filtered to
// online, not-paused, role=agent-runner rows.
//
// - 0 spawnable → button disabled with a tooltip explaining
// - 1 spawnable → plain button passes that machine's name to onSpawn
//   (we never call onSpawn(undefined) on a gateway-hosted frontend,
//   because the backend would interpret it as "spawn here" and reject)
// - >=2 spawnable → Popover trigger; clicking an entry calls onSpawn(name)
//
// The model picker is a custom Popover dropdown (not a native <select>) so each
// option can lay out the model name (left) and pricing (right) with flexbox —
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

import { api } from "@/lib/api";
import { groupedModels, isSuperseded, providerLabel } from "@/lib/models";
import { useUserSettings } from "@/lib/use-user-settings";
import { FLEX, FLEX_1, MIN_W_0 } from "@/lib/layout";
import { cn } from "@/lib/utils";

interface SpawnOpts {
  machine?: string;
  model?: string;
  preset?: string;
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
  const { data: modelsData } = useQuery({
    queryKey: ["models"],
    queryFn: () => api.getModels(),
    // Model list is static cluster config — never refetch on focus/remount.
    staleTime: Infinity,
  });
  const { data: presetsData } = useQuery({
    queryKey: ["presets"],
    queryFn: () => api.listPresets(),
    // Presets change rarely (managed on the Control page); a short stale window
    // keeps the picker fresh without polling.
    staleTime: 30_000,
  });
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
  const selectedPreset = (settings["behavior.spawn_preset"] as string | null) ?? undefined;
  const setSelectedPreset = (v: string | undefined) => setSetting("behavior.spawn_preset", v ?? null);
  const selectedReasoningEffort = (settings["behavior.spawn_reasoning_effort"] as string | null) ?? undefined;
  const setSelectedReasoningEffort = (v: string | undefined) =>
    setSetting("behavior.spawn_reasoning_effort", v ?? null);

  // Agent processes only run on agent-runner machines whose probe returned a
  // determinate unpaused verdict. `online=true, paused=null` means the ops
  // server was reached but status is unknown, so spawning must fail closed.
  const allMachines = statusData?.cluster.machines ?? [];
  const spawnable = allMachines.filter(
    (m) => m.serve_agent_runner && m.online && m.paused === false,
  );

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

  // Resolve the effective model: an explicit selection is ALWAYS sent —
  // even when it equals the cluster default — because the preset merge on the
  // backend lets explicit config beat the preset's per key, and a preset that
  // pins llm_model must not silently win over a model the user visibly picked.
  // Never touched (no stored spawn_model) → undefined = omit config, so the
  // cluster default (or the preset's model) applies.
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
  // option for models with a concrete default.
  const modelDefaultEffort =
    typeof selectedModelInfo?.reasoning_effort_default === "string"
      ? selectedModelInfo.reasoning_effort_default
      : null;
  const hasConcreteDefault =
    modelDefaultEffort !== null && effortLevels.includes(modelDefaultEffort);
  // Re-derived from the resolved model on every render rather than trusted
  // from the stored setting: a level the current model does not offer must not
  // reach the spawn request. Explicit selection wins, then the model's own
  // default, then nothing (the provider's default — expressible only via the
  // legacy "Effort: default" option, which stays for models without a concrete
  // default). The stored setting is left as the user last set it, so switching
  // back to a model that offers it restores it.
  const effectiveReasoningEffort: string | undefined =
    selectedReasoningEffort !== undefined && effortLevels.includes(selectedReasoningEffort)
      ? selectedReasoningEffort
      : hasConcreteDefault
        ? modelDefaultEffort
        : undefined;

  const presets = presetsData ?? [];

  function buildOpts(machine?: string): SpawnOpts {
    return {
      machine,
      model: effectiveModel,
      preset: selectedPreset,
      reasoning_effort: effectiveReasoningEffort,
    };
  }

  // Custom Popover-based model picker — native <select> can't flexbox-align
  // option text, so we use a dropdown with model name left + price right.
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
            className="z-50 min-w-[220px] max-w-[90vw] rounded-md border border-border bg-popover text-popover-foreground shadow-md outline-none"
          >
            <div className="px-3 py-2 border-b border-border text-xs font-medium text-muted-foreground">
              {t("models")}
            </div>
            <ul className="py-1 max-h-[320px] overflow-y-auto">
              {modelGroups.map(([provider, models]) => (
                <Fragment key={provider}>
                  <li className="px-3 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground/70 first:pt-1">
                    {providerLabel(provider)}
                  </li>
                  {models.map((m) => {
                    const info = modelsData?.models[m];
                    const p = info?.pricing;
                    const isSelected = m === (selectedModel ?? defaultModel);
                    return (
                      <li key={m}>
                        <button
                          type="button"
                          onClick={() => {
                            setSelectedModel(m);
                            setModelOpen(false);
                          }}
                          className={
                            "w-full text-left px-3 py-1.5 text-sm hover:bg-sidebar-accent flex items-center justify-between gap-3" +
                            (isSelected ? " bg-sidebar-accent/50" : "")
                          }
                        >
                          <span className="truncate font-medium">{m}</span>
                          {p ? (
                            <span className="text-xs text-muted-foreground shrink-0 tabular-nums">
                              ${p.input.toFixed(2)}&thinsp;/&thinsp;${p.cache_read.toFixed(2)}&thinsp;/&thinsp;${p.output.toFixed(2)}
                            </span>
                          ) : (
                            <span className="text-xs text-muted-foreground shrink-0">—</span>
                          )}
                        </button>
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
  // offers levels: a select whose sole entry is "Effort: default" is a control
  // with nothing to control. The select shows ONLY concrete ladder values with
  // the model's default pre-selected (task #568) — no synthetic "Effort:
  // default" row. The legacy "" option survives only for a model with no
  // concrete default (no catalog model today): it sends no reasoning_effort,
  // leaving the provider's own server-side default in force.
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
        {!hasConcreteDefault && <option value="">{t("effortDefault")}</option>}
        {effortLevels.map((effort) => (
          <option key={effort} value={effort}>
            {effort}
          </option>
        ))}
      </select>
    ) : null;

  // Only shown when presets exist — an empty catalog leaves the spawn header
  // uncluttered. The blank option is "no preset" (a raw spawn); a selection
  // seeds the new agent's config from that preset. A preset that carries a
  // model (config.llm_model) or an effort (config.reasoning_effort) OVERRIDES
  // the pickers with those values (task #568) — picking "Ultra Speed Worker"
  // must visibly switch to its mimo model instead of silently spawning on
  // whatever model was selected before. The preset remains the seed: the
  // override is written into the stored spawn_model / spawn_reasoning_effort
  // settings, and a later explicit pick still wins (backend merge: explicit
  // config beats preset config per key).
  // #723 round 2 (user ruling): preset / model / effort pickers get FIXED
  // widths (no width jitter as names change length) with ellipsis overflow.
  // A native <select> cannot render an ellipsis (its text is the selected
  // <option>'s, truncated without a marker), so the preset picker — whose
  // names are the long ones — becomes a Popover button like the model
  // picker; effort (short ladder values) stays a native select.
  const [presetOpen, setPresetOpen] = useState(false);
  const selectPreset = (name: string | undefined) => {
    setSelectedPreset(name);
    setPresetOpen(false);
    const preset = presets.find((p) => p.name === name);
    if (preset) {
      const presetModel = preset.config.llm_model;
      if (typeof presetModel === "string" && presetModel !== "") {
        setSelectedModel(presetModel);
      }
      const presetEffort = preset.config.reasoning_effort;
      if (typeof presetEffort === "string" && presetEffort !== "") {
        setSelectedReasoningEffort(presetEffort);
      }
    }
  };
  const presetSelect =
    presets.length > 0 ? (
      <Popover.Root open={presetOpen} onOpenChange={setPresetOpen}>
        <Popover.Trigger asChild>
          <button
            type="button"
            aria-label={t("preset")}
            className={cn("w-[120px] inline-flex items-center gap-0.5 truncate text-xs bg-transparent border border-border rounded px-1 py-0.5 text-muted-foreground hover:text-foreground focus:outline-none focus:ring-1 focus:ring-ring cursor-pointer", MIN_W_0)}
          >
            <span className="truncate">{selectedPreset ?? t("noPreset")}</span>
            <ChevronDown className="size-3 shrink-0 opacity-50" />
          </button>
        </Popover.Trigger>
        <Popover.Portal>
          <Popover.Content
            sideOffset={6}
            align="start"
            className="z-50 min-w-[180px] max-w-[90vw] rounded-md border border-border bg-popover text-popover-foreground shadow-md outline-none"
          >
            <div className="px-3 py-2 border-b border-border text-xs font-medium text-muted-foreground">
              {t("presets")}
            </div>
            <ul className="py-1 max-h-[320px] overflow-y-auto">
              <li>
                <button
                  type="button"
                  onClick={() => selectPreset(undefined)}
                  className="w-full text-left px-3 py-1.5 text-sm hover:bg-sidebar-accent"
                >
                  {t("noPreset")}
                </button>
              </li>
              {presets.map((preset) => (
                <li key={preset.id}>
                  <button
                    type="button"
                    onClick={() => selectPreset(preset.name)}
                    className={
                      "w-full text-left px-3 py-1.5 text-sm hover:bg-sidebar-accent flex items-center justify-between gap-3" +
                      (preset.name === selectedPreset ? " bg-sidebar-accent/50" : "")
                    }
                  >
                    <span className="truncate">{preset.label}</span>
                  </button>
                </li>
              ))}
            </ul>
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
    ) : null;

  if (spawnable.length === 0) {
    return (
      <TriggerButton
        variant={variant}
        disabled
      />
    );
  }

  if (spawnable.length === 1) {
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
        {presetSelect}
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

  // ≥2 spawnable agent-runners → popover picker, alphabetical
  const sorted = [...spawnable].sort((a, b) => a.name.localeCompare(b.name));

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
                    onClick={() => {
                      onSpawn(buildOpts(m.name));
                      setOpen(false);
                    }}
                    className={cn("w-full text-left px-3 py-1.5 text-sm hover:bg-sidebar-accent items-center justify-between gap-3", FLEX)}
                  >
                    <span className="truncate">{m.name}</span>
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
      {presetSelect}
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
                    onClick={() => {
                      onSpawn(buildOpts(m.name));
                      setOpen(false);
                    }}
                    className={cn("w-full text-left px-3 py-1.5 text-sm hover:bg-sidebar-accent items-center justify-between gap-3", FLEX)}
                  >
                    <span className="truncate">{m.name}</span>
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
  // Height matches the adjacent preset / model / effort pickers: they are
  // native selects styled as text-xs + py-0.5 + border, so the Spawn button
  // uses the same formula (instead of buttonVariants' h-8) to sit level with
  // them in the spawn row (user report: the button was visibly taller).
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
