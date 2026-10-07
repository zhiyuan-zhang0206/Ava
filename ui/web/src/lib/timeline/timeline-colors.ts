// Timeline color configuration — the single source for the per-kind color
// families of the timeline (message cards + system markers).
//
// Mechanism: every visual color is a *family* (emerald / teal / cyan / ...).
// Each family maps to an exhaustive, typed set of class literals (`CLASSES`),
// so Tailwind's scanner sees every class that can ever render — a missing
// family or shape fails `tsc` instead of silently dropping a style. Consumers
// (`messageCardConfig` / `markerVisual`) pick classes for a slot's resolved
// family; the resolved families come from the user settings
// (`display.color.<slot>`, stored as the family name). The default here is the
// user-approved palette (2026-09-14): the agent-written code block (tool call)
// is CYAN — it used to collide with the emerald text output; thinking is
// BLUE-400/40 (was slate); the System note family is TEAL (was muted gray,
// which collided with thinking); the user message stays gray; everything else
// is unchanged from the previous palette.

export const COLOR_FAMILIES = [
  "emerald",
  "teal",
  "cyan",
  "sky",
  "blue",
  "indigo",
  "violet",
  "fuchsia",
  "pink",
  "rose",
  "red",
  "orange",
  "amber",
  "gray",
] as const;

export type ColorFamily = (typeof COLOR_FAMILIES)[number];

/** Class shapes used across the timeline visuals. Every string in CLASSES is
 *  a full literal (no runtime interpolation) so every renderable class is
 *  scannable at build time. */
export interface FamilyClasses {
  readonly border500_50: string;
  readonly border500_70: string;
  readonly border500_40: string;
  readonly border400_40: string;
  readonly border400_50: string;
  readonly border400_60: string;
  readonly bg50_40_950_15: string;
  readonly bg50_60_900_20: string;
  readonly bg50_40_950_10: string;
  readonly bg50_950_20: string;
  readonly bg50_900_30: string;
  readonly bg50_950_30: string;
  readonly text700_300: string;
}

export type ColorShape = keyof FamilyClasses;

