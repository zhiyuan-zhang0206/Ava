"use client";

// /control#display — user-facing display & behavior preferences.
//
// Each setting is a row: label + description + control (toggle for bool,
// radio group for enums). Changes are optimistic — the toggle flips
// immediately while a PUT fires in the background; on failure it reverts.
//
// Settings are stored server-side (user_settings DB table) so they survive
// across browsers and machines.

import {
  Activity,
  AlertTriangle,
  Bell,
  Calendar,
  CalendarClock,
  CircleDot,
  Clock,
  Eye,
  EyeOff,
  Languages,
  Monitor,
  MoveHorizontal,
  Palette,
  Rows3,
  Ruler,
  Sparkles,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { Fragment } from "react";

import { Switch } from "@/components/ui/switch";
import { api } from "@/lib/api";
import { groupedModels, isSuperseded, providerLabel } from "@/lib/models";
import { useBreakpoint } from "@/lib/breakpoint";
import {
  TIMELINE_NARROW_BREAKPOINT_PX,
  TIMELINE_WIDTH_RATIO_DEFAULT,
  TIMELINE_WIDTH_RATIO_MAX,
  TIMELINE_WIDTH_RATIO_MIN,
} from "@/lib/timeline-width";
import { themePackId, useThemePacks } from "@/lib/use-theme-packs";
import { useDebouncedSetting, useUserSettings } from "@/lib/use-user-settings";
import { FLEX, FLEX_1, MIN_W_0 } from "@/lib/layout";

// ── Setting row components ──

function ToggleRow({
  icon: Icon,
  label,
  description,
  value,
  disabled = false,
  onChange,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  description: React.ReactNode;
  value: boolean;
  disabled?: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    // items-center: the switch stays vertically centered even when the label +
    // description wrap to multiple lines.
    <div className={cn("items-center justify-between gap-4 px-3 py-2.5", FLEX)}>
      <div className={cn("gap-3", FLEX, MIN_W_0)}>
        <Icon className="size-4 mt-0.5 shrink-0 text-muted-foreground" />
        <div className={cn(MIN_W_0)}>
          <div className="text-sm font-medium">{label}</div>
          <div className="text-xs text-muted-foreground mt-0.5 [overflow-wrap:anywhere]">{description}</div>
        </div>
      </div>
      <Switch
        checked={value}
        onCheckedChange={onChange}
        disabled={disabled}
        aria-label={label}
      />
    </div>
  );
}

function RadioRow({
  icon: Icon,
  label,
  description,
  options,
  value,
  onChange,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  description: React.ReactNode;
  options: { value: string; label: string }[];
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className={cn("items-start justify-between gap-4 px-3 py-2.5", FLEX)}>
      <div className={cn("gap-3", FLEX, MIN_W_0)}>
        <Icon className="size-4 mt-0.5 shrink-0 text-muted-foreground" />
        <div className={cn(MIN_W_0)}>
          <div className="text-sm font-medium">{label}</div>
          <div className="text-xs text-muted-foreground mt-0.5 [overflow-wrap:anywhere]">{description}</div>
          <div className={cn("flex-wrap gap-3 mt-2", FLEX)}>
            {options.map((opt) => (
              <label key={opt.value} className={cn("items-center gap-1.5 cursor-pointer", FLEX)}>
                <input
                  type="radio"
                  name={label}
                  value={opt.value}
                  checked={value === opt.value}
                  onChange={() => onChange(opt.value)}
                  className="size-3.5 accent-primary"
                />
                <span className="text-xs">{opt.label}</span>
              </label>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

// A ratio/amount slider row — the Timeline max-width control. Values are
// dragged (high-frequency), so persistence goes through useDebouncedSetting
// (one PUT after the drag settles) instead of a write-through on every change.
function SliderRow({
  icon: Icon,
  label,
  description,
  min,
  max,
  step,
  value,
  onChange,
  format,
  disabled = false,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  description: React.ReactNode;
  min: number;
  max: number;
  step: number;
  value: number;
  onChange: (v: number) => void;
  format: (v: number) => string;
  disabled?: boolean;
}) {
  return (
    <div className={cn("items-start justify-between gap-4 px-3 py-2.5", FLEX)}>
      <div className={cn("gap-3", FLEX, MIN_W_0, FLEX_1)}>
        <Icon className="size-4 mt-0.5 shrink-0 text-muted-foreground" />
        <div className={cn(MIN_W_0, FLEX_1)}>
          <div className="text-sm font-medium">{label}</div>
          <div className="text-xs text-muted-foreground mt-0.5 [overflow-wrap:anywhere]">{description}</div>
          <input
            type="range"
            min={min}
            max={max}
            step={step}
            value={value}
            onChange={(e) => onChange(Number(e.target.value))}
            aria-label={label}
            disabled={disabled}
            className={cn("mt-2 w-full accent-primary", disabled && "opacity-40 cursor-not-allowed")}
          />
        </div>
      </div>
      <div className="text-sm tabular-nums text-muted-foreground shrink-0 mt-0.5">{format(value)}</div>
    </div>
  );
}

// ── Section wrapper ──

// `id` is the group's URL anchor — must match the Display sub-entries in
// _sections.ts so the nav's sub-links land here.
function SettingsSection({
  id,
  title,
  children,
}: {
  id: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div id={id} className="scroll-mt-4">
      <h3 className="text-sm font-semibold text-muted-foreground mb-2">{title}</h3>
      <div className="rounded-md border border-border divide-y divide-border">
        {children}
      </div>
    </div>
  );
}

// ── Page ──

export default function DisplaySettingsPage() {
  const t = useTranslations("displaySettings");
  const { settings, setSetting, isLoading } = useUserSettings();
  const [widthRatio, setWidthRatio] = useDebouncedSetting<number>(
    "display.timeline_width_ratio",
    TIMELINE_WIDTH_RATIO_DEFAULT,
  );
  // Task #805: the width ratio is a desktop concept — on narrow viewports
  // (below md, phones) the timeline is full-width and the setting is inert,
  // so the slider is disabled here rather than promising an effect it won't
  // have. Same 768px boundary the layout itself uses.
  const { isNarrow } = useBreakpoint();

  // Skins contributed by the cluster's enabled plugins. Must be called
  // unconditionally (before any early return) — React hook rules.
  const { packs: themePacks } = useThemePacks();

  // Fetch models for the model picker visibility section.
  // Must be called unconditionally (before any early return) — React hook rules.
  const { data: modelsData } = useQuery({
    queryKey: ["models"],
    queryFn: () => api.getModels(),
    staleTime: Infinity,
  });

  if (isLoading) {
    return (
      <div className={cn("justify-center py-12", FLEX)}>
        <div className="size-5 animate-spin rounded-full border-2 border-primary border-t-transparent" />
      </div>
    );
  }

  // "Collapse details by default" reuses display.expand_runs_mode, the same
  // setting the header's Details control reads (see lib/content-toggle-store.ts):
  // a single global default, flipping it here is the same expand-all/collapse-all
  // as flipping the header control. The row inverts the underlying setting's
  // polarity — a "show" setting reads as "Collapse ... by default" — since that
  // is how the feature reads to the user.
  const expandRunsDefault = settings["display.expand_runs_mode"] === "all";

  const hiddenModels: string[] = (settings["models.hidden"] as string[] | undefined) ?? [];
  const modelGroups = groupedModels(modelsData);

  return (
    <div className="space-y-6">
      <SettingsSection id="display-language" title={t("language")}>
        <RadioRow
          icon={Languages}
          label={t("language")}
          description={t("languageDescription")}
          options={[
            { value: "en", label: t("english") },
            { value: "zh", label: t("chinese") },
          ]}
          value={(settings["display.language"] as string | undefined) ?? "en"}
          onChange={(v) => setSetting("display.language", v)}
        />
      </SettingsSection>

      {/* Skins exist only when a plugin contributes one, so the section
          appears with the first pack rather than standing empty offering the
          console's own palette as the only choice. */}
      {themePacks.length > 0 && (
        <SettingsSection id="display-theme" title="Theme">
          <RadioRow
            icon={Palette}
            label="Skin"
            description="A plugin-contributed color pack, applied over the active light/dark palette. Colors it does not set keep following light/dark. A pack marked 'no dark variant' pins the colors it does set across both modes, so the light/dark toggle will not change them"
            options={[
              { value: "", label: "Default (Ava)" },
              ...themePacks.map((p) => ({
                value: themePackId(p),
                // Marked per pack rather than only in the description: a pack
                // without a dark half disables the mode toggle for every color
                // it sets, and that is a property of the pack being chosen, not
                // of skins in general.
                label: `${p.name} (${p.plugin})${p.dark_tokens ? "" : " — no dark variant"}`,
              })),
            ]}
            value={(settings["display.theme_pack"] as string | null) ?? ""}
            onChange={(v) => setSetting("display.theme_pack", v === "" ? null : v)}
          />
        </SettingsSection>
      )}

      <SettingsSection id="display-agent-list" title={t("agentListDisplay")}>
        <ToggleRow
          icon={Monitor}
          label="Show machine name"
          description="On multi-machine deployments, show the host machine's name next to each agent row"
          value={settings["display.show_machine_name"] as boolean}
          onChange={(v) => setSetting("display.show_machine_name", v)}
        />
        <ToggleRow
          icon={CircleDot}
          label="Show agent status dot"
          description="Show a status color dot (running/idle/…) on each sidebar agent row; off by default to keep the list static and quiet"
          value={settings["display.show_agent_status"] as boolean}
          onChange={(v) => setSetting("display.show_agent_status", v)}
        />
        <ToggleRow
          icon={Activity}
          label="Show activity line"
          description="In wide-sidebar mode, show a second line of the agent's activity; off by default"
          value={settings["display.show_activity_line"] as boolean}
          onChange={(v) => setSetting("display.show_activity_line", v)}
        />
        <ToggleRow
          icon={EyeOff}
          label="Show terminated agents"
          description="Show terminated agents (red status dot) in the sidebar; off shows only live agents"
          value={settings["display.show_terminated"] === true}
          onChange={(v) => setSetting("display.show_terminated", v)}
        />
        <ToggleRow
          icon={CalendarClock}
          label="Show weekday in timestamps"
          description="Show the weekday in timeline card timestamps (e.g. [2026-05-06 Tue 16:56:29 GMT+8])"
          value={settings["display.show_timestamp_weekday"] as boolean}
          onChange={(v) => setSetting("display.show_timestamp_weekday", v)}
        />
        <RadioRow
          icon={Clock}
          label="Time display mode"
          description="The time shown at the right of each agent row"
          options={[
            { value: "last_active", label: "Last active" },
            { value: "spawned", label: "Spawned" },
            { value: "hidden", label: "Hidden" },
          ]}
          value={settings["display.time_mode"] as string}
          onChange={(v) => setSetting("display.time_mode", v)}
        />
        <RadioRow
          icon={Calendar}
          label="Date format"
          description="Display format for timestamps"
          options={[
            { value: "relative", label: "Relative (e.g. 3m ago)" },
            { value: "absolute", label: "Absolute (e.g. 7/12 15:45)" },
          ]}
          value={settings["display.date_format"] as string}
          onChange={(v) => setSetting("display.date_format", v)}
        />
      </SettingsSection>

      <SettingsSection id="display-timeline" title="Timeline">
        <SliderRow
          icon={MoveHorizontal}
          label="Timeline max width"
          description={`Max width of the conversation column as a fraction of the viewport (${Math.round(TIMELINE_WIDTH_RATIO_MIN * 100)}%–${Math.round(TIMELINE_WIDTH_RATIO_MAX * 100)}%), capped at 1280px so ultra-wide screens stay readable. Desktop only: on phones (below ${TIMELINE_NARROW_BREAKPOINT_PX}px) the timeline is full-width and this setting is ignored. Default ${Math.round(TIMELINE_WIDTH_RATIO_DEFAULT * 100)}% = 768px on a 1920px screen.`}
          min={TIMELINE_WIDTH_RATIO_MIN}
          max={TIMELINE_WIDTH_RATIO_MAX}
          step={0.05}
          value={widthRatio}
          onChange={setWidthRatio}
          format={(v) => `${Math.round(v * 100)}%`}
          disabled={isNarrow}
        />
        <ToggleRow
          icon={Rows3}
          label="Collapse details by default"
          description="A newly-toggled work block starts collapsed instead of open. Flipping this (or the header's Details chip) expands/collapses every work block at once"
          value={!expandRunsDefault}
          onChange={(v) => setSetting("display.expand_runs_mode", !v ? "none" : "all")}
        />
        <ToggleRow
          icon={Sparkles}
          label="Render reasoning as markdown"
          description="Render agent reasoning (thinking) content as formatted markdown; off shows the raw text"
          value={settings["display.render_reasoning_markdown"] as boolean}
          onChange={(v) => setSetting("display.render_reasoning_markdown", v)}
        />
      </SettingsSection>

      <SettingsSection id="display-context-bar" title="Context usage bar">
        <RadioRow
          icon={Ruler}
          label="Usage bar width"
          description="Width of the collapsed context-usage bar at the bottom of the composer; the expanded breakdown panel is unaffected"
          options={[
            { value: "compact", label: "Compact" },
            { value: "comfortable", label: "Comfortable (default)" },
            { value: "wide", label: "Wide" },
          ]}
          value={settings["display.context_meter_width"] as string}
          onChange={(v) => setSetting("display.context_meter_width", v)}
        />
      </SettingsSection>

      <SettingsSection id="display-notifications" title="Notifications">
        <ToggleRow
          icon={Bell}
          label="Awaiting-reply notification"
          description="Show a red dot + count badge in the sidebar for agents awaiting reply; off by default — check the Fleet page when you want to know"
          value={settings["notification.awaiting_reply"] as boolean}
          onChange={(v) => setSetting("notification.awaiting_reply", v)}
        />
      </SettingsSection>

      <SettingsSection id="display-confirmations" title="Confirmations">
        <ToggleRow
          icon={AlertTriangle}
          label="Confirm before terminate"
          description="Show a confirmation dialog when terminating an agent"
          value={settings["behavior.confirm_terminate"] as boolean}
          onChange={(v) => setSetting("behavior.confirm_terminate", v)}
        />
        <ToggleRow
          icon={AlertTriangle}
          label="Confirm before restart"
          description="Show a confirmation dialog when restarting an agent"
          value={settings["behavior.confirm_restart"] as boolean}
          onChange={(v) => setSetting("behavior.confirm_restart", v)}
        />
        <ToggleRow
          icon={AlertTriangle}
          label="Confirm before force kill"
          description="Show a confirmation dialog when force-killing (kill / SIGKILL) an agent"
          value={settings["behavior.confirm_force_kill"] as boolean}
          onChange={(v) => setSetting("behavior.confirm_force_kill", v)}
        />
      </SettingsSection>

      <SettingsSection id="display-model-picker" title="Model picker">
        {modelGroups.map(([provider, models]) => (
          <Fragment key={provider}>
            <div className="px-3 py-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground/70 bg-muted/30">
              {providerLabel(provider)}
            </div>
            {models.map((modelName) => {
              const hidden = hiddenModels.includes(modelName);
              const info = modelsData?.models[modelName];
              const supersededBy = info?.superseded_by ?? null;
              const superseded = isSuperseded(modelsData, modelName);
              const p = info?.pricing;
              return (
                <ToggleRow
                  key={modelName}
                  icon={Eye}
                  label={modelName}
                  description={
                    supersededBy ? (
                      <span className="italic">
                        superseded by {supersededBy} — kept out of the spawn picker; agents
                        can still be switched to it through settings/config
                      </span>
                    ) : p ? (
                      <span className="tabular-nums">
                        ${p.input.toFixed(2)} / ${p.cache_read.toFixed(2)} / ${p.output.toFixed(2)}
                      </span>
                    ) : null
                  }
                  value={!hidden}
                  disabled={superseded}
                  onChange={(v) => {
                    const next = v
                      ? hiddenModels.filter((m) => m !== modelName)
                      : [...hiddenModels, modelName];
                    setSetting("models.hidden", next);
                  }}
                />
              );
            })}
          </Fragment>
        ))}
      </SettingsSection>
    </div>
  );
}
