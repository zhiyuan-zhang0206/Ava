// Raw-context strip categories (P4-2, task #4023): the demo timeline's nine
// legend classes plus the "other" bucket. Both mappings are fail-visible —
// a kind the console does not know throws instead of silently landing in
// "other", so a future server kind surfaces at render time (the 3187 review
// condition). "other" only ever carries the two kinds that sat outside the
// demo's legend: attach rows and legacy human rows.

import type { RunTimelineMessage } from "@/lib/types";

/** The color identity of one strip part / message (the demo's `k-*`
 *  classes). Legend-independent: "compact" and "ib-sys" render the same
 *  swatch pair the demo's compact/system legend row lights. */
export type StripColorClass =
  | "think"
  | "text"
  | "call"
  | "out"
  | "note"
  | "ib-agent"
  | "ib-user"
  | "ib-sys"
  | "compact"
  | "prompt"
  | "other";

/** The nine legend rows; "sys" is the one row covering two color classes. */
export type StripLegendCategory =
  | "think"
  | "text"
  | "call"
  | "out"
  | "note"
  | "ib-agent"
  | "ib-user"
  | "sys"
  | "prompt";

export const STRIP_LEGEND_CATEGORIES: readonly StripLegendCategory[] = [
  "think",
  "text",
  "call",
  "out",
  "note",
  "ib-agent",
  "ib-user",
  "sys",
  "prompt",
];

type StripPartKind = RunTimelineMessage["parts"][number]["kind"];

function inboundClass(source: string | null): StripColorClass {
  if (source?.startsWith("agent:")) return "ib-agent";
  if (source === "user") return "ib-user";
  return "ib-sys";
}

/** One part's color class. `source` comes from the parent message (the
 *  inbound split is a message-level fact). */
export function stripPartClass(partKind: StripPartKind, source: string | null): StripColorClass {
  switch (partKind) {
    case "think":
      return "think";
    case "text":
      return "text";
    case "call":
      return "call";
    case "out":
      return "out";
    case "note":
      return "note";
    case "compact":
      return "compact";
    case "inbound":
      return inboundClass(source);
    case "attach":
      return "other";
    case "prompt":
      return "prompt";
    default: {
      const unknown: never = partKind;
      throw new Error(`run-timeline strip: unmapped part kind ${String(unknown)}`);
    }
  }
}

/** The message-level color class: AI messages read as "think" (the demo's
 *  msgClass), everything else collapses to its single part kind. */
export function stripMessageClass(message: RunTimelineMessage): StripColorClass {
  switch (message.kind) {
    case "ai":
      return "think";
    case "prompt":
      return "prompt";
    case "note":
      return "note";
    case "compact":
      return "compact";
    case "inbound":
      return inboundClass(message.source);
    case "attach":
      return "other";
    case "exec":
      return "out";
    default: {
      const unknown: never = message.kind;
      throw new Error(`run-timeline strip: unmapped message kind ${String(unknown)}`);
    }
  }
}

/** Whether a color class lights up under one legend category. */
export function legendMatches(colorClass: StripColorClass, active: StripLegendCategory): boolean {
  if (active === "sys") return colorClass === "compact" || colorClass === "ib-sys";
  return colorClass === active;
}