const CLASSES: Record<ColorFamily, FamilyClasses> = {
  emerald: {
    border500_50: "border-emerald-500/50",
    border500_70: "border-emerald-500/70",
    border500_40: "border-emerald-500/40",
    border400_40: "border-emerald-400/40",
    border400_50: "border-emerald-400/50",
    border400_60: "border-emerald-400/60",
    bg50_40_950_15: "bg-emerald-50/40 dark:bg-emerald-950/15",
    bg50_60_900_20: "bg-emerald-50/60 dark:bg-emerald-900/20",
    bg50_40_950_10: "bg-emerald-50/40 dark:bg-emerald-950/10",
    bg50_950_20: "bg-emerald-50 dark:bg-emerald-950/20",
    bg50_900_30: "bg-emerald-50 dark:bg-emerald-900/30",
    bg50_950_30: "bg-emerald-50 dark:bg-emerald-950/30",
    text700_300: "text-emerald-700 dark:text-emerald-300",
  },
  teal: {
    border500_50: "border-teal-500/50",
    border500_70: "border-teal-500/70",
    border500_40: "border-teal-500/40",
    border400_40: "border-teal-400/40",
    border400_50: "border-teal-400/50",
    border400_60: "border-teal-400/60",
    bg50_40_950_15: "bg-teal-50/40 dark:bg-teal-950/15",
    bg50_60_900_20: "bg-teal-50/60 dark:bg-teal-900/20",
    bg50_40_950_10: "bg-teal-50/40 dark:bg-teal-950/10",
    bg50_950_20: "bg-teal-50 dark:bg-teal-950/20",
    bg50_900_30: "bg-teal-50 dark:bg-teal-900/30",
    bg50_950_30: "bg-teal-50 dark:bg-teal-950/30",
    text700_300: "text-teal-700 dark:text-teal-300",
  },
  cyan: {
    border500_50: "border-cyan-500/50",
    border500_70: "border-cyan-500/70",
    border500_40: "border-cyan-500/40",
    border400_40: "border-cyan-400/40",
    border400_50: "border-cyan-400/50",
    border400_60: "border-cyan-400/60",
    bg50_40_950_15: "bg-cyan-50/40 dark:bg-cyan-950/15",
    bg50_60_900_20: "bg-cyan-50/60 dark:bg-cyan-900/20",
    bg50_40_950_10: "bg-cyan-50/40 dark:bg-cyan-950/10",
    bg50_950_20: "bg-cyan-50 dark:bg-cyan-950/20",
    bg50_900_30: "bg-cyan-50 dark:bg-cyan-900/30",
    bg50_950_30: "bg-cyan-50 dark:bg-cyan-950/30",
    text700_300: "text-cyan-700 dark:text-cyan-300",
  },
  sky: {
    border500_50: "border-sky-500/50",
    border500_70: "border-sky-500/70",
    border500_40: "border-sky-500/40",
    border400_40: "border-sky-400/40",
    border400_50: "border-sky-400/50",
    border400_60: "border-sky-400/60",
    bg50_40_950_15: "bg-sky-50/40 dark:bg-sky-950/15",
    bg50_60_900_20: "bg-sky-50/60 dark:bg-sky-900/20",
    bg50_40_950_10: "bg-sky-50/40 dark:bg-sky-950/10",
    bg50_950_20: "bg-sky-50 dark:bg-sky-950/20",
    bg50_900_30: "bg-sky-50 dark:bg-sky-900/30",
    bg50_950_30: "bg-sky-50 dark:bg-sky-950/30",
    text700_300: "text-sky-700 dark:text-sky-300",
  },
  blue: {
    border500_50: "border-blue-500/50",
    border500_70: "border-blue-500/70",
    border500_40: "border-blue-500/40",
    border400_40: "border-blue-400/40",
    border400_50: "border-blue-400/50",
    border400_60: "border-blue-400/60",
    bg50_40_950_15: "bg-blue-50/40 dark:bg-blue-950/15",
    bg50_60_900_20: "bg-blue-50/60 dark:bg-blue-900/20",
    bg50_40_950_10: "bg-blue-50/40 dark:bg-blue-950/10",
    bg50_950_20: "bg-blue-50 dark:bg-blue-950/20",
    bg50_900_30: "bg-blue-50 dark:bg-blue-900/30",
    bg50_950_30: "bg-blue-50 dark:bg-blue-950/30",
    text700_300: "text-blue-700 dark:text-blue-300",
  },
  indigo: {
    border500_50: "border-indigo-500/50",
    border500_70: "border-indigo-500/70",
    border500_40: "border-indigo-500/40",
    border400_40: "border-indigo-400/40",
    border400_50: "border-indigo-400/50",
    border400_60: "border-indigo-400/60",
    bg50_40_950_15: "bg-indigo-50/40 dark:bg-indigo-950/15",
    bg50_60_900_20: "bg-indigo-50/60 dark:bg-indigo-900/20",
    bg50_40_950_10: "bg-indigo-50/40 dark:bg-indigo-950/10",
    bg50_950_20: "bg-indigo-50 dark:bg-indigo-950/20",
    bg50_900_30: "bg-indigo-50 dark:bg-indigo-900/30",
    bg50_950_30: "bg-indigo-50 dark:bg-indigo-950/30",
    text700_300: "text-indigo-700 dark:text-indigo-300",
  },
  violet: {
    border500_50: "border-violet-500/50",
    border500_70: "border-violet-500/70",
    border500_40: "border-violet-500/40",
    border400_40: "border-violet-400/40",
    border400_50: "border-violet-400/50",
    border400_60: "border-violet-400/60",
    bg50_40_950_15: "bg-violet-50/40 dark:bg-violet-950/15",
    bg50_60_900_20: "bg-violet-50/60 dark:bg-violet-900/20",
    bg50_40_950_10: "bg-violet-50/40 dark:bg-violet-950/10",
    bg50_950_20: "bg-violet-50 dark:bg-violet-950/20",
    bg50_900_30: "bg-violet-50 dark:bg-violet-900/30",
    bg50_950_30: "bg-violet-50 dark:bg-violet-950/30",
    text700_300: "text-violet-700 dark:text-violet-300",
  },
  fuchsia: {
    border500_50: "border-fuchsia-500/50",
    border500_70: "border-fuchsia-500/70",
    border500_40: "border-fuchsia-500/40",
    border400_40: "border-fuchsia-400/40",
    border400_50: "border-fuchsia-400/50",
    border400_60: "border-fuchsia-400/60",
    bg50_40_950_15: "bg-fuchsia-50/40 dark:bg-fuchsia-950/15",
    bg50_60_900_20: "bg-fuchsia-50/60 dark:bg-fuchsia-900/20",
    bg50_40_950_10: "bg-fuchsia-50/40 dark:bg-fuchsia-950/10",
    bg50_950_20: "bg-fuchsia-50 dark:bg-fuchsia-950/20",
    bg50_900_30: "bg-fuchsia-50 dark:bg-fuchsia-900/30",
    bg50_950_30: "bg-fuchsia-50 dark:bg-fuchsia-950/30",
    text700_300: "text-fuchsia-700 dark:text-fuchsia-300",
  },
  pink: {
    border500_50: "border-pink-500/50",
    border500_70: "border-pink-500/70",
    border500_40: "border-pink-500/40",
    border400_40: "border-pink-400/40",
    border400_50: "border-pink-400/50",
    border400_60: "border-pink-400/60",
    bg50_40_950_15: "bg-pink-50/40 dark:bg-pink-950/15",
    bg50_60_900_20: "bg-pink-50/60 dark:bg-pink-900/20",
    bg50_40_950_10: "bg-pink-50/40 dark:bg-pink-950/10",
    bg50_950_20: "bg-pink-50 dark:bg-pink-950/20",
    bg50_900_30: "bg-pink-50 dark:bg-pink-900/30",
    bg50_950_30: "bg-pink-50 dark:bg-pink-950/30",
    text700_300: "text-pink-700 dark:text-pink-300",
  },
  rose: {
    border500_50: "border-rose-500/50",
    border500_70: "border-rose-500/70",
    border500_40: "border-rose-500/40",
    border400_40: "border-rose-400/40",
    border400_50: "border-rose-400/50",
    border400_60: "border-rose-400/60",
    bg50_40_950_15: "bg-rose-50/40 dark:bg-rose-950/15",
    bg50_60_900_20: "bg-rose-50/60 dark:bg-rose-900/20",
    bg50_40_950_10: "bg-rose-50/40 dark:bg-rose-950/10",
    bg50_950_20: "bg-rose-50 dark:bg-rose-950/20",
    bg50_900_30: "bg-rose-50 dark:bg-rose-900/30",
    bg50_950_30: "bg-rose-50 dark:bg-rose-950/30",
    text700_300: "text-rose-700 dark:text-rose-300",
  },
  red: {
    border500_50: "border-red-500/50",
    border500_70: "border-red-500/70",
    border500_40: "border-red-500/40",
    border400_40: "border-red-400/40",
    border400_50: "border-red-400/50",
    border400_60: "border-red-400/60",
    bg50_40_950_15: "bg-red-50/40 dark:bg-red-950/15",
    bg50_60_900_20: "bg-red-50/60 dark:bg-red-900/20",
    bg50_40_950_10: "bg-red-50/40 dark:bg-red-950/10",
    bg50_950_20: "bg-red-50 dark:bg-red-950/20",
    bg50_900_30: "bg-red-50 dark:bg-red-900/30",
    bg50_950_30: "bg-red-50 dark:bg-red-950/30",
    text700_300: "text-red-700 dark:text-red-300",
  },
  orange: {
    border500_50: "border-orange-500/50",
    border500_70: "border-orange-500/70",
    border500_40: "border-orange-500/40",
    border400_40: "border-orange-400/40",
    border400_50: "border-orange-400/50",
    border400_60: "border-orange-400/60",
    bg50_40_950_15: "bg-orange-50/40 dark:bg-orange-950/15",
    bg50_60_900_20: "bg-orange-50/60 dark:bg-orange-900/20",
    bg50_40_950_10: "bg-orange-50/40 dark:bg-orange-950/10",
    bg50_950_20: "bg-orange-50 dark:bg-orange-950/20",
    bg50_900_30: "bg-orange-50 dark:bg-orange-900/30",
    bg50_950_30: "bg-orange-50 dark:bg-orange-950/30",
    text700_300: "text-orange-700 dark:text-orange-300",
  },
  amber: {
    border500_50: "border-amber-500/50",
    border500_70: "border-amber-500/70",
    border500_40: "border-amber-500/40",
    border400_40: "border-amber-400/40",
    border400_50: "border-amber-400/50",
    border400_60: "border-amber-400/60",
    bg50_40_950_15: "bg-amber-50/40 dark:bg-amber-950/15",
    bg50_60_900_20: "bg-amber-50/60 dark:bg-amber-900/20",
    bg50_40_950_10: "bg-amber-50/40 dark:bg-amber-950/10",
    bg50_950_20: "bg-amber-50 dark:bg-amber-950/20",
    bg50_900_30: "bg-amber-50 dark:bg-amber-900/30",
    bg50_950_30: "bg-amber-50 dark:bg-amber-950/30",
    text700_300: "text-amber-700 dark:text-amber-300",
  },
  gray: {
    border500_50: "border-gray-500/50",
    border500_70: "border-gray-500/70",
    border500_40: "border-gray-500/40",
    border400_40: "border-gray-400/40",
    border400_50: "border-gray-400/50",
    border400_60: "border-gray-400/60",
    bg50_40_950_15: "bg-gray-50/40 dark:bg-gray-950/15",
    bg50_60_900_20: "bg-gray-50/60 dark:bg-gray-900/20",
    bg50_40_950_10: "bg-gray-50/40 dark:bg-gray-950/10",
    bg50_950_20: "bg-gray-50 dark:bg-gray-950/20",
    bg50_900_30: "bg-gray-50 dark:bg-gray-900/30",
    bg50_950_30: "bg-gray-50 dark:bg-gray-950/30",
    text700_300: "text-gray-700 dark:text-gray-300",
  },
};

