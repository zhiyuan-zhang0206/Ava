"use client";

// Row primitives + the Timeline-colors section for the Display settings page.
// Split out of page.tsx so that file stays under the 500-line budget after the
// Timeline-colors section landed (task #3312): the page keeps the section
// composition, this module owns the presentational rows.

import { Switch } from "@/components/ui/switch";
import { FLEX, FLEX_1, MIN_W_0 } from "@/lib/layout";
import {
  COLOR_FAMILIES,
  COLOR_FAMILY_LABELS,
  COLOR_ITEM_LABELS,
  COLOR_SLOT_IDS,
  FAMILY_SWATCH,
  TIMELINE_COLOR_ITEMS,
  isColorFamily,
  type ColorFamily,
} from "@/lib/timeline-colors";
import { useUserSettings } from "@/lib/use-user-settings";
import { cn } from "@/lib/utils";

// ── Setting row components ──

export function ToggleRow({
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

export function RadioRow({
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
export function SliderRow({
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

// A palette picker row — one timeline visual's color family. The swatch
// mirrors the selected family; the select lists every family. Writes go
// straight through setSetting (a select commits at most one value per
// interaction, so no debounce is needed — the timeline recolors while the
// row updates optimistically).
function ColorRow({
  label,
  value,
  onChange,
}: {
  label: string;
  value: ColorFamily;
  onChange: (v: ColorFamily) => void;
}) {
  return (
    <div className={cn("items-center justify-between gap-4 px-3 py-2.5", FLEX)}>
      <div className="text-sm font-medium">{label}</div>
      <div className={cn("items-center gap-2 shrink-0", FLEX)}>
        <span
          aria-hidden
          className={cn("size-3 rounded-full border border-border", FAMILY_SWATCH[value])}
        />
        <select
          aria-label={label}
          value={value}
          onChange={(e) => onChange(e.target.value as ColorFamily)}
          className="text-xs bg-transparent border border-border rounded px-1.5 py-1 text-foreground focus:outline-none focus:ring-1 focus:ring-ring cursor-pointer"
        >
          {COLOR_FAMILIES.map((f) => (
            <option key={f} value={f}>
              {COLOR_FAMILY_LABELS[f]}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}

// ── Section wrapper ──

// `id` is the group's URL anchor — must match the Display sub-entries in
// _sections.ts so the nav's sub-links land here.
export function SettingsSection({
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

// ── Timeline colors ──

// One color row per configurable slot (tasks #3304 / #3312). Self-contained —
// it reads and writes the settings store itself, so page.tsx only drops the
// element into the composition.
export function TimelineColorsSection() {
  const { settings, setSetting } = useUserSettings();
  return (
    <SettingsSection id="display-colors" title="Timeline colors">
      {COLOR_SLOT_IDS.map((slot) => {
        const spec = TIMELINE_COLOR_ITEMS[slot];
        const value = settings[spec.key];
        return (
          <ColorRow
            key={slot}
            label={COLOR_ITEM_LABELS[slot]}
            value={isColorFamily(value) ? value : spec.default}
            onChange={(v) => setSetting(spec.key, v)}
          />
        );
      })}
    </SettingsSection>
  );
}