/** Solid 500-swap per family — the settings panel's color dot. */
export const FAMILY_SWATCH: Record<ColorFamily, string> = {
  emerald: "bg-emerald-500",
  teal: "bg-teal-500",
  cyan: "bg-cyan-500",
  sky: "bg-sky-500",
  blue: "bg-blue-500",
  indigo: "bg-indigo-500",
  violet: "bg-violet-500",
  fuchsia: "bg-fuchsia-500",
  pink: "bg-pink-500",
  rose: "bg-rose-500",
  red: "bg-red-500",
  orange: "bg-orange-500",
  amber: "bg-amber-500",
  gray: "bg-gray-500",
};

export const COLOR_SLOT_IDS = [
  "agent_chat",
  "agent_code",
  "code_output",
  "reasoning",
  "inbound_human",
  "inbound_agent",
  "inbound_system",
  "system_prompt",
  "attach",
  "note",
  "memory",
  "lifecycle_terminate",
  "lifecycle_restart",
  "lifecycle_resurrect",
  "lifecycle_fork",
] as const;

export type ColorSlotId = (typeof COLOR_SLOT_IDS)[number];

export interface ColorItemSpec {
  /** The user-settings key holding this slot's family override. */
  readonly key: string;
  readonly default: ColorFamily;
  readonly border: ColorShape;
  readonly bg: ColorShape | null;
  readonly text: ColorShape | null;
}

/** One configurable item per distinct timeline visual. The slot set mirrors
 *  the user-approved inventory (2026-09-14): every card kind with a colored
 *  border, the note / memory / lifecycle markers, in the order the settings
 *  panel lists them. */
export const TIMELINE_COLOR_ITEMS: Record<ColorSlotId, ColorItemSpec> = {
  agent_chat: { key: "display.color.agent_chat", default: "emerald", border: "border500_50", bg: null, text: null },
  agent_code: { key: "display.color.agent_code", default: "cyan", border: "border500_70", bg: null, text: null },
  code_output: { key: "display.color.code_output", default: "amber", border: "border500_50", bg: "bg50_40_950_10", text: null },
  reasoning: { key: "display.color.reasoning", default: "blue", border: "border400_40", bg: "bg50_60_900_20", text: null },
  inbound_human: { key: "display.color.inbound_human", default: "gray", border: "border400_60", bg: "bg50_900_30", text: null },
  inbound_agent: { key: "display.color.inbound_agent", default: "violet", border: "border400_60", bg: "bg50_950_20", text: null },
  inbound_system: { key: "display.color.inbound_system", default: "sky", border: "border400_60", bg: "bg50_950_20", text: null },
  system_prompt: { key: "display.color.system_prompt", default: "indigo", border: "border400_40", bg: "bg50_40_950_15", text: null },
  attach: { key: "display.color.attach", default: "sky", border: "border400_50", bg: "bg50_40_950_10", text: null },
  note: { key: "display.color.note", default: "teal", border: "border500_40", bg: "bg50_40_950_15", text: "text700_300" },
  memory: { key: "display.color.memory", default: "violet", border: "border400_60", bg: "bg50_950_30", text: "text700_300" },
  lifecycle_terminate: { key: "display.color.lifecycle_terminate", default: "rose", border: "border400_60", bg: "bg50_950_30", text: "text700_300" },
  lifecycle_restart: { key: "display.color.lifecycle_restart", default: "amber", border: "border400_60", bg: "bg50_950_30", text: "text700_300" },
  lifecycle_resurrect: { key: "display.color.lifecycle_resurrect", default: "emerald", border: "border400_60", bg: "bg50_950_30", text: "text700_300" },
  lifecycle_fork: { key: "display.color.lifecycle_fork", default: "sky", border: "border400_60", bg: "bg50_950_30", text: "text700_300" },
};

/** Display names for the Timeline-colors settings rows. Hardcoded English,
 *  like the page's other newer sections (Timeline, Context usage bar, …) —
 *  15 i18n keys would be symmetry weight for a settings-only surface. The
 *  two tool entries follow the user's 2026-09-14 copy ruling (tool call /
 *  tool output wording; never the executor name). */
export const COLOR_ITEM_LABELS: Record<ColorSlotId, string> = {
  agent_chat: "Agent message",
  agent_code: "Tool call (code)",
  code_output: "Tool output",
  reasoning: "Thinking",
  inbound_human: "Human message",
  inbound_agent: "Agent message (inbound)",
  inbound_system: "System message (inbound)",
  system_prompt: "System prompt",
  attach: "Attachment",
  note: "Note",
  memory: "Memory",
  lifecycle_terminate: "Terminated",
  lifecycle_restart: "Restarted",
  lifecycle_resurrect: "Resurrected",
  lifecycle_fork: "Forked",
};

/** Human names for the selectable families — option text in the color rows. */
export const COLOR_FAMILY_LABELS: Record<ColorFamily, string> = {
  emerald: "Emerald",
  teal: "Teal",
  cyan: "Cyan",
  sky: "Sky",
  blue: "Blue",
  indigo: "Indigo",
  violet: "Violet",
  fuchsia: "Fuchsia",
  pink: "Pink",
  rose: "Rose",
  red: "Red",
  orange: "Orange",
  amber: "Amber",
  gray: "Gray",
};

export interface ResolvedColorClasses {
  readonly border: string;
  readonly bg: string | null;
  readonly text: string | null;
}

export type TimelineColors = Record<ColorSlotId, ResolvedColorClasses>;

export function isColorFamily(v: unknown): v is ColorFamily {
  return typeof v === "string" && (COLOR_FAMILIES as readonly string[]).includes(v);
}

// Last resolution, keyed by the signature of the values it read. The hook
// calls resolve on every render (any settings write replaces the settings
// object); this keeps the result reference-stable while no color value
// changed, which the timeline's per-row config memo relies on.
let lastResolved: { signature: string; colors: TimelineColors } | null = null;

/** Read the 15 color values as one signature string. Only a stored family
 *  string is meaningful; anything else reads as empty and resolves to the
 *  slot default the same way in both the signature and the resolution. */
function colorSignature(settings: Record<string, unknown>): string {
  return COLOR_SLOT_IDS.map((id) => {
    const v = settings[TIMELINE_COLOR_ITEMS[id].key];
    return typeof v === "string" ? v : "";
  }).join("|");
}

/** Resolve every slot's classes from the settings map. Values that are not a
 *  known family (hand-edited DB row, older value shape) fall back to the
 *  slot's default — the UI can never render an unstyled border because of a
 *  bad stored value. Memoized by the signature of the values it reads, so an
 *  unchanged input returns the same reference (pure for callers). */
export function resolveTimelineColors(settings: Record<string, unknown>): TimelineColors {
  const signature = colorSignature(settings);
  if (lastResolved !== null && lastResolved.signature === signature) {
    return lastResolved.colors;
  }
  const out = {} as Record<ColorSlotId, ResolvedColorClasses>;
  for (const id of COLOR_SLOT_IDS) {
    const spec = TIMELINE_COLOR_ITEMS[id];
    const family = isColorFamily(settings[spec.key]) ? (settings[spec.key] as ColorFamily) : spec.default;
    const classes = CLASSES[family];
    out[id] = {
      border: classes[spec.border],
      bg: spec.bg ? classes[spec.bg] : null,
      text: spec.text ? classes[spec.text] : null,
    };
  }
  lastResolved = { signature, colors: out };
  return out;
}

/** The palette with no user overrides — safe default for pure callers
 *  (`messageCardConfig(item)` in tests, etc.). */
export const DEFAULT_TIMELINE_COLORS: TimelineColors = resolveTimelineColors({});
